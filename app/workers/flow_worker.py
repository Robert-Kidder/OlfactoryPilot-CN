from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

from app.services.flow_service import FlowApplyResult, FlowService


@dataclass(frozen=True, slots=True)
class FlowCommand:
    command_id: str
    execution_epoch: int
    sequence: int
    mode: str
    a: float
    b: float
    c: float
    source: str


@dataclass(frozen=True, slots=True)
class FlowCommandResult:
    command: FlowCommand
    result: FlowApplyResult
    stale: bool = False


class FlowWorker(QThread):
    """Single serial owner for ordered MFC commands."""

    result_ready = Signal(object)

    def __init__(
        self,
        service: FlowService,
        parent: QObject | None = None,
        *,
        airflow_poll_interval_s: float = 0.2,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._condition = threading.Condition()
        self._queue: deque[FlowCommand] = deque()
        self._running = False
        self._accepting = True
        self._active_command: FlowCommand | None = None
        self._execution_epoch: int | None = None
        self._device_lease = "idle"
        self._airflow_sink: Callable[[float, float, str | None], None] | None = None
        self._airflow_poll_interval_s = max(0.02, float(airflow_poll_interval_s))
        self._next_airflow_poll = 0.0

    def set_airflow_sink(
        self,
        sink: Callable[[float, float, str | None], None] | None,
    ) -> None:
        with self._condition:
            self._airflow_sink = sink
            self._next_airflow_poll = 0.0
            self._condition.notify_all()

    def submit(self, command: FlowCommand) -> bool:
        with self._condition:
            if not self._accepting:
                return False
            if self._execution_epoch is None:
                self._execution_epoch = command.execution_epoch
            elif (
                command.execution_epoch > self._execution_epoch
                and self._device_lease == "idle"
                and self._active_command is None
                and not self._queue
            ):
                # An idle owner may atomically advance to ActuationWorker's
                # newer namespace.  It can never roll back to an old epoch.
                self._execution_epoch = command.execution_epoch
            if self._command_rejection_reason_locked(command):
                return False
            self._queue.append(command)
            self._condition.notify_all()
            return True

    def acquire_protocol_lease(self, execution_epoch: int) -> bool:
        """Atomically prevent queued/in-flight manual flow work crossing protocol start."""
        with self._condition:
            if not self._accepting:
                return False
            if self._active_command is not None or self._queue:
                return False
            requested_epoch = int(execution_epoch)
            if self._execution_epoch is not None and requested_epoch < self._execution_epoch:
                return False
            self._execution_epoch = requested_epoch
            self._device_lease = "protocol"
        return True

    def release_protocol_lease(
        self,
        execution_epoch: int,
        *,
        next_execution_epoch: int | None = None,
    ) -> bool:
        with self._condition:
            requested_epoch = int(execution_epoch)
            if self._device_lease != "protocol":
                return False
            if self._execution_epoch != requested_epoch:
                return False
            next_epoch = (
                requested_epoch
                if next_execution_epoch is None
                else int(next_execution_epoch)
            )
            if next_epoch < requested_epoch:
                return False
            self._execution_epoch = next_epoch
            self._device_lease = "idle"
            return True

    @property
    def has_in_flight_command(self) -> bool:
        with self._condition:
            return self._active_command is not None

    @property
    def execution_context(self) -> tuple[int | None, str, bool, int]:
        """Return an atomic diagnostic snapshot without exposing mutable internals."""
        with self._condition:
            return (
                self._execution_epoch,
                self._device_lease,
                self._accepting,
                len(self._queue),
            )

    def process_ready(self, *, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            with self._condition:
                if not self._queue:
                    break
                command = self._queue.popleft()
                reason = self._command_rejection_reason_locked(command)
                if not reason:
                    self._active_command = command
            if reason:
                result = self._cancelled_result(command, reason)
            else:
                try:
                    result = self._apply(command)
                finally:
                    with self._condition:
                        self._active_command = None
                        self._condition.notify_all()
            self.result_ready.emit(FlowCommandResult(command=command, result=result))
            processed += 1
        return processed

    def run(self) -> None:
        with self._condition:
            if not self._accepting:
                return
            self._running = True
        try:
            while self._running:
                if self.process_ready(max_items=1):
                    continue
                now = time.monotonic()
                if self._airflow_sink is not None and now >= self._next_airflow_poll:
                    self._poll_airflow()
                    self._next_airflow_poll = now + self._airflow_poll_interval_s
                    continue
                with self._condition:
                    if self._running and not self._queue:
                        timeout = None
                        if self._airflow_sink is not None:
                            timeout = max(0.0, self._next_airflow_poll - time.monotonic())
                        self._condition.wait(timeout)
        finally:
            releaser = getattr(self.service.hal, "release_serial_resources", None)
            if releaser is not None:
                releaser()

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        cancelled: list[FlowCommand] = []
        with self._condition:
            self._accepting = False
            self._running = False
            while self._queue:
                cancelled.append(self._queue.popleft())
            self._condition.notify_all()
        self._emit_cancelled(cancelled, "流量 worker 关闭，命令已取消。")
        if self.isRunning():
            return bool(self.wait(max(1, int(timeout_ms))))
        return True

    def prepare_restart(self, *, execution_epoch: int | None = None) -> bool:
        """Reopen only after explicitly rebinding a previously observed epoch."""
        if self.isRunning():
            return True
        with self._condition:
            if execution_epoch is None:
                if self._execution_epoch is not None:
                    return False
            else:
                requested_epoch = int(execution_epoch)
                if (
                    self._execution_epoch is not None
                    and requested_epoch < self._execution_epoch
                ):
                    return False
                self._execution_epoch = requested_epoch
            self._accepting = True
            self._device_lease = "idle"
        return True

    def _command_rejection_reason_locked(self, command: FlowCommand) -> str:
        if self._execution_epoch is not None and command.execution_epoch != self._execution_epoch:
            return "流量命令 execution epoch 已失效，未写入硬件。"
        if self._device_lease == "protocol":
            return "协议已持有设备租约，流量命令已取消。"
        return ""

    @staticmethod
    def _cancelled_result(command: FlowCommand, message: str) -> FlowApplyResult:
        return FlowApplyResult(
            False,
            message,
            command.a,
            command.b,
            command.c,
            command.a + command.c,
            "cancelled",
        )

    def _emit_cancelled(self, commands: list[FlowCommand], message: str) -> None:
        for command in commands:
            self.result_ready.emit(
                FlowCommandResult(
                    command=command,
                    result=self._cancelled_result(command, message),
                )
            )

    def _apply(self, command: FlowCommand) -> FlowApplyResult:
        if command.mode == "zero":
            return self.service.apply_zero()
        return self.service.apply_flows(
            a_target=command.a,
            b_target=command.b,
            c_target=command.c,
            mode=command.mode,
        )

    def _poll_airflow(self) -> None:
        sink = self._airflow_sink
        if sink is None:
            return
        timestamp = time.time()
        try:
            value = float(self.service.hal.read_flow())
        except Exception as exc:  # serial errors must invalidate readiness immediately
            sink(float("nan"), timestamp, f"{type(exc).__name__}: {exc}")
            return
        sink(value, timestamp, None)
