from __future__ import annotations

import threading
from collections import deque
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


class FlowWorker(QThread):
    """Single serial owner for ordered MFC commands."""

    result_ready = Signal(object)

    def __init__(self, service: FlowService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._condition = threading.Condition()
        self._queue: deque[FlowCommand] = deque()
        self._running = False
        self._accepting = True

    def submit(self, command: FlowCommand) -> bool:
        with self._condition:
            if not self._accepting:
                return False
            self._queue.append(command)
            self._condition.notify_all()
            return True

    def process_ready(self, *, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            with self._condition:
                if not self._queue:
                    break
                command = self._queue.popleft()
            result = self._apply(command)
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
                with self._condition:
                    if self._running and not self._queue:
                        self._condition.wait()
        finally:
            releaser = getattr(self.service.hal, "release_serial_resources", None)
            if releaser is not None:
                releaser()

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        with self._condition:
            self._accepting = False
            self._running = False
            self._condition.notify_all()
        if self.isRunning():
            return bool(self.wait(max(1, int(timeout_ms))))
        return True

    def prepare_restart(self) -> bool:
        if self.isRunning():
            return True
        with self._condition:
            self._accepting = True
        return True

    def _apply(self, command: FlowCommand) -> FlowApplyResult:
        if command.mode == "zero":
            return self.service.apply_zero()
        return self.service.apply_flows(
            a_target=command.a,
            b_target=command.b,
            c_target=command.c,
            mode=command.mode,
        )
