from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

from app.models import (
    AZeroReceipt,
    CommissioningMonitorStatus,
    DeviceLeaseKind,
    DeviceLeaseToken,
    ExclusiveDeviceLease,
    FlowSettlingMonitor,
    MaintenanceLeaseReleaseEvidence,
    RealSupplyPolicy,
    SafeStopIdentity,
)
from app.services.flow_service import FlowApplyResult, FlowService
from app.services.hal import FlowReadbackSnapshot


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
    operation_id: str | None = None
    generation: int | None = None
    lease_token: str | None = None


@dataclass(frozen=True, slots=True)
class FlowCommandResult:
    command: FlowCommand
    result: FlowApplyResult
    stale: bool = False


@dataclass(frozen=True, slots=True)
class _RealSupplyAuthorization:
    operation_id: str
    generation: int
    targets_sccm: tuple[float, float, float]
    policy: RealSupplyPolicy


LOG = logging.getLogger(__name__)


class FlowWorker(QThread):
    """Single serial owner for ordered MFC commands."""

    result_ready = Signal(object)
    flow_snapshot_ready = Signal(object)
    commissioning_result_ready = Signal(object)

    def __init__(
        self,
        service: FlowService,
        parent: QObject | None = None,
        *,
        airflow_poll_interval_s: float = 0.2,
        device_lease: ExclusiveDeviceLease | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._condition = threading.Condition()
        self._queue: deque[FlowCommand] = deque()
        self._running = False
        self._accepting = True
        self._active_command: FlowCommand | None = None
        self._execution_epoch: int | None = None
        self._lease = device_lease or ExclusiveDeviceLease()
        self._protocol_lease_token: DeviceLeaseToken | None = None
        self._airflow_sink: Callable[[float, float, str | None], None] | None = None
        self._airflow_poll_interval_s = max(0.02, float(airflow_poll_interval_s))
        self._next_airflow_poll = 0.0
        self._shutdown_sequence = 0
        self._safe_stop_identity: SafeStopIdentity | None = None
        self._safe_stop_lease_token: DeviceLeaseToken | None = None
        self._shutdown_waiters: dict[
            str,
            tuple[threading.Event, list[FlowCommandResult]],
        ] = {}
        self._real_supply_authorization: _RealSupplyAuthorization | None = None
        self._consumed_real_supply_identity: tuple[str, int] | None = None
        self._commissioning_monitor: FlowSettlingMonitor | None = None
        self._commissioning_audit_next_ns = 0

    def set_airflow_sink(
        self,
        sink: Callable[[float, float, str | None], None] | None,
    ) -> None:
        with self._condition:
            self._airflow_sink = sink
            self._next_airflow_poll = 0.0
            self._condition.notify_all()

    def submit(self, command: FlowCommand) -> bool:
        cancelled: list[FlowCommand] = []
        with self._condition:
            if not self._accepting:
                return False
            if command.source == "safety:safe-stop":
                if command.operation_id is None or command.generation is None:
                    return False
                try:
                    identity = SafeStopIdentity(
                        command.operation_id,
                        command.generation,
                        command.execution_epoch,
                    )
                except ValueError:
                    return False
                if (
                    self._execution_epoch is not None
                    and identity.execution_epoch < self._execution_epoch
                ):
                    return False
                if (
                    self._safe_stop_identity is not None
                    and self._safe_stop_identity != identity
                    and identity.execution_epoch
                    <= self._safe_stop_identity.execution_epoch
                ):
                    return False
                self._safe_stop_identity = identity
                self._safe_stop_lease_token = self._lease.snapshot
                self._cancel_real_supply_locked()
                while self._queue:
                    cancelled.append(self._queue.popleft())
                self._execution_epoch = max(
                    int(self._execution_epoch or 0),
                    identity.execution_epoch,
                )
            if self._execution_epoch is None:
                self._execution_epoch = command.execution_epoch
            elif (
                command.execution_epoch > self._execution_epoch
                and self._lease.snapshot.kind == DeviceLeaseKind.IDLE
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
        self._emit_cancelled(cancelled, "安全停止已失效旧流量命令。")
        return True

    def authorize_real_supply(
        self,
        *,
        operation_id: str,
        generation: int,
        targets_sccm: tuple[float, float, float],
        policy: RealSupplyPolicy,
    ) -> bool:
        """Bind one frozen policy to the exact manual lease identity."""

        with self._condition:
            try:
                policy.validate_controller_targets(targets_sccm)
            except (TypeError, ValueError):
                return False
            if (
                not policy.enabled
                or self._real_supply_authorization is not None
                or self._commissioning_monitor is not None
                or not self._lease.matches(
                    kind=DeviceLeaseKind.MANUAL,
                    operation_id=operation_id,
                    generation=generation,
                    token=self._lease.snapshot.token,
                )
            ):
                return False
            self._real_supply_authorization = _RealSupplyAuthorization(
                operation_id=str(operation_id),
                generation=int(generation),
                targets_sccm=tuple(float(value) for value in targets_sccm),
                policy=policy,
            )
            self._consumed_real_supply_identity = None
            return True

    def cancel_real_supply_authorization(self, operation_id: str) -> None:
        with self._condition:
            if (
                self._real_supply_authorization is not None
                and self._real_supply_authorization.operation_id == operation_id
            ):
                self._consumed_real_supply_identity = (
                    self._real_supply_authorization.operation_id,
                    self._real_supply_authorization.generation,
                )
                self._real_supply_authorization = None
            if (
                self._commissioning_monitor is not None
                and self._commissioning_monitor.operation_id == operation_id
            ):
                self._commissioning_monitor = None
                self._commissioning_audit_next_ns = 0
            self._condition.notify_all()

    def acquire_protocol_lease(self, execution_epoch: int) -> bool:
        """Atomically prevent queued/in-flight manual flow work crossing protocol start."""
        with self._condition:
            if not self._accepting or self._safe_stop_identity is not None:
                return False
            if self._active_command is not None or self._queue:
                return False
            requested_epoch = int(execution_epoch)
            if self._execution_epoch is not None and requested_epoch < self._execution_epoch:
                return False
            if self._protocol_lease_token is None:
                token = self._lease.acquire(
                    DeviceLeaseKind.PROTOCOL,
                    operation_id=f"protocol:{requested_epoch}",
                    generation=requested_epoch,
                )
            else:
                token = self._lease.renew(
                    self._protocol_lease_token,
                    operation_id=f"protocol:{requested_epoch}",
                    generation=requested_epoch,
                )
            if token is None:
                return False
            self._execution_epoch = requested_epoch
            self._protocol_lease_token = token
        return True

    def release_protocol_lease(
        self,
        execution_epoch: int,
        *,
        next_execution_epoch: int | None = None,
    ) -> bool:
        with self._condition:
            requested_epoch = int(execution_epoch)
            token = self._protocol_lease_token
            if token is None:
                return False
            safe_epoch = (
                None
                if self._safe_stop_identity is None
                else self._safe_stop_identity.execution_epoch
            )
            if token.generation != requested_epoch or self._execution_epoch not in {
                requested_epoch,
                safe_epoch,
            }:
                return False
            next_epoch = (
                requested_epoch
                if next_execution_epoch is None
                else int(next_execution_epoch)
            )
            if next_epoch < int(self._execution_epoch or requested_epoch):
                return False
            if not self._lease.release(token):
                return False
            self._execution_epoch = next_epoch
            self._protocol_lease_token = None
            if token == self._safe_stop_lease_token:
                self._safe_stop_identity = None
                self._safe_stop_lease_token = None
            return True

    def acquire_maintenance_lease(
        self,
        operation_id: str,
        generation: int,
    ) -> DeviceLeaseToken | None:
        with self._condition:
            if (
                not self._accepting
                or self._safe_stop_identity is not None
                or self._active_command is not None
                or self._queue
            ):
                return None
            return self._lease.acquire(
                DeviceLeaseKind.MAINTENANCE,
                operation_id=operation_id,
                generation=generation,
            )

    def acquire_lease(
        self,
        kind: DeviceLeaseKind,
        *,
        operation_id: str,
        generation: int,
    ) -> DeviceLeaseToken | None:
        with self._condition:
            if (
                not self._accepting
                or self._safe_stop_identity is not None
                or self._active_command is not None
                or self._queue
            ):
                return None
            return self._lease.acquire(
                kind,
                operation_id=operation_id,
                generation=generation,
            )

    def acquire_manual_lease(
        self,
        operation_id: str,
        generation: int,
    ) -> DeviceLeaseToken | None:
        return self.acquire_lease(
            DeviceLeaseKind.MANUAL,
            operation_id=operation_id,
            generation=generation,
        )

    def release_lease(self, token: DeviceLeaseToken) -> bool:
        with self._condition:
            if self._active_command is not None or self._queue:
                return False
            if token.kind == DeviceLeaseKind.MAINTENANCE:
                return False
            released = self._lease.release(token)
            if released:
                self._cancel_real_supply_locked()
            if released and token == self._protocol_lease_token:
                self._protocol_lease_token = None
            return released

    def release_maintenance_lease(
        self,
        token: DeviceLeaseToken,
        evidence: MaintenanceLeaseReleaseEvidence,
    ) -> bool:
        with self._condition:
            if token.kind != DeviceLeaseKind.MAINTENANCE:
                return False
            if not evidence.complete:
                return False
            if self._active_command is not None or self._queue:
                return False
            return self._lease.release(token)

    @property
    def lease_snapshot(self) -> DeviceLeaseToken:
        return self._lease.snapshot

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
                self._lease.snapshot.kind.value,
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
                    result = self._apply_real_supply_receipt_gate(command, result)
                finally:
                    with self._condition:
                        self._active_command = None
                        self._condition.notify_all()
            wrapped = FlowCommandResult(command=command, result=result)
            self.result_ready.emit(wrapped)
            with self._condition:
                waiter = self._shutdown_waiters.get(command.command_id)
                if waiter is not None:
                    waiter[1].append(wrapped)
                    waiter[0].set()
            processed += 1
        return processed

    def zero_for_shutdown(self, timeout_ms: int = 2000) -> bool:
        """Legacy wrapper; new shutdown code uses the correlated safe-stop API."""
        with self._condition:
            self._shutdown_sequence += 1
            identity = SafeStopIdentity(
                operation_id=f"legacy-shutdown-{self._shutdown_sequence}",
                generation=self._shutdown_sequence,
                execution_epoch=int(self._execution_epoch or 0) + 1,
            )
        receipt = self.zero_a_for_safe_stop(identity, timeout_ms)
        return bool(
            receipt is not None
            and receipt.success
            and not receipt.stale
            and self.zero_all_for_safe_stop(identity, timeout_ms)
        )

    def zero_a_for_safe_stop(
        self,
        identity: SafeStopIdentity,
        timeout_ms: int = 2000,
    ) -> AZeroReceipt | None:
        wrapped = self._run_safe_stop_command(
            identity,
            mode="safe_stop_a_zero",
            timeout_ms=timeout_ms,
        )
        if wrapped is None:
            return None
        confirmed_a = wrapped.result.a_setpoint_readback_sccm
        zero_confirmed = bool(
            confirmed_a is not None
            and math.isfinite(float(confirmed_a))
            and abs(float(confirmed_a)) <= 1e-9
        )
        return AZeroReceipt(
            command_id=wrapped.command.command_id,
            identity=SafeStopIdentity(
                wrapped.command.operation_id or "",
                int(wrapped.command.generation or 0),
                wrapped.command.execution_epoch,
            ),
            success=bool(wrapped.result.success and zero_confirmed),
            confirmed_a=(float("nan") if confirmed_a is None else float(confirmed_a)),
            stale=bool(wrapped.stale),
            message=(
                str(wrapped.result.message or "")
                if zero_confirmed
                else "A=0 receipt 缺少或超出零 tolerance 的实际 setpoint readback。"
            ),
            source=wrapped.command.source,
            mode=wrapped.command.mode,
            lease_token=wrapped.command.lease_token,
        )

    def zero_all_for_safe_stop(
        self,
        identity: SafeStopIdentity,
        timeout_ms: int = 2000,
    ) -> bool:
        wrapped = self._run_safe_stop_command(
            identity,
            mode="zero",
            timeout_ms=timeout_ms,
        )
        if wrapped is None:
            return False
        return bool(
            wrapped.result.success
            and not wrapped.stale
            and wrapped.result.zero_confirmed
            and all(
                value is not None
                and math.isfinite(float(value))
                and abs(float(value)) <= 1e-9
                for value in (
                    wrapped.result.a_setpoint_readback_sccm,
                    wrapped.result.b_setpoint_readback_sccm,
                    wrapped.result.c_setpoint_readback_sccm,
                )
            )
        )

    def release_lease_for_safe_stop(
        self,
        identity: SafeStopIdentity,
        maintenance_evidence: MaintenanceLeaseReleaseEvidence | None = None,
    ) -> bool:
        """Release the exact active lease only after the correlated zero barrier."""

        with self._condition:
            if (
                self._safe_stop_identity != identity
                or self._active_command is not None
                or self._queue
            ):
                return False
            token = self._lease.snapshot
            if token != self._safe_stop_lease_token:
                return False
            if token.kind == DeviceLeaseKind.IDLE:
                self._protocol_lease_token = None
                self._safe_stop_identity = None
                self._safe_stop_lease_token = None
                return True
            if token.kind == DeviceLeaseKind.MAINTENANCE and (
                maintenance_evidence is None
                or not maintenance_evidence.complete
            ):
                return False
            if not self._lease.release(token):
                return False
            if token == self._protocol_lease_token:
                self._protocol_lease_token = None
            self._safe_stop_identity = None
            self._safe_stop_lease_token = None
            return True

    def _run_safe_stop_command(
        self,
        identity: SafeStopIdentity,
        *,
        mode: str,
        timeout_ms: int,
    ) -> FlowCommandResult | None:
        cancelled: list[FlowCommand] = []
        with self._condition:
            if not self._accepting:
                return None
            self._shutdown_sequence += 1
            lease = self._lease.snapshot
            if (
                self._execution_epoch is not None
                and identity.execution_epoch < self._execution_epoch
            ):
                return None
            if (
                self._safe_stop_identity is not None
                and self._safe_stop_identity != identity
                and identity.execution_epoch
                <= self._safe_stop_identity.execution_epoch
            ):
                return None
            self._safe_stop_identity = identity
            self._safe_stop_lease_token = lease
            self._cancel_real_supply_locked()
            while self._queue:
                cancelled.append(self._queue.popleft())
            self._execution_epoch = max(
                int(self._execution_epoch or 0),
                identity.execution_epoch,
            )
            command_id = f"flow-safe-stop-{mode}-{self._shutdown_sequence}"
            command = FlowCommand(
                command_id=command_id,
                execution_epoch=identity.execution_epoch,
                sequence=self._shutdown_sequence,
                mode=mode,
                a=0.0,
                b=0.0,
                c=0.0,
                source="safety:safe-stop",
                operation_id=identity.operation_id,
                generation=identity.generation,
                lease_token=(
                    lease.token
                    if lease.kind == DeviceLeaseKind.MAINTENANCE
                    else None
                ),
            )
            event = threading.Event()
            results: list[FlowCommandResult] = []
            self._shutdown_waiters[command_id] = (event, results)
            self._queue.appendleft(command)
            self._condition.notify_all()
        self._emit_cancelled(cancelled, "安全停止已失效旧流量命令。")
        deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
        if not self.isRunning():
            self.process_ready()
        remaining = deadline - time.monotonic()
        completed = remaining > 0 and event.wait(remaining)
        with self._condition:
            self._shutdown_waiters.pop(command_id, None)
        if not completed or not results:
            return None
        return results[0]

    def run(self) -> None:
        with self._condition:
            if not self._accepting:
                return
            self._running = True
        try:
            while self._running:
                now = time.monotonic()
                with self._condition:
                    polling_required = bool(
                        self._airflow_sink is not None
                        or self._commissioning_monitor is not None
                    )
                    poll_due = polling_required and now >= self._next_airflow_poll
                if poll_due:
                    self._poll_airflow()
                    self._next_airflow_poll = now + self._airflow_poll_interval_s
                    continue
                if self.process_ready(max_items=1):
                    continue
                with self._condition:
                    if self._running and not self._queue:
                        timeout = None
                        if (
                            self._airflow_sink is not None
                            or self._commissioning_monitor is not None
                        ):
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
            self._cancel_real_supply_locked()
            self._condition.notify_all()
        self._emit_cancelled(cancelled, "流量 worker 关闭，命令已取消。")
        if self.isRunning():
            stopped = bool(self.wait(max(1, int(timeout_ms))))
            return bool(stopped and not self._serial_resources_in_use())
        hal = getattr(self.service, "hal", None)
        releaser = getattr(hal, "release_serial_resources", None)
        try:
            if releaser is not None:
                releaser()
        except Exception:
            return False
        return not self._serial_resources_in_use()

    def _serial_resources_in_use(self) -> bool:
        hal = getattr(self.service, "hal", None)
        return getattr(hal, "serial_resources_in_use", False) is True

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
            if self._lease.snapshot.kind != DeviceLeaseKind.IDLE:
                return False
            self._safe_stop_identity = None
            self._safe_stop_lease_token = None
        return True

    def _cancel_real_supply_locked(self) -> None:
        """Retire authorization/monitor state while holding ``_condition``."""
        authorization = self._real_supply_authorization
        if authorization is not None:
            self._consumed_real_supply_identity = (
                authorization.operation_id,
                authorization.generation,
            )
        self._real_supply_authorization = None
        self._commissioning_monitor = None
        self._commissioning_audit_next_ns = 0

    def _command_rejection_reason_locked(self, command: FlowCommand) -> str:
        if command.source == "safety:safe-stop":
            identity = self._safe_stop_identity
            if (
                identity is not None
                and command.operation_id == identity.operation_id
                and command.generation == identity.generation
                and command.execution_epoch == identity.execution_epoch
                and command.mode in {"safe_stop_a_zero", "zero"}
            ):
                return ""
            return "安全停止流量命令身份不匹配。"
        if self._safe_stop_identity is not None:
            return "安全停止进行中，业务流量命令已失效。"
        lease = self._lease.snapshot
        maintenance_match = self._lease.matches(
            kind=DeviceLeaseKind.MAINTENANCE,
            operation_id=command.operation_id,
            generation=command.generation,
            token=command.lease_token,
        )
        manual_match = self._lease.matches(
            kind=DeviceLeaseKind.MANUAL,
            operation_id=command.operation_id,
            generation=command.generation,
            token=command.lease_token,
        )
        verification_match = self._lease.matches(
            kind=DeviceLeaseKind.VERIFICATION,
            operation_id=command.operation_id,
            generation=command.generation,
            token=command.lease_token,
        )
        if (
            not maintenance_match
            and not verification_match
            and self._execution_epoch is not None
            and command.execution_epoch != self._execution_epoch
        ):
            return "流量命令 execution epoch 已失效，未写入硬件。"
        if lease.kind == DeviceLeaseKind.PROTOCOL:
            return "协议已持有设备租约，流量命令已取消。"
        if lease.kind == DeviceLeaseKind.MAINTENANCE and not maintenance_match:
            return "maintenance 已持有设备租约，流量命令身份不匹配。"
        if command.source == "manual:experiment" and not manual_match:
            return "manual 流量命令租约身份不匹配。"
        if command.source == "verification" and not verification_match:
            return "verification 流量命令租约身份不匹配。"
        if manual_match:
            if command.source != "manual:experiment":
                return "manual 租约拒绝非手动命令。"
            authorization = self._real_supply_authorization
            if authorization is None:
                if self._consumed_real_supply_identity == (
                    command.operation_id,
                    command.generation,
                ):
                    return "Real supply authorization 已消费，重复命令已拒绝。"
                return ""
            if (
                command.operation_id != authorization.operation_id
                or command.generation != authorization.generation
            ):
                return "Real supply authorization identity 不匹配。"
            if command.mode == "manual_post_close_a_zero":
                return (
                    ""
                    if authorization.policy.targets_match(
                        (0.0, authorization.targets_sccm[1], authorization.targets_sccm[2]),
                        (command.a, command.b, command.c),
                    )
                    else "Real supply 前置 A=0 targets 与冻结授权不一致。"
                )
            if command.mode != "manual_restore_supply":
                return "Real supply 只允许既有 supply-only transaction。"
            if not authorization.policy.targets_match(
                authorization.targets_sccm,
                (command.a, command.b, command.c),
            ):
                return "Real supply controller targets 与冻结授权不一致。"
            return ""
        if verification_match:
            if command.source != "verification":
                return "verification 租约拒绝非验证命令。"
            verification_a_only = bool(
                command.mode == "verification"
                and command.a > 0
                and abs(command.b) <= 1e-9
                and abs(command.c) <= 1e-9
            )
            verification_zero = bool(
                command.mode in {"verification_a_zero", "verification_zero"}
                and all(abs(value) <= 1e-9 for value in (command.a, command.b, command.c))
            )
            return (
                ""
                if verification_a_only or verification_zero
                else "verification 只接受 A-only 非零或 A/B/C 全零命令。"
            )
        if lease.kind not in {DeviceLeaseKind.IDLE, DeviceLeaseKind.MAINTENANCE}:
            return f"{lease.kind.value} 已持有设备租约，流量命令已取消。"
        if lease.kind == DeviceLeaseKind.IDLE and command.lease_token:
            return "流量命令携带的设备租约已失效。"
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
        if command.mode == "safe_stop_a_zero":
            return self.service.apply_a_zero()
        if command.mode == "zero":
            return self.service.apply_zero()
        if command.mode == "verification_a_zero":
            return self.service.apply_a_zero()
        if command.mode == "verification_zero":
            return self.service.apply_zero()
        return self.service.apply_flows(
            a_target=command.a,
            b_target=command.b,
            c_target=command.c,
            mode=command.mode,
        )

    def _apply_real_supply_receipt_gate(
        self,
        command: FlowCommand,
        result: FlowApplyResult,
    ) -> FlowApplyResult:
        identity = (command.operation_id, command.generation)
        with self._condition:
            authorization = self._real_supply_authorization
            if authorization is None:
                if (
                    command.mode in {"manual_post_close_a_zero", "manual_restore_supply"}
                    and self._consumed_real_supply_identity == identity
                ):
                    return self._real_supply_gate_failure(
                        result,
                        "Real supply authorization 已消费或取消，命令回执已拒绝。",
                        "real_supply_authorization_consumed",
                    )
                return result
            if identity != (authorization.operation_id, authorization.generation):
                return result

            readbacks = (
                result.a_setpoint_readback_sccm,
                result.b_setpoint_readback_sccm,
                result.c_setpoint_readback_sccm,
            )
            if command.mode == "manual_post_close_a_zero":
                if not result.success:
                    return result
                expected = (
                    0.0,
                    authorization.targets_sccm[1],
                    authorization.targets_sccm[2],
                )
                if not self._accepted_readbacks_match(
                    authorization.policy,
                    expected,
                    readbacks,
                ):
                    return self._real_supply_gate_failure(
                        result,
                        "selector compensation 前 A/B/C accepted/readback 超出 commissioning tolerance。",
                        "pre_close_readback_mismatch",
                    )
                return result
            if command.mode != "manual_restore_supply":
                return result

            # Claim this one-shot identity before examining the receipt.  The
            # condition lock makes claim, consumption, and monitor install
            # indivisible with cancellation and SafeStop.
            self._real_supply_authorization = None
            self._consumed_real_supply_identity = (
                authorization.operation_id,
                authorization.generation,
            )
            if not result.success:
                return result
            if not self._accepted_readbacks_match(
                authorization.policy,
                authorization.targets_sccm,
                readbacks,
            ):
                return self._real_supply_gate_failure(
                    result,
                    "accepted/readback 超出冻结的 1.0 sccm tolerance，必须执行 SafeStop。",
                    "accepted_readback_mismatch",
                )
            first_tx_ns = result.first_nonzero_tx_monotonic_ns
            if first_tx_ns is None:
                return self._real_supply_gate_failure(
                    result,
                    "缺少首个非零 TX 单调时戳，不能启动 settling/hold 门禁。",
                    "missing_nonzero_tx_timestamp",
                )
            self._commissioning_monitor = FlowSettlingMonitor(
                policy=authorization.policy,
                operation_id=authorization.operation_id,
                targets_sccm=authorization.targets_sccm,
                first_nonzero_tx_monotonic_ns=int(first_tx_ns),
            )
            self._commissioning_audit_next_ns = 0
            self._next_airflow_poll = 0.0
            self._condition.notify_all()
            return result

    @staticmethod
    def _accepted_readbacks_match(
        policy: RealSupplyPolicy,
        expected: tuple[float, float, float],
        readbacks: tuple[float | None, float | None, float | None],
    ) -> bool:
        return bool(
            all(value is not None and math.isfinite(float(value)) for value in readbacks)
            and policy.accepted_readbacks_match(
                expected,
                tuple(float(value) for value in readbacks),  # type: ignore[arg-type]
            )
        )

    @staticmethod
    def _real_supply_gate_failure(
        result: FlowApplyResult,
        message: str,
        error: str,
    ) -> FlowApplyResult:
        return FlowApplyResult(
            False,
            message,
            result.a,
            result.b,
            result.c,
            result.a_comp,
            error,
            a_setpoint_readback_sccm=result.a_setpoint_readback_sccm,
            b_setpoint_readback_sccm=result.b_setpoint_readback_sccm,
            c_setpoint_readback_sccm=result.c_setpoint_readback_sccm,
            first_nonzero_tx_monotonic_ns=result.first_nonzero_tx_monotonic_ns,
            recovery_required=True,
        )

    def _poll_airflow(self) -> None:
        with self._condition:
            sink = self._airflow_sink
            monitor_required = self._commissioning_monitor is not None
        if sink is None and not monitor_required:
            return
        timestamp = time.time()
        try:
            snapshot_reader = getattr(self.service.hal, "read_flow_snapshot", None)
            if callable(snapshot_reader):
                snapshot = snapshot_reader()
                if not isinstance(snapshot, FlowReadbackSnapshot):
                    raise TypeError("HAL returned an invalid flow snapshot")
                value = float(snapshot.by_channel["A"].mass_flow_sccm)
                timestamp = float(snapshot.wall_timestamp)
                evaluated_ns = time.perf_counter_ns()
                self._evaluate_commissioning(snapshot, now_ns=evaluated_ns)
                self.flow_snapshot_ready.emit(snapshot)
            else:
                if monitor_required:
                    raise TypeError("HAL does not provide three-channel flow snapshots")
                value = float(self.service.hal.read_flow())
        except Exception as exc:  # serial errors must invalidate readiness immediately
            if sink is not None:
                sink(float("nan"), timestamp, f"{type(exc).__name__}: {exc}")
            with self._condition:
                monitor = self._commissioning_monitor
                result = (
                    None
                    if monitor is None
                    else monitor.fail(
                        f"三路 Poll 失败或 serial desync：{type(exc).__name__}: {exc}",
                        now_ns=time.perf_counter_ns(),
                    )
                )
                if result is not None and self._commissioning_monitor is monitor:
                    self._commissioning_monitor = None
                    self._commissioning_audit_next_ns = 0
            if result is not None:
                self.commissioning_result_ready.emit(result)
            return
        if sink is not None:
            sink(value, timestamp, None)

    def _evaluate_commissioning(
        self,
        snapshot: FlowReadbackSnapshot,
        *,
        now_ns: int | None = None,
    ) -> None:
        evaluated_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self._condition:
            monitor = self._commissioning_monitor
            if monitor is None:
                return
            self._audit_commissioning_sample_locked(monitor, snapshot, evaluated_ns)
            result = monitor.evaluate(snapshot, now_ns=evaluated_ns)
            if (
                result is not None
                and result.status is CommissioningMonitorStatus.FAILED
                and self._commissioning_monitor is monitor
            ):
                self._commissioning_monitor = None
                self._commissioning_audit_next_ns = 0
        if result is None:
            return
        LOG.info(
            "real_supply_commissioning | operation=%s | status=%s | reason=%s | samples=%s",
            result.operation_id,
            result.status.value,
            result.reason,
            result.consecutive_samples,
        )
        self.commissioning_result_ready.emit(result)

    def _audit_commissioning_sample_locked(
        self,
        monitor: FlowSettlingMonitor,
        snapshot: FlowReadbackSnapshot,
        evaluated_ns: int,
    ) -> None:
        if evaluated_ns < self._commissioning_audit_next_ns:
            return
        self._commissioning_audit_next_ns = evaluated_ns + 1_000_000_000
        LOG.info(
            "real_supply_commissioning_sample | %s",
            {
                "operation_id": monitor.operation_id,
                "snapshot_monotonic_ns": snapshot.monotonic_ns,
                "evaluated_monotonic_ns": evaluated_ns,
                "channels": {
                    reading.channel: {
                        "raw_frame": str(reading.raw_frame)[:256],
                        "setpoint_sccm": reading.setpoint_sccm,
                        "mass_flow_sccm": reading.mass_flow_sccm,
                        "gas": reading.gas,
                        "monotonic_ns": reading.monotonic_ns,
                    }
                    for reading in snapshot.readings
                },
            },
        )
