from __future__ import annotations

import heapq
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    AZeroReceipt,
    CleaningOutcome,
    CleaningPlan,
    CleaningResult,
    CleaningSnapshot,
    CleaningStatus,
    DeviceLeaseKind,
    DeviceLeaseToken,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolGateEvent,
    SafeStopIdentity,
    SafeStopPlan,
    SelectorReceipt,
    SelectorRoute,
    normalize_digital_target,
)
from app.services.actuation_metrics import ActuationMetrics
from app.services.flow_service import FlowApplyResult
from app.services.gating_service import GatingService
from app.services.safety_manager import SafetyManager
from app.workers.flow_worker import FlowCommand, FlowCommandResult

if False:  # pragma: no cover - typing-only without import cycles
    from app.services.protocol_executor import ProtocolExecutor


@dataclass(frozen=True, slots=True)
class InterlockSnapshot:
    connected: bool = False
    hardware_ready: bool = False
    flow_setpoints_ready: bool = False
    safety_state: str = "UNKNOWN"
    ttl_input_ready: bool = False
    has_protocol: bool = False
    device_lease: str = "idle"
    recording_ready: bool = False
    recorder_failed: bool = False
    recorder_generation: int = 0
    session_closing: bool = False

    def unsafe_reason(self) -> str:
        if not self.connected:
            return "硬件连接已断开，已取消阀门动作。"
        if not self.hardware_ready:
            return "硬件自检状态已失效，已取消阀门动作。"
        if not self.flow_setpoints_ready:
            return "MFC 流量 readiness 已失效，已取消阀门动作。"
        if self.safety_state != "SAFE":
            return f"安全状态为 {self.safety_state}，已取消阀门动作。"
        return ""

    def rejection_reason(self, *, require_ttl: bool = False) -> str:
        unsafe = self.unsafe_reason()
        if unsafe:
            return unsafe
        if not self.has_protocol:
            return "请先加载有效协议。"
        if require_ttl and not self.ttl_input_ready:
            return "TTL 输入 AI6 readiness 已失效，已取消阀门动作。"
        if self.device_lease != "protocol":
            return "协议未持有设备租约，已取消阀门动作。"
        return ""

    def command_rejection_reason(self, command: ActuationCommand) -> str:
        if command.category == ActuationCategory.SAFETY:
            return "" if command.action == ActuationAction.CLOSE else "安全命令只能关闭输出。"
        if (
            (
                command.category == ActuationCategory.NORMAL
                or (
                    command.category == ActuationCategory.WARMUP
                    and command.action == ActuationAction.OPEN
                )
                or command.category == ActuationCategory.CLEANING
            )
            and not self.recording_ready
        ):
            return "记录器尚未进入就绪状态，已拒绝非安全动作。"
        if self.recorder_failed:
            return (
                "会话记录器已失败，已拒绝非安全动作；"
                "请执行安全停止并成功建立新会话。"
            )
        if self.session_closing:
            return "会话正在关闭，已拒绝非安全动作。"
        unsafe = self.unsafe_reason()
        if unsafe and command.action == ActuationAction.OPEN:
            return unsafe
        if command.category == ActuationCategory.NORMAL:
            return self.rejection_reason()
        if command.category == ActuationCategory.CLEANING:
            if self.device_lease != "maintenance":
                return "maintenance 未持有设备租约，已取消清洗动作。"
            return ""
        if self.device_lease == "maintenance":
            return "maintenance 设备租约已占用，已拒绝其他动作。"
        if command.category in {ActuationCategory.MANUAL, ActuationCategory.PRETEST} and self.device_lease == "protocol":
            return "协议运行中设备租约已占用，已拒绝手动或预检输出。"
        return ""


class ActuationInterlockIngress:
    """Producer-safe immutable readiness ingress; protocol state remains elsewhere."""

    def __init__(
        self,
        initial: InterlockSnapshot | None = None,
        *,
        safety_manager: SafetyManager | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._generation = 1
        self._snapshot = initial or InterlockSnapshot()
        self._unsafe_latched = bool(self._snapshot.unsafe_reason())
        self._safety_manager = safety_manager or SafetyManager()

    def publish(self, snapshot: InterlockSnapshot) -> int:
        with self._lock:
            if snapshot != self._snapshot:
                self._snapshot = snapshot
                self._generation += 1
            if snapshot.unsafe_reason():
                self._unsafe_latched = True
            return self._generation

    def update(self, **changes) -> int:
        with self._lock:
            candidate = replace(self._snapshot, **changes)
            if candidate != self._snapshot:
                self._snapshot = candidate
                self._generation += 1
            if candidate.unsafe_reason():
                self._unsafe_latched = True
            return self._generation

    def publish_raw_telemetry(
        self,
        *,
        airflow: float,
        timestamp: float,
        hardware_state: str | None,
        connected: bool,
        hardware_ready: bool,
        flow_setpoints_ready: bool,
        ttl_input_ready: bool,
        has_protocol: bool,
        device_lease: str,
    ) -> int:
        with self._lock:
            safety_state = self._safety_manager.evaluate(
                airflow,
                timestamp=timestamp,
                previous_state=self._snapshot.safety_state,
                hardware_state=hardware_state,
            )
            candidate = replace(
                self._snapshot,
                connected=bool(connected),
                hardware_ready=bool(hardware_ready),
                # These fields are owned by flow/protocol producers.  Preserve
                # their latest values even if the telemetry caller read an
                # older snapshot before entering this atomic publication.
                flow_setpoints_ready=self._snapshot.flow_setpoints_ready,
                safety_state=safety_state,
                ttl_input_ready=bool(ttl_input_ready),
                has_protocol=self._snapshot.has_protocol,
                device_lease=self._snapshot.device_lease,
            )
            if candidate != self._snapshot:
                self._snapshot = candidate
                self._generation += 1
            if candidate.unsafe_reason():
                self._unsafe_latched = True
            return self._generation

    def publish_airflow(
        self,
        *,
        airflow: float,
        timestamp: float,
        hardware_state: str | None = None,
    ) -> int:
        """Atomically update only airflow-derived safety, preserving other owners."""
        with self._lock:
            safety_state = self._safety_manager.evaluate(
                airflow,
                timestamp=timestamp,
                previous_state=self._snapshot.safety_state,
                hardware_state=hardware_state,
            )
            candidate = replace(self._snapshot, safety_state=safety_state)
            if candidate != self._snapshot:
                self._snapshot = candidate
                self._generation += 1
            if candidate.unsafe_reason():
                self._unsafe_latched = True
            return self._generation

    def read(self) -> tuple[int, InterlockSnapshot, bool]:
        with self._lock:
            return self._generation, self._snapshot, self._unsafe_latched

    def clear_unsafe_latch(self) -> bool:
        with self._lock:
            if self._snapshot.unsafe_reason():
                return False
            self._unsafe_latched = False
            return True

    def clear_recorder_failure(self, generation: int) -> bool:
        with self._lock:
            if (
                not self._snapshot.recording_ready
                or self._snapshot.recorder_generation != int(generation)
            ):
                return False
            if self._snapshot.unsafe_reason():
                return False
            self._snapshot = replace(self._snapshot, recorder_failed=False)
            self._generation += 1
            return True


Writer = Callable[[ActuationCommand], ActuationReceipt]


@dataclass(frozen=True, slots=True)
class ProtocolStartAck:
    accepted: bool
    lease_epoch: int
    previous_epoch: int
    execution_epoch: int
    status: ProtocolExecutionStatus
    message: str = ""


class ActuationWorker(QThread):
    """Single owner for protocol actuation state, quality metrics and DO scheduling."""

    _NORMAL_DEADLINE_RESERVE_NS = 5_000_000

    receipt_ready = Signal(object)
    snapshot_ready = Signal(object)
    status_message = Signal(str)
    executor_result_ready = Signal(object)
    start_result_ready = Signal(object)
    plan_result_ready = Signal(object)
    flow_result_ready = Signal(object)
    document_result_ready = Signal(object)
    ttl_arm_requested = Signal(int)
    ttl_disarm_requested = Signal()
    cleaning_snapshot_ready = Signal(object)
    cleaning_result_ready = Signal(object)
    protocol_safe_stop_handoff_requested = Signal(object)

    def __init__(
        self,
        *,
        protocol_state: ProtocolExecutionState | None = None,
        protocol_executor: ProtocolExecutor | None = None,
        gating_service: GatingService | None = None,
        valve_service=None,
        writer: Writer,
        interlock: ActuationInterlockIngress,
        metrics: ActuationMetrics | None = None,
        monotonic_ns_clock: Callable[[], int] | None = None,
        wall_clock: Callable[[], float] | None = None,
        normal_queue_capacity: int = 256,
        sample_transform: Callable[[float], float] | None = None,
        flow_submitter: Callable[[FlowCommand], bool] | None = None,
        cleaning_flow_ready_timeout_ms: int = 5000,
        safe_stop_receipt_timeout_ms: int = 2000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if protocol_executor is None and protocol_state is None:
            raise ValueError("ActuationWorker 需要唯一 ProtocolExecutor 或 ProtocolExecutionState。")
        if protocol_executor is not None and protocol_state is not None:
            if protocol_executor.state is not protocol_state:
                raise ValueError("ProtocolExecutor 与 protocol_state 必须引用同一状态。")
        self.protocol_executor = protocol_executor
        self.protocol_state = protocol_executor.state if protocol_executor is not None else protocol_state
        assert self.protocol_state is not None
        self.gating_service = (
            protocol_executor.gating_service
            if protocol_executor is not None
            else gating_service
        )
        self.valve_service = valve_service
        self.writer = writer
        self.interlock = interlock
        self.metrics = metrics or ActuationMetrics()
        self._clock_ns = monotonic_ns_clock or time.perf_counter_ns
        self._wall_clock = wall_clock or time.time
        self._normal_capacity = max(1, int(normal_queue_capacity))
        self._sample_transform = sample_transform or float
        self._flow_submitter = flow_submitter
        self._cleaning_flow_ready_timeout_ns = (
            max(1, int(cleaning_flow_ready_timeout_ms)) * 1_000_000
        )
        self._safe_stop_receipt_timeout_ns = (
            max(1, int(safe_stop_receipt_timeout_ms)) * 1_000_000
        )
        self._condition = threading.Condition(threading.RLock())
        self._normal_heap: list[tuple[int, int, int, ActuationCommand]] = []
        self._messages: deque[tuple[str, dict[str, Any]]] = deque()
        self._deadline_heap: list[tuple[int, int, int, str, dict[str, Any]]] = []
        self._emergency: deque[ActuationCommand] = deque()
        self._sequence = 1_000_000
        self._running = False
        self._accepting = True
        self._pending_ttl_arm_epoch: int | None = None
        self._ttl_hardware_armed_epoch: int | None = None
        self._seen_receipts: dict[str, ActuationReceipt] = {}
        self._seen_receipt_order: deque[tuple[str, int]] = deque()
        self._receipt_history_limit = max(1024, self._normal_capacity * 8)
        self._commands_by_id: dict[str, ActuationCommand] = {}
        self._plan_contexts: dict[str, dict[str, Any]] = {}
        self._plan_by_command: dict[str, str] = {}
        self._shutdown_close_pending: set[str] = set()
        self._shutdown_close_failed = False
        self._shutdown_close_started = False
        self._safe_stop_identity: SafeStopIdentity | None = None
        self._safe_stop_plan: SafeStopPlan | None = None
        self._safe_stop_selector_waiters: dict[
            str,
            tuple[threading.Event, list[ActuationReceipt]],
        ] = {}
        self._safe_stop_selector_deadlines: dict[str, int] = {}
        self._background_safe_stop_generation = 0
        self._background_safe_stop_plan: SafeStopPlan | None = None
        self._background_safe_stop_flow_id: str | None = None
        self._background_safe_stop_flow_command: FlowCommand | None = None
        self._background_safe_stop_final_flow_id: str | None = None
        self._background_safe_stop_final_flow_command: FlowCommand | None = None
        self._background_safe_stop_flow_deadlines: dict[str, int] = {}
        self._background_safe_stop_selector_id: str | None = None
        self._background_safe_stop_close_pending: set[str] = set()
        self._do_handed_off = True
        self._pending_safe_transition: tuple[str, dict[str, Any]] | None = None
        self._safe_transition_close_pending: set[str] = set()
        self._safe_transition_handoff_identity: SafeStopIdentity | None = None
        self._unsafe_close_generation: int | None = None
        self._session_recorder = None
        self._session_recorder_sequence = 0
        self._session_recorder_generation = 0
        self._recorder_failure_notified = False
        self._cleaning_snapshot = CleaningSnapshot()
        self._cleaning_plan: CleaningPlan | None = None
        self._cleaning_phase = "idle"
        self._cleaning_step_index = 0
        self._cleaning_expected: dict[str, dict[str, Any]] = {}
        self._cleaning_receipts: dict[str, ActuationReceipt] = {}
        self._cleaning_possibly_open: set[str] = set()
        self._cleaning_lease_token: DeviceLeaseToken | None = None
        self._maintenance_recorder = None
        self._queued_maintenance_recorders: list[object] = []
        self._maintenance_recorder_sequence = 0
        self._cleaning_pending_flow_id: str | None = None
        self._cleaning_pending_flow_command: FlowCommand | None = None
        self._cleaning_stop_outcome = CleaningOutcome.FAILED
        self._cleaning_terminal_status = CleaningStatus.FAILED
        self._cleaning_failure_reason = ""

    def set_session_recorder(self, recorder) -> bool:
        return self.bind_session_recorder(recorder, generation=0)

    @property
    def cleaning_snapshot(self) -> CleaningSnapshot:
        with self._condition:
            return self._cleaning_snapshot

    @property
    def cleaning_owner_handoff_ready(self) -> bool:
        with self._condition:
            return bool(
                self._cleaning_snapshot.status
                in {
                    CleaningStatus.COMPLETED,
                    CleaningStatus.FAILED,
                    CleaningStatus.RECOVERY_REQUIRED,
                }
                and not self._cleaning_expected
                and self._cleaning_pending_flow_id is None
                and not self._cleaning_possibly_open
                and self._cleaning_snapshot.flow_zero_confirmed
                and self._cleaning_snapshot.selector_safe_confirmed
            )

    def post_cleaning_start(
        self,
        plan: CleaningPlan,
        *,
        lease_token: DeviceLeaseToken,
        recorder,
    ) -> bool:
        with self._condition:
            if not self._accepting or self._cleaning_snapshot.status not in {
                CleaningStatus.IDLE,
                CleaningStatus.COMPLETED,
                CleaningStatus.FAILED,
                CleaningStatus.RECOVERY_REQUIRED,
            }:
                return False
            self._messages.append(
                (
                    "cleaning_start",
                    {
                        "plan": plan,
                        "lease_token": lease_token,
                        "recorder": recorder,
                    },
                )
            )
            self._condition.notify_all()
        return True

    def post_cleaning_stop(self, *, reason: str, aborted: bool = True) -> bool:
        with self._condition:
            if self._cleaning_snapshot.status not in {
                CleaningStatus.PREPARING,
                CleaningStatus.RUNNING,
            }:
                return False
            self._messages.appendleft(
                (
                    "cleaning_stop",
                    {"reason": str(reason), "aborted": bool(aborted)},
                )
            )
            self._condition.notify_all()
        return True

    def post_cleaning_recover(self) -> bool:
        with self._condition:
            if self._cleaning_snapshot.status not in {
                CleaningStatus.FAILED,
                CleaningStatus.RECOVERY_REQUIRED,
            }:
                return False
            self._messages.appendleft(("cleaning_recover", {}))
            self._condition.notify_all()
        return True

    def finalize_maintenance_recorder(self) -> bool:
        recorder = self._maintenance_recorder
        if (
            recorder is None
            or self._cleaning_snapshot.status
            not in {
                CleaningStatus.COMPLETED,
                CleaningStatus.FAILED,
                CleaningStatus.RECOVERY_REQUIRED,
            }
        ):
            return False
        accepted = bool(
            recorder.post_fence(
                "actuation",
                producer_sequence=self._maintenance_recorder_sequence,
                final_payload={
                    "cleaning_snapshot": self._cleaning_snapshot,
                },
            )
        )
        if accepted:
            self._maintenance_recorder = None
        return accepted

    def handoff_maintenance_for_safe_stop(self) -> bool:
        """Fence an active maintenance producer before DO owner shutdown."""

        handed_off = True
        while self._queued_maintenance_recorders:
            recorder = self._queued_maintenance_recorders.pop(0)
            try:
                handed_off = bool(
                    recorder.post_fence(
                        "actuation",
                        producer_sequence=0,
                        final_payload={"cleaning_snapshot": self._cleaning_snapshot},
                    )
                ) and handed_off
            except Exception:
                handed_off = False
        if self._maintenance_recorder is not None:
            handed_off = self.finalize_maintenance_recorder() and handed_off
        return handed_off

    def complete_global_safe_stop_handoff(self) -> bool:
        """Clear maintenance admission state only after global owners handed off."""

        if self.isRunning() or not self._do_handed_off:
            return False
        with self._condition:
            self._cleaning_plan = None
            self._cleaning_lease_token = None
            self._cleaning_expected.clear()
            self._cleaning_pending_flow_id = None
            self._cleaning_pending_flow_command = None
            self._cleaning_possibly_open.clear()
            self._cleaning_receipts.clear()
            self._maintenance_recorder = None
            self._queued_maintenance_recorders.clear()
            self._cleaning_phase = "terminal"
            self._cleaning_snapshot = replace(
                self._cleaning_snapshot,
                lease_held=False,
                recording_ready=False,
            )
        return True

    def bind_session_recorder(
        self,
        recorder,
        *,
        generation: int,
        timeout_ms: int = 1000,
    ) -> bool:
        ack = threading.Event()
        cancelled = threading.Event()
        result: dict[str, bool] = {}
        payload = {
            "recorder": recorder,
            "generation": int(generation),
            "ack": ack,
            "cancelled": cancelled,
            "result": result,
        }
        self._post_message("recorder_bind", payload)
        if not self.isRunning():
            self.process_ready()
        if not ack.wait(max(1, int(timeout_ms)) / 1000.0):
            with self._condition:
                if not ack.is_set():
                    cancelled.set()
                    for index, (kind, queued_payload) in enumerate(self._messages):
                        if kind == "recorder_bind" and queued_payload is payload:
                            del self._messages[index]
                            break
                    result["accepted"] = False
                    ack.set()
        return bool(result.get("accepted"))

    def post_recorder_failed(self, message: str) -> None:
        with self._condition:
            if self._recorder_failure_notified:
                return
            self._recorder_failure_notified = True
        self._post_message("recorder_failed", {"message": str(message)})

    def post_recorder_ready(
        self,
        generation: int,
        *,
        wait: bool = False,
        timeout_ms: int = 1000,
    ) -> bool:
        ack = threading.Event() if wait else None
        result: dict[str, bool] = {}
        self._post_message(
            "recorder_ready",
            {
                "generation": int(generation),
                "ack": ack,
                "result": result,
            },
        )
        if not self.isRunning():
            self.process_ready()
        if ack is None:
            return True
        if not ack.wait(max(1, int(timeout_ms)) / 1000.0):
            return False
        return bool(result.get("accepted"))

    def post_recorder_fence(
        self,
        *,
        wait: bool = False,
        timeout_ms: int = 1000,
    ) -> bool:
        ack = threading.Event() if wait else None
        result: dict[str, bool] = {}
        self._post_message("recorder_fence", {"ack": ack, "result": result})
        if not self.isRunning() and self._writer_hal() is None:
            self.process_ready()
        if ack is None:
            return True
        if not ack.wait(max(1, int(timeout_ms)) / 1000.0):
            return False
        return bool(result.get("accepted"))

    def _emit_receipt(self, receipt: ActuationReceipt) -> None:
        recorder = self._session_recorder
        if recorder is not None:
            self._session_recorder_sequence += 1
            if not recorder.post_receipt(
                receipt,
                producer_sequence=self._session_recorder_sequence,
            ):
                self.post_recorder_failed(
                    "动作回执无法进入会话记录队列，已请求安全阻断。"
                )
        self.receipt_ready.emit(receipt)

    def _emit_executor_result(self, result) -> None:
        recorder = self._session_recorder
        if recorder is not None:
            for event in result.events:
                self._session_recorder_sequence += 1
                if event.event in {
                    "actuation_receipt",
                    "quality_acknowledged",
                    "quality_ack_rejected",
                }:
                    accepted = recorder.post_quality_event(
                        event=(
                            "actuation_quality"
                            if event.event == "actuation_receipt"
                            else event.event
                        ),
                        snapshot=self.protocol_state.quality,
                        producer_sequence=self._session_recorder_sequence,
                        command_id=event.command_id,
                        message=event.message,
                        timestamp=event.timestamp,
                        monotonic_ns=(
                            event.monotonic_ns
                            if event.monotonic_ns is not None
                            else event.actual_ns
                        ),
                        transitions=tuple(
                            {
                                "stream": stream,
                                "direction": direction,
                                "p95_ms": p95_ms,
                            }
                            for stream, direction, p95_ms in event.quality_transitions
                        ),
                    )
                else:
                    accepted = recorder.post_protocol_event(
                        event,
                        producer_sequence=self._session_recorder_sequence,
                    )
                if not accepted:
                    self.post_recorder_failed(
                        "协议/质量事件无法进入会话记录队列，已请求安全阻断。"
                    )
                    break
        self.executor_result_ready.emit(result)

    def _emit_recorder_fence(self) -> bool:
        recorder = self._session_recorder
        if recorder is None:
            return False
        final_quality = self.metrics.snapshot()
        accepted = bool(
            recorder.post_fence(
                "actuation",
                producer_sequence=self._session_recorder_sequence,
                final_payload={"quality": final_quality},
            )
        )
        if accepted:
            self._session_recorder = None
            self._session_recorder_generation = 0
        return accepted

    def finalize_recorder_after_owner_stopped(self) -> bool:
        if self.isRunning() or not self._do_handed_off:
            return False
        return self._emit_recorder_fence()

    @property
    def emergency_queue_size(self) -> int:
        with self._condition:
            return len(self._emergency)

    @property
    def normal_queue_size(self) -> int:
        with self._condition:
            return len(self._normal_heap)

    def submit(self, command: ActuationCommand) -> bool:
        """Owner-side enqueue. External producers should post intents, not mutate state."""
        with self._condition:
            if command.valve == 0 and not (
                self._is_authorized_cleaning_selector_command(command)
                or self._is_authorized_selector_business_command(command)
            ):
                return False
            if not self._accepting and command.category != ActuationCategory.SAFETY:
                if (
                    command.category == ActuationCategory.NORMAL
                    and command.action == ActuationAction.CLOSE
                ):
                    self.protocol_state.possibly_open_valves.add(command.valve)
                    self._clear_cancelled_pending(command)
                    self.submit_emergency_close(
                        command.valve,
                        reason="普通关闭在停止接单后被拒绝，已升级为紧急关闭。",
                    )
                return False
            if command.category == ActuationCategory.SAFETY:
                self._commands_by_id[command.command_id] = command
                self._enqueue_emergency_locked(command)
                self._condition.notify_all()
                return True
            if len(self._normal_heap) >= self._normal_capacity:
                reason = "动作队列已满，已取消普通动作并阻断协议；请停止并检查系统负载。"
                if command.category == ActuationCategory.NORMAL:
                    if command.action == ActuationAction.CLOSE:
                        self.protocol_state.possibly_open_valves.add(command.valve)
                    self.invalidate_execution(reason=reason)
                    self._emit_receipt(
                        ActuationReceipt.from_write(
                            command=command,
                            started_ns=None,
                            actual_ns=None,
                            wall_timestamp=self._wall_clock(),
                            result=ActuationResult.CANCELLED,
                            message=reason,
                            stale=True,
                        )
                    )
                else:
                    self._block(reason)
                return False
            if command.category == ActuationCategory.NORMAL:
                if command.action == ActuationAction.OPEN:
                    if self.protocol_state.pending_open_command_id not in {
                        None,
                        command.command_id,
                    }:
                        return False
                    self.protocol_state.pending_open_command_id = command.command_id
                elif command.action == ActuationAction.CLOSE:
                    if self.protocol_state.pending_close_command_id not in {
                        None,
                        command.command_id,
                    }:
                        return False
                    self.protocol_state.pending_close_command_id = command.command_id
            self._commands_by_id[command.command_id] = command
            heapq.heappush(
                self._normal_heap,
                (command.expected_ns, 10, command.sequence, command),
            )
            self._condition.notify_all()
            return True

    def _is_authorized_cleaning_selector_command(
        self,
        command: ActuationCommand,
    ) -> bool:
        plan = self._cleaning_plan
        expected = self._cleaning_expected.get(command.command_id)
        selector = (
            None
            if self.valve_service is None
            else getattr(self.valve_service, "selector", None)
        )
        selector_target = (
            getattr(selector, "target", None)
            or (
                None
                if self.valve_service is None
                else getattr(
                    self.valve_service,
                    "selector_target",
                    getattr(self.valve_service, "master_valve_line", None),
                )
            )
        )
        return bool(
            plan is not None
            and expected is not None
            and expected.get("role") == "selector_safe"
            and expected.get("command") == command
            and selector_target
            and command.target == selector_target
            and command.operation_id == plan.identity.operation_id
            and command.generation == plan.identity.generation
            and command.step_id == "selector_safe"
            and command.action_kind == command.action
        )

    def _is_authorized_selector_business_command(
        self,
        command: ActuationCommand,
    ) -> bool:
        """Permit only the configured selector's odor route outside safe-stop plans."""

        if self.valve_service is None:
            return False
        if command.category not in {
            ActuationCategory.MASTER,
            ActuationCategory.CLEANING,
            ActuationCategory.WARMUP,
            ActuationCategory.MANUAL,
            ActuationCategory.PRETEST,
        }:
            return False
        if command.category == ActuationCategory.CLEANING:
            expected = self._cleaning_expected.get(command.command_id)
            if (
                expected is None
                or expected.get("role") != "selector_odor"
                or expected.get("command") != command
            ):
                return False
        elif command.command_id not in self._plan_by_command:
            return False
        try:
            step = self.valve_service.selector_route_step(SelectorRoute.ODOR)
        except ValueError:
            return False
        expected_action = ActuationAction.OPEN if step.state else ActuationAction.CLOSE
        return bool(
            command.target
            and normalize_digital_target(command.target)
            == normalize_digital_target(f"{step.device}/{step.line}")
            and command.action == expected_action
        )

    def post_start(self, *, document, readiness, lease_epoch: int | None = None) -> None:
        self._post_message(
            "start",
            {
                "document": document,
                "readiness": readiness,
                "lease_epoch": lease_epoch,
            },
        )

    def post_load(self, document) -> None:
        self._post_message("load", {"document": document})

    def post_manual_trigger(self, *, readiness) -> None:
        self._post_message("manual_trigger", {"readiness": readiness})

    def post_ttl_pulse(self, pulse, *, readiness=None) -> None:
        self._post_message("ttl_pulse", {"pulse": pulse, "readiness": readiness})

    def post_ai_batch(self, batch, *, readiness=None) -> None:
        self._post_message("ai_batch", {"batch": batch, "readiness": readiness})

    def post_input_error(self, message: str) -> None:
        self._post_message("input_error", {"message": str(message)})

    def post_stop(self, *, message: str = "用户已停止协议执行。") -> None:
        self._post_message("stop", {"message": message})

    def post_pause(self) -> None:
        self._post_message("pause", {})

    def post_resume(self) -> None:
        self._post_message("resume", {})

    def post_rearm(self) -> None:
        self._post_message("rearm", {})

    def post_mode(self, mode: str) -> None:
        self._post_message("mode", {"mode": mode})

    def post_skip(self) -> None:
        self._post_message("skip", {})

    def post_readiness_update(self, *, readiness, timestamp: float | None = None) -> None:
        self._post_message(
            "readiness",
            {"readiness": readiness, "timestamp": timestamp},
        )

    def post_interlock_changed(self, *, timestamp: float | None = None) -> None:
        """Wake the owner after a producer published raw interlock state."""
        self._post_message("interlock_changed", {"timestamp": timestamp})

    def post_gating_thresholds(
        self,
        *,
        inhale_threshold: float,
        exhale_threshold: float,
    ) -> None:
        self._post_message(
            "gating_thresholds",
            {
                "inhale_threshold": float(inhale_threshold),
                "exhale_threshold": float(exhale_threshold),
            },
        )

    def post_snapshot_request(self) -> None:
        self._post_message("snapshot", {})

    def post_valve_plan(
        self,
        plan,
        *,
        category: ActuationCategory,
        request_id: str,
    ) -> None:
        self._post_message(
            "valve_plan",
            {"plan": plan, "category": category, "request_id": request_id},
        )

    def post_flow_intent(
        self,
        *,
        mode: str,
        a: float,
        b: float,
        c: float,
        source: str,
    ) -> None:
        self._post_message(
            "flow_intent",
            {"mode": mode, "a": a, "b": b, "c": c, "source": source},
        )

    def post_flow_result(self, result: FlowCommandResult) -> None:
        self._post_message("flow_result", {"flow_result": result})
        if not self.isRunning() and self._writer_hal() is None:
            self.process_ready()

    def _post_message(self, kind: str, payload: dict[str, Any]) -> None:
        rejected = False
        with self._condition:
            if not self._accepting:
                rejected = True
            else:
                self._messages.append((kind, payload))
                self._condition.notify_all()
        if rejected:
            self._reject_stopped_message(kind, payload)

    def _reject_stopped_message(self, kind: str, payload: dict[str, Any]) -> None:
        """Give correlated intents a terminal result instead of replaying them after restart."""
        message = "动作线程已停止接单，请在硬件恢复后重新发起请求。"
        if kind in {"recorder_bind", "recorder_fence", "recorder_ready"}:
            payload["result"]["accepted"] = False
            if payload.get("ack") is not None:
                payload["ack"].set()
            return
        if kind == "start":
            current_epoch = int(self.protocol_state.execution_epoch)
            self.start_result_ready.emit(
                ProtocolStartAck(
                    accepted=False,
                    lease_epoch=(
                        current_epoch
                        if payload.get("lease_epoch") is None
                        else int(payload["lease_epoch"])
                    ),
                    previous_epoch=current_epoch,
                    execution_epoch=current_epoch,
                    status=self.protocol_state.status,
                    message=message,
                )
            )
        elif kind == "load":
            self.document_result_ready.emit(
                {
                    "document": payload["document"],
                    "success": False,
                    "message": message,
                }
            )
        elif kind == "valve_plan":
            self.plan_result_ready.emit(
                {
                    "request_id": payload["request_id"],
                    "success": False,
                    "message": message,
                }
            )
        elif kind == "flow_intent":
            with self._condition:
                self._sequence += 1
                sequence = self._sequence
            command = FlowCommand(
                command_id=f"flow-{self.protocol_state.execution_epoch}-{sequence}",
                execution_epoch=self.protocol_state.execution_epoch,
                sequence=sequence,
                mode=str(payload["mode"]),
                a=float(payload["a"]),
                b=float(payload["b"]),
                c=float(payload["c"]),
                source=str(payload["source"]),
            )
            result = FlowApplyResult(
                False,
                message,
                command.a,
                command.b,
                command.c,
                command.a + command.c,
                "rejected",
            )
            self.flow_result_ready.emit(FlowCommandResult(command=command, result=result))
        elif kind == "flow_result":
            self.flow_result_ready.emit(replace(payload["flow_result"], stale=True))

    def submit_emergency_close(self, valve: int, *, reason: str) -> ActuationCommand:
        if int(valve) == 0:
            raise ValueError(
                "selector 不能通过通用 emergency close 写入；必须使用 SafeStopPlan。"
            )
        return self._submit_emergency_close_target(
            valve,
            reason=reason,
            target_device=None,
            target_line=None,
        )

    def _submit_emergency_close_target(
        self,
        valve: int,
        *,
        reason: str,
        target_device: str | None,
        target_line: str | None,
        prefix: str = "safety-close",
        trial_id: str | None = None,
        trial_index: int | None = None,
    ) -> ActuationCommand:
        if int(valve) == 0:
            raise ValueError(
                "selector 不能通过普通安全关闭命令写入；必须使用专用 route API。"
            )
        with self._condition:
            self._sequence += 1
            command = ActuationCommand(
                command_id=f"{prefix}-{valve}-{self._sequence}",
                execution_epoch=self.protocol_state.execution_epoch,
                arm_epoch=self.protocol_state.arm_epoch,
                sequence=self._sequence,
                trial_id=(
                    trial_id
                    if trial_id is not None
                    else (
                        self.protocol_state.current_trial.trial_id
                        if self.protocol_state.current_trial
                        else None
                    )
                ),
                trial_index=(
                    trial_index
                    if trial_index is not None
                    else self.protocol_state.trial_index
                ),
                valve=valve,
                action=ActuationAction.CLOSE,
                category=ActuationCategory.SAFETY,
                expected_ns=int(self._clock_ns()),
                duration_ns=None,
                wall_timestamp=float(self._wall_clock()),
                safety_generation=self.interlock.read()[0],
                target_device=target_device,
                target_line=target_line,
            )
            self._commands_by_id[command.command_id] = command
            self._enqueue_emergency_locked(command)
            self.protocol_state.quality_block_reason = reason
            self._condition.notify_all()
            return command

    def _submit_all_configured_closes(
        self,
        *,
        reason: str,
        prefix: str = "safety-close-all",
        trial_id: str | None = None,
        trial_index: int | None = None,
    ) -> list[ActuationCommand]:
        if self.valve_service is None:
            valves = set(self.protocol_state.possibly_open_valves)
            if self.protocol_state.active_valve is not None:
                valves.add(self.protocol_state.active_valve)
            return [
                self._submit_emergency_close_target(
                    valve,
                    reason=reason,
                    target_device=None,
                    target_line=None,
                    prefix=prefix,
                    trial_id=trial_id,
                    trial_index=trial_index,
                )
                for valve in sorted(valves)
            ]
        steps = self.valve_service.all_configured_close_steps()
        commands = [
            self._submit_emergency_close_target(
                step.logical_valve,
                reason=reason,
                target_device=step.device,
                target_line=step.line,
                prefix=prefix,
                trial_id=trial_id,
                trial_index=trial_index,
            )
            for step in steps
        ]
        covered = {step.logical_valve for step in steps}
        conservative = set(self.protocol_state.possibly_open_valves)
        if self.protocol_state.active_valve is not None:
            conservative.add(self.protocol_state.active_valve)
        commands.extend(
            self._submit_emergency_close_target(
                valve,
                reason=reason,
                target_device=None,
                target_line=None,
                prefix=prefix,
                trial_id=trial_id,
                trial_index=trial_index,
            )
            for valve in sorted(conservative - covered)
        )
        return commands

    def process_ready(self, *, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            work = self._pop_ready()
            if work is None:
                break
            if isinstance(work, ActuationCommand):
                self._execute(work)
            else:
                kind, payload = work
                self._handle_message(kind, payload)
            processed += 1
        return processed

    def process_ready_with_do_ownership(self, *, max_items: int | None = None) -> int:
        """Deterministic non-threaded bridge for tests/startup before QThread starts."""
        if self.isRunning():
            return 0
        hal = self._writer_hal()
        if hal is not None and not hal.prepare_do_output():
            return 0
        if hal is not None:
            self._do_handed_off = False
        processed = 0
        try:
            processed = self.process_ready(max_items=max_items)
        finally:
            if hal is not None:
                self._do_handed_off = hal.release_do_output() is True
        return processed

    def consume_receipt(self, receipt: ActuationReceipt) -> None:
        receipt = self._enforce_selector_deadline(receipt)
        if (
            receipt.operation_id is not None
            and self._cleaning_plan is not None
            and receipt.operation_id == self._cleaning_plan.identity.operation_id
        ):
            self._consume_cleaning_receipt(receipt)
            return
        previous_receipt = self._seen_receipts.get(receipt.command_id)
        if previous_receipt is not None:
            self._handle_duplicate_receipt(
                receipt,
                conflicting=previous_receipt != receipt,
            )
            return
        source_command = self._commands_by_id.get(receipt.command_id)
        if source_command is not None and not self._receipt_matches_command(
            receipt,
            source_command,
        ):
            receipt = ActuationReceipt.from_write(
                command=source_command,
                started_ns=int(self._clock_ns()),
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                result=ActuationResult.UNCERTAIN,
                message="DO receipt 完整身份冲突，硬件结果按不确定处理。",
                stale=True,
            )
        self._remember_receipt(receipt)

        if receipt.category == ActuationCategory.SAFETY:
            if self.valve_service is not None:
                self.valve_service.commit_receipt(receipt)
            self._advance_background_safe_stop_receipt(receipt)
            if receipt.result == ActuationResult.SUCCESS:
                if receipt.valve != 0:
                    self._confirm_closed(receipt.valve)
            else:
                if receipt.valve != 0:
                    self.protocol_state.possibly_open_valves.add(receipt.valve)
                self._block(
                    "RECOVERY_REQUIRED：selector 路线写入失败，状态不确定；请执行显式恢复。"
                    if receipt.valve == 0
                    else f"RECOVERY_REQUIRED：安全关闭阀门 {receipt.valve} 失败，硬件状态不确定；请人工确认并重试。"
                )
                self._abort_safe_transition_after_close_failure(receipt)
            with self._condition:
                selector_waiter = self._safe_stop_selector_waiters.get(
                    receipt.command_id
                )
                if selector_waiter is not None:
                    selector_waiter[1].append(receipt)
                    selector_waiter[0].set()
                if receipt.command_id in self._shutdown_close_pending:
                    self._shutdown_close_pending.discard(receipt.command_id)
                    if receipt.result != ActuationResult.SUCCESS:
                        self._shutdown_close_failed = True
                    self._condition.notify_all()
                self._safe_transition_close_pending.discard(receipt.command_id)
            self._handle_plan_receipt(receipt)
            self._emit_receipt(receipt)
            self._maybe_finalize_safe_transition()
            self._retire_command(receipt.command_id)
            return

        if receipt.category != ActuationCategory.NORMAL:
            self._handle_plan_receipt(receipt)
            self._emit_receipt(receipt)
            self._retire_command(receipt.command_id)
            return

        expected_identity = (
            self.protocol_state.pending_open_command_id
            if receipt.action == ActuationAction.OPEN
            else self.protocol_state.pending_close_command_id
        )
        stale = (
            receipt.stale
            or receipt.execution_epoch != self.protocol_state.execution_epoch
            or receipt.command_id != expected_identity
        )
        if stale:
            if receipt.action == ActuationAction.OPEN and receipt.result == ActuationResult.SUCCESS:
                if receipt.valve == 0 and self.valve_service is not None:
                    self.valve_service.mark_selector_unknown()
                elif receipt.valve != 0:
                    self.protocol_state.possibly_open_valves.add(receipt.valve)
                self.invalidate_execution(
                    reason="收到陈旧成功开阀回执，已进入异常安全停止。",
                    close_all_configured=True,
                )
            elif receipt.action == ActuationAction.CLOSE and receipt.result == ActuationResult.SUCCESS:
                self._confirm_closed(receipt.valve)
            self._emit_receipt(replace(receipt, stale=True))
            self._retire_command(receipt.command_id)
            return

        if self.protocol_executor is not None:
            self._consume_executor_receipt(receipt, source_command=source_command)
            self._retire_command(receipt.command_id)
            return

        if receipt.action == ActuationAction.OPEN:
            self.protocol_state.pending_open_command_id = None
            if receipt.result != ActuationResult.SUCCESS:
                self._mark_target_uncertain(receipt.valve)
                self.invalidate_execution(
                    reason="开阀写入失败或结果不确定，已进入异常安全停止。",
                    close_all_configured=True,
                )
            else:
                self.protocol_state.active_valve = receipt.valve
                self.protocol_state.actual_open_ns = receipt.actual_ns
                update = self.metrics.record(receipt)
                self.protocol_state.quality = update.snapshot
                if update.severe:
                    self._block(
                        "阀门时序严重超限，已暂停新的阀门动作并请求安全关闭。"
                        "请检查系统负载和设备状态，确认所有阀门关闭后重新布防。"
                    )
                    self._mark_target_uncertain(receipt.valve)
                    self.invalidate_execution(
                        reason=self.protocol_state.quality_block_reason,
                        close_all_configured=True,
                    )
                else:
                    self._schedule_normal_close(receipt, source_command=source_command)
        else:
            if receipt.result != ActuationResult.SUCCESS:
                self._mark_target_uncertain(receipt.valve)
                self.invalidate_execution(
                    reason="定时关闭写入失败或状态不确定，已进入异常安全停止。",
                    close_all_configured=True,
                )
            else:
                self.protocol_state.pending_close_command_id = None
                self._confirm_closed(receipt.valve)
                update = self.metrics.record(receipt)
                self.protocol_state.quality = update.snapshot
                if update.severe:
                    self._block("关闭动作时序严重超限；阀门已确认关闭，请显式重新布防。")
        self._emit_receipt(receipt)
        self._retire_command(receipt.command_id)

    def _advance_background_safe_stop_receipt(
        self,
        receipt: ActuationReceipt,
    ) -> None:
        plan = self._background_safe_stop_plan
        if plan is None:
            return
        if receipt.command_id == self._background_safe_stop_selector_id:
            self._clear_background_safe_stop_deadline(
                plan.identity.operation_id,
                "selector",
            )
            self._background_safe_stop_selector_id = None
            selector_receipt = SelectorReceipt(
                command_id=receipt.command_id,
                identity=self._selector_receipt_identity(receipt, plan.identity),
                target=receipt.target or "",
                route=(
                    self.valve_service.selector_route
                    if receipt.result == ActuationResult.SUCCESS
                    and self.valve_service is not None
                    else SelectorRoute.UNKNOWN
                ),
                success=receipt.result == ActuationResult.SUCCESS,
                stale=bool(receipt.stale),
                message=str(receipt.message or ""),
            )
            if not plan.accept_selector(selector_receipt):
                self.protocol_state.quality_block_reason = (
                    f"RECOVERY_REQUIRED：{plan.recovery_reason}"
                )
            commands = self._submit_all_configured_closes(
                reason=(
                    "异常停止正在关闭全部气味阀。"
                    if plan.selector_confirmed
                    else self.protocol_state.quality_block_reason
                ),
                prefix="abnormal-safe-stop-odor-close",
            )
            self._track_background_close_commands(commands)
            return
        if receipt.command_id not in self._background_safe_stop_close_pending:
            return
        self._background_safe_stop_close_pending.discard(receipt.command_id)
        if receipt.result != ActuationResult.SUCCESS:
            plan.require_recovery(
                receipt.message or "异常停止气味阀关闭状态不确定。"
            )
        if self._background_safe_stop_close_pending:
            return
        if not plan.selector_confirmed or self.protocol_state.possibly_open_valves:
            plan.require_recovery(
                plan.recovery_reason or "异常停止安全证据不完整。"
            )
        self._submit_background_safe_stop_final_zero(plan)

    def _track_background_close_commands(
        self,
        commands: list[ActuationCommand],
    ) -> None:
        command_ids = {command.command_id for command in commands}
        self._background_safe_stop_close_pending = command_ids
        if self._pending_safe_transition is not None:
            self._safe_transition_close_pending = set(command_ids)

    def invalidate_execution(self, *, reason: str, close_all_configured: bool = False) -> None:
        with self._condition:
            self.protocol_state.execution_epoch += 1
            self.protocol_state.arm_epoch += 1
            self.protocol_state.pending_open_command_id = None
            self.protocol_state.pending_close_command_id = None
            cancelled = self._cancel_non_safety_commands_locked(reason=reason)
            self.request_ttl_disarm()
            self._block(reason)
            self._condition.notify_all()
        self._settle_cancelled_receipts(cancelled)
        self._begin_background_safe_stop(
            reason=reason,
            close_all_configured=close_all_configured,
        )

    def _begin_background_safe_stop(
        self,
        *,
        reason: str,
        close_all_configured: bool,
    ) -> None:
        del close_all_configured  # Story 4.5 always converges every configured odor output.
        previous = self._background_safe_stop_plan
        if previous is not None:
            if not previous.safe_terminal and previous.status.value != "recovery_required":
                return
            self._background_safe_stop_plan = None
            self._background_safe_stop_flow_id = None
            self._background_safe_stop_flow_command = None
            self._background_safe_stop_final_flow_id = None
            self._background_safe_stop_final_flow_command = None
            self._background_safe_stop_flow_deadlines.clear()
            self._background_safe_stop_selector_id = None
            self._background_safe_stop_close_pending.clear()
        selector = (
            None
            if self.valve_service is None
            else getattr(self.valve_service, "selector", None)
        )
        if self._flow_submitter is None:
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{reason}；A 清零 owner 不可用。"
            )
            commands = self._submit_all_configured_closes(
                reason=self.protocol_state.quality_block_reason,
                prefix="recovery-odor-close",
            )
            self._track_background_close_commands(commands)
            if not commands and self._pending_safe_transition is not None:
                self._finalize_safe_transition()
            return
        self._background_safe_stop_generation += 1
        identity = SafeStopIdentity(
            operation_id=f"abnormal-safe-stop-{self._background_safe_stop_generation}",
            generation=self._background_safe_stop_generation,
            execution_epoch=self.protocol_state.execution_epoch,
        )
        plan = SafeStopPlan(identity, selector)
        self._background_safe_stop_plan = plan
        self._safe_stop_identity = identity
        self._sequence += 1
        command = FlowCommand(
            command_id=f"{identity.operation_id}:a-zero:{self._sequence}",
            execution_epoch=identity.execution_epoch,
            sequence=self._sequence,
            mode="safe_stop_a_zero",
            a=0.0,
            b=0.0,
            c=0.0,
            source="safety:safe-stop",
            operation_id=identity.operation_id,
            generation=identity.generation,
        )
        plan.expect_a_zero(command.command_id)
        self._background_safe_stop_flow_id = command.command_id
        self._background_safe_stop_flow_command = command
        submitted = self._flow_submitter(command)
        if submitted is False:
            self._background_safe_stop_flow_id = None
            self._background_safe_stop_flow_command = None
            plan.require_recovery("A 清零命令未被 FlowWorker 接受。")
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            commands = self._submit_all_configured_closes(
                reason=self.protocol_state.quality_block_reason,
                prefix="recovery-odor-close",
            )
            self._track_background_close_commands(commands)
            if not commands:
                self._submit_background_safe_stop_final_zero(plan)
            return
        deadline_ns = int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns
        self._background_safe_stop_flow_deadlines[command.command_id] = deadline_ns
        self._sequence += 1
        heapq.heappush(
            self._deadline_heap,
            (
                deadline_ns,
                5,
                self._sequence,
                "background_safe_stop_timeout",
                {
                    "operation_id": identity.operation_id,
                    "stage": "a_zero",
                },
            ),
        )

    def _is_background_safe_stop_flow_result(
        self,
        wrapped: FlowCommandResult,
    ) -> bool:
        plan = self._background_safe_stop_plan
        return bool(
            plan is not None
            and wrapped.command.operation_id == plan.identity.operation_id
        )

    def _consume_background_safe_stop_flow_result(
        self,
        wrapped: FlowCommandResult,
    ) -> None:
        plan = self._background_safe_stop_plan
        if plan is None:
            return
        if wrapped.command.command_id == self._background_safe_stop_final_flow_id:
            self._clear_background_safe_stop_deadline(
                plan.identity.operation_id,
                "final_zero",
            )
            self._background_safe_stop_final_flow_id = None
            expected_command = self._background_safe_stop_final_flow_command
            self._background_safe_stop_final_flow_command = None
            late = self._consume_background_flow_deadline(wrapped.command.command_id)
            final_zero = bool(
                expected_command is not None
                and wrapped.command == expected_command
                and not late
                and wrapped.result.success
                and not wrapped.stale
                and all(
                    math.isfinite(float(value)) and abs(float(value)) <= 1e-9
                    for value in (
                        wrapped.result.a,
                        wrapped.result.b,
                        wrapped.result.c,
                    )
                )
            )
            if not final_zero:
                plan.require_recovery(
                    wrapped.result.message or "异常停止 A/B/C 终态清零未确认。"
                )
            if self._pending_safe_transition is not None and final_zero:
                self.protocol_state.quality_block_reason = (
                    "气路已收敛，等待 controller 确认 protocol lease handoff。"
                )
                self._safe_transition_handoff_identity = plan.identity
                self.protocol_safe_stop_handoff_requested.emit(plan.identity)
            else:
                plan.complete(
                    odors_closed=not self.protocol_state.possibly_open_valves,
                    owners_handed_off=False,
                )
                self.protocol_state.quality_block_reason = (
                    f"RECOVERY_REQUIRED：{plan.recovery_reason or 'owner handoff 未确认。'}"
                )
            return
        if (
            wrapped.command.command_id != self._background_safe_stop_flow_id
            or wrapped.command != self._background_safe_stop_flow_command
        ):
            plan.require_recovery("异常停止 flow receipt 迟到或 command identity 冲突。")
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            return
        receipt = AZeroReceipt(
            command_id=wrapped.command.command_id,
            identity=SafeStopIdentity(
                wrapped.command.operation_id or "",
                int(wrapped.command.generation or 0),
                wrapped.command.execution_epoch,
            ),
            success=bool(wrapped.result.success),
            confirmed_a=float(wrapped.result.a),
            stale=bool(wrapped.stale),
            message=str(wrapped.result.message or ""),
            source=wrapped.command.source,
            mode=wrapped.command.mode,
            lease_token=wrapped.command.lease_token,
        )
        self._clear_background_safe_stop_deadline(
            plan.identity.operation_id,
            "a_zero",
        )
        late = self._consume_background_flow_deadline(wrapped.command.command_id)
        self._background_safe_stop_flow_id = None
        self._background_safe_stop_flow_command = None
        if late:
            plan.require_recovery("A 清零 receipt 迟到或超过单调 deadline。")
        if late or not plan.accept_a_zero(receipt):
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            commands = self._submit_all_configured_closes(
                reason=self.protocol_state.quality_block_reason,
                prefix="recovery-odor-close",
            )
            self._track_background_close_commands(commands)
            if not commands:
                self._submit_background_safe_stop_final_zero(plan)
            return
        if plan.selector is None:
            plan.require_recovery("selector 配置不可用，A=0 后保持原路线并等待恢复。")
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            commands = self._submit_all_configured_closes(
                reason=self.protocol_state.quality_block_reason,
                prefix="recovery-odor-close",
            )
            self._track_background_close_commands(commands)
            if not commands:
                self._submit_background_safe_stop_final_zero(plan)
            return
        self._sequence += 1
        command_id = (
            f"{plan.identity.operation_id}:selector-safe:{self._sequence}"
        )
        plan.expect_selector(command_id)
        self._background_safe_stop_selector_id = command_id
        self._safe_stop_selector_deadlines[command_id] = (
            int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns
        )
        self._begin_safe_stop_selector(
            identity=plan.identity,
            command_id=command_id,
        )
        self._sequence += 1
        heapq.heappush(
            self._deadline_heap,
            (
                int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns,
                5,
                self._sequence,
                "background_safe_stop_timeout",
                {
                    "operation_id": plan.identity.operation_id,
                    "stage": "selector",
                },
            ),
        )

    def _submit_background_safe_stop_final_zero(self, plan: SafeStopPlan) -> None:
        if self._flow_submitter is None or self._background_safe_stop_final_flow_id:
            if self._flow_submitter is None:
                plan.require_recovery("异常停止 final A/B/C 清零 owner 不可用。")
            return
        self._sequence += 1
        command = FlowCommand(
            command_id=f"{plan.identity.operation_id}:final-zero:{self._sequence}",
            execution_epoch=plan.identity.execution_epoch,
            sequence=self._sequence,
            mode="zero",
            a=0.0,
            b=0.0,
            c=0.0,
            source="safety:safe-stop",
            operation_id=plan.identity.operation_id,
            generation=plan.identity.generation,
        )
        self._background_safe_stop_final_flow_id = command.command_id
        self._background_safe_stop_final_flow_command = command
        if self._flow_submitter(command) is False:
            self._background_safe_stop_final_flow_id = None
            self._background_safe_stop_final_flow_command = None
            plan.require_recovery("异常停止 final A/B/C 清零命令未被接受。")
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            return
        deadline_ns = int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns
        self._background_safe_stop_flow_deadlines[command.command_id] = deadline_ns
        self._sequence += 1
        heapq.heappush(
            self._deadline_heap,
            (
                deadline_ns,
                5,
                self._sequence,
                "background_safe_stop_timeout",
                {
                    "operation_id": plan.identity.operation_id,
                    "stage": "final_zero",
                },
            ),
        )

    def _clear_background_safe_stop_deadline(
        self,
        operation_id: str,
        stage: str,
    ) -> None:
        self._deadline_heap = [
            item
            for item in self._deadline_heap
            if not (
                item[3] == "background_safe_stop_timeout"
                and item[4].get("operation_id") == operation_id
                and item[4].get("stage") == stage
            )
        ]
        heapq.heapify(self._deadline_heap)

    def _consume_background_flow_deadline(self, command_id: str) -> bool:
        deadline_ns = self._background_safe_stop_flow_deadlines.pop(command_id, None)
        return deadline_ns is None or int(self._clock_ns()) > deadline_ns

    def _handle_background_safe_stop_timeout(
        self,
        *,
        operation_id: str,
        stage: str,
    ) -> None:
        plan = self._background_safe_stop_plan
        if plan is None or plan.identity.operation_id != operation_id:
            return
        if stage == "a_zero" and plan.status.value != "a_zero_pending":
            return
        if stage == "selector" and plan.status.value != "selector_pending":
            return
        if stage == "final_zero" and self._background_safe_stop_final_flow_id is None:
            return
        label = {
            "a_zero": "A 清零 receipt",
            "selector": "selector receipt",
            "final_zero": "A/B/C final zero receipt",
        }.get(stage, stage)
        plan.timeout(label)
        if stage == "a_zero":
            if self._background_safe_stop_flow_id is not None:
                self._background_safe_stop_flow_deadlines.pop(
                    self._background_safe_stop_flow_id,
                    None,
                )
            self._background_safe_stop_flow_id = None
            self._background_safe_stop_flow_command = None
        elif stage == "final_zero":
            if self._background_safe_stop_final_flow_id is not None:
                self._background_safe_stop_flow_deadlines.pop(
                    self._background_safe_stop_final_flow_id,
                    None,
                )
            self._background_safe_stop_final_flow_id = None
            self._background_safe_stop_final_flow_command = None
        if self.valve_service is not None and stage == "selector":
            self.valve_service.mark_selector_unknown()
        self.protocol_state.quality_block_reason = (
            f"RECOVERY_REQUIRED：{plan.recovery_reason}"
        )
        if stage != "final_zero":
            commands = self._submit_all_configured_closes(
                reason=self.protocol_state.quality_block_reason,
                prefix="recovery-odor-close",
            )
            self._track_background_close_commands(commands)
            if not commands:
                self._submit_background_safe_stop_final_zero(plan)

    def request_ttl_arm(self, *, arm_epoch: int) -> None:
        self.protocol_state.ttl_armed = False
        self._pending_ttl_arm_epoch = int(arm_epoch)
        self.ttl_arm_requested.emit(int(arm_epoch))

    def consume_ttl_arm_ack(self, arm_epoch: int, armed: bool) -> None:
        self._post_message(
            "ttl_arm_ack",
            {"arm_epoch": int(arm_epoch), "armed": bool(armed)},
        )
        if not self.isRunning() and self._writer_hal() is None:
            self.process_ready()

    def _apply_ttl_arm_ack(self, *, arm_epoch: int, armed: bool) -> None:
        if arm_epoch != self._pending_ttl_arm_epoch or arm_epoch != self.protocol_state.arm_epoch:
            return
        self._pending_ttl_arm_epoch = None
        if armed:
            self.protocol_state.ttl_armed = True
            self._ttl_hardware_armed_epoch = arm_epoch
            return
        self.invalidate_execution(reason="TTL 输入布防失败，协议已阻断。")
        if self.protocol_executor is not None:
            event = self.protocol_executor._event(
                "ttl_arm_failed",
                self._wall_clock(),
                safety_state=self._current_readiness().safety_state,
                result="blocked",
                message="TTL 输入布防返回失败，已失效当前布防并阻断协议。",
            )
            result = self.protocol_executor._result_with_events([event])
            self._emit_executor_result(result)

    def request_ttl_disarm(self) -> None:
        self._pending_ttl_arm_epoch = None
        self._ttl_hardware_armed_epoch = None
        self.protocol_state.ttl_armed = False
        self.ttl_disarm_requested.emit()

    def run(self) -> None:
        with self._condition:
            if not self._accepting:
                self._do_handed_off = True
                self._condition.notify_all()
                return
        hal = self._writer_hal()
        if hal is not None and not hal.prepare_do_output():
            with self._condition:
                self._accepting = False
                self._block("DO session 准备失败，动作 owner 已阻断。")
                self._do_handed_off = True
                self._condition.notify_all()
            self.status_message.emit("DO session 准备失败，动作线程未启动；请检查 NI 资源占用。")
            return
        self._do_handed_off = False
        self._running = True
        try:
            while self._running:
                if self.process_ready(max_items=1):
                    continue
                with self._condition:
                    if not self._running:
                        break
                    deadlines = []
                    if self._normal_heap:
                        deadlines.append(self._normal_heap[0][0])
                    if self._deadline_heap:
                        deadlines.append(self._deadline_heap[0][0])
                    timeout = None
                    if deadlines:
                        remaining_ns = min(deadlines) - int(self._clock_ns())
                        timeout = max(0.0, remaining_ns / 1_000_000_000)
                    self._condition.wait(timeout=timeout)
        finally:
            self._emit_recorder_fence()
            released = True
            if hal is not None:
                released = hal.release_do_output() is True
            with self._condition:
                self._do_handed_off = released
                self._condition.notify_all()

    def emergency_close_all(self, timeout_ms: int = 500) -> bool:
        """Legacy emergency API: fence and close odor outputs only.

        The selector is deliberately excluded; only SafeStopPlan may route it
        after a matching A=0 receipt.
        """
        if self.valve_service is None:
            return False
        with self._condition:
            self._shutdown_close_started = False
            self._shutdown_close_failed = False
            self._messages.appendleft(("emergency_close_all", {}))
            self._condition.notify_all()
        if not self.isRunning():
            self.process_ready_with_do_ownership()
        deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
        with self._condition:
            while not self._shutdown_close_started or self._shutdown_close_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return (
                self._shutdown_close_started
                and not self._shutdown_close_pending
                and not self._shutdown_close_failed
            )

    def fence_for_safe_stop(
        self,
        *,
        operation_id: str,
        generation: int,
        reason: str,
        timeout_ms: int = 2000,
    ) -> SafeStopIdentity | None:
        event = threading.Event()
        result: dict[str, SafeStopIdentity | None] = {"identity": None}
        with self._condition:
            self._messages.appendleft(
                (
                    "safe_stop_fence",
                    {
                        "operation_id": str(operation_id),
                        "generation": int(generation),
                        "reason": str(reason),
                        "event": event,
                        "result": result,
                    },
                )
            )
            self._condition.notify_all()
        if not self.isRunning():
            self.process_ready()
        if not event.wait(max(1, int(timeout_ms)) / 1000.0):
            return None
        return result["identity"]

    def _begin_safe_stop_fence(
        self,
        *,
        operation_id: str,
        generation: int,
        reason: str,
        event: threading.Event,
        result: dict[str, SafeStopIdentity | None],
    ) -> None:
        with self._condition:
            self._fence_cleaning_for_global_safe_stop(reason)
            self._accepting = False
            self.protocol_state.execution_epoch += 1
            self.protocol_state.arm_epoch += 1
            self.protocol_state.pending_open_command_id = None
            self.protocol_state.pending_close_command_id = None
            cancelled = self._cancel_non_safety_commands_locked(
                reason="安全停止已取消并失效旧 epoch 动作。"
            )
            self._deadline_heap.clear()
            self.request_ttl_disarm()
            identity = SafeStopIdentity(
                operation_id=operation_id,
                generation=generation,
                execution_epoch=self.protocol_state.execution_epoch,
            )
            self._safe_stop_identity = identity
            self._block(reason)
            result["identity"] = identity
            self._condition.notify_all()
        self._settle_cancelled_receipts(cancelled)
        event.set()

    def _fence_cleaning_for_global_safe_stop(self, reason: str) -> None:
        queued_starts = [
            payload
            for kind, payload in self._messages
            if kind == "cleaning_start"
        ]
        if queued_starts:
            self._messages = deque(
                (kind, payload)
                for kind, payload in self._messages
                if kind != "cleaning_start"
            )
            self._queued_maintenance_recorders.extend(
                payload["recorder"] for payload in queued_starts
            )
            for payload in queued_starts:
                queued_plan = payload["plan"]
                recovery_reason = (
                    f"全局安全停止已取消待启动 cleaning：{reason}"
                )
                self.cleaning_result_ready.emit(
                    CleaningResult(
                        identity=queued_plan.identity,
                        status=CleaningStatus.RECOVERY_REQUIRED,
                        outcome=CleaningOutcome.FAILED,
                        reason=recovery_reason,
                    )
                )
        plan = self._cleaning_plan
        if plan is None or self._cleaning_snapshot.status not in {
            CleaningStatus.PREPARING,
            CleaningStatus.RUNNING,
            CleaningStatus.STOPPING,
        }:
            return
        operation_id = plan.identity.operation_id
        self._normal_heap = [
            item
            for item in self._normal_heap
            if item[3].operation_id != operation_id
        ]
        heapq.heapify(self._normal_heap)
        self._emergency = deque(
            command
            for command in self._emergency
            if command.operation_id != operation_id
        )
        self._cleaning_expected.clear()
        self._cleaning_pending_flow_id = None
        self._cleaning_pending_flow_command = None
        self._cleaning_phase = "terminal"
        recovery_reason = f"全局安全停止已抢占 cleaning：{reason}"
        self._publish_cleaning_snapshot(
            status=CleaningStatus.RECOVERY_REQUIRED,
            recovery_reason=recovery_reason,
            flow_zero_confirmed=False,
            selector_safe_confirmed=False,
        )
        self.cleaning_result_ready.emit(
            CleaningResult(
                identity=plan.identity,
                status=CleaningStatus.RECOVERY_REQUIRED,
                outcome=CleaningOutcome.FAILED,
                reason=recovery_reason,
            )
        )

    def route_selector_safe(
        self,
        plan: SafeStopPlan,
        timeout_ms: int = 500,
    ) -> SelectorReceipt | None:
        if not plan.selector_allowed or self.valve_service is None:
            return None
        self._safe_stop_plan = plan
        with self._condition:
            if self._safe_stop_identity != plan.identity:
                return None
            self._sequence += 1
            command_id = f"safe-stop-selector-{plan.identity.generation}-{self._sequence}"
            plan.expect_selector(command_id)
            event = threading.Event()
            receipts: list[ActuationReceipt] = []
            self._safe_stop_selector_waiters[command_id] = (event, receipts)
            self._safe_stop_selector_deadlines[command_id] = (
                int(self._clock_ns()) + max(1, int(timeout_ms)) * 1_000_000
            )
            self._messages.appendleft(
                (
                    "safe_stop_selector",
                    {"identity": plan.identity, "command_id": command_id},
                )
            )
            self._condition.notify_all()
        if not self.isRunning():
            self.process_ready_with_do_ownership()
        completed = event.wait(max(1, int(timeout_ms)) / 1000.0)
        with self._condition:
            self._safe_stop_selector_waiters.pop(command_id, None)
        if not completed or not receipts:
            self.valve_service.mark_selector_unknown()
            return None
        receipt = receipts[0]
        return SelectorReceipt(
            command_id=receipt.command_id,
            identity=self._selector_receipt_identity(receipt, plan.identity),
            target=receipt.target or "",
            route=(
                self.valve_service.selector_route
                if receipt.result == ActuationResult.SUCCESS
                else SelectorRoute.UNKNOWN
            ),
            success=receipt.result == ActuationResult.SUCCESS,
            stale=bool(receipt.stale),
            message=str(receipt.message or ""),
        )

    @staticmethod
    def _selector_receipt_identity(
        receipt: ActuationReceipt,
        expected: SafeStopIdentity,
    ) -> SafeStopIdentity:
        """Preserve the writer receipt identity so the evidence gate can reject it."""

        operation_id = receipt.operation_id
        if not isinstance(operation_id, str) or not operation_id:
            operation_id = f"missing-selector-identity:{expected.operation_id}"
        generation = receipt.generation
        if type(generation) is not int or generation < 0:
            generation = 0
        execution_epoch = receipt.execution_epoch
        if type(execution_epoch) is not int or execution_epoch < 0:
            execution_epoch = 0
        return SafeStopIdentity(
            operation_id=operation_id,
            generation=generation,
            execution_epoch=execution_epoch,
        )

    def _begin_safe_stop_selector(
        self,
        *,
        identity: SafeStopIdentity,
        command_id: str,
    ) -> None:
        if self.valve_service is None or identity != self._safe_stop_identity:
            waiter = self._safe_stop_selector_waiters.get(command_id)
            if waiter is not None:
                waiter[0].set()
            return
        try:
            selector = self.valve_service.selector
            if selector is None:
                raise ValueError("selector 未配置。")
            step = self.valve_service.selector_route_step(selector.safe_route)
        except ValueError:
            self.valve_service.mark_selector_unknown()
            waiter = self._safe_stop_selector_waiters.get(command_id)
            if waiter is not None:
                waiter[0].set()
            return
        self._sequence += 1
        command = ActuationCommand(
            command_id=command_id,
            execution_epoch=identity.execution_epoch,
            arm_epoch=self.protocol_state.arm_epoch,
            sequence=self._sequence,
            trial_id=None,
            trial_index=None,
            valve=0,
            action=ActuationAction.OPEN if step.state else ActuationAction.CLOSE,
            category=ActuationCategory.SAFETY,
            expected_ns=int(self._clock_ns()),
            duration_ns=None,
            wall_timestamp=float(self._wall_clock()),
            safety_generation=self.interlock.read()[0],
            target_device=step.device,
            target_line=step.line,
            operation_id=identity.operation_id,
            generation=identity.generation,
            step_id="selector_safe",
            action_kind=ActuationAction.OPEN if step.state else ActuationAction.CLOSE,
        )
        self._commands_by_id[command.command_id] = command
        self._enqueue_emergency_locked(command)

    def close_odors_for_safe_stop(
        self,
        identity: SafeStopIdentity,
        timeout_ms: int = 500,
    ) -> bool:
        if identity != self._safe_stop_identity:
            return False
        with self._condition:
            self._shutdown_close_started = False
            self._shutdown_close_failed = False
            self._messages.appendleft(
                ("safe_stop_close_odors", {"identity": identity})
            )
            self._condition.notify_all()
        if not self.isRunning():
            self.process_ready_with_do_ownership()
        deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
        with self._condition:
            while not self._shutdown_close_started or self._shutdown_close_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return bool(
                self._shutdown_close_started
                and not self._shutdown_close_pending
                and not self._shutdown_close_failed
            )

    def _begin_safe_stop_odor_close(self, identity: SafeStopIdentity) -> None:
        if identity != self._safe_stop_identity:
            return
        with self._condition:
            self._shutdown_close_pending.clear()
            self._shutdown_close_started = True
            for step in self._all_configured_close_steps():
                self._sequence += 1
                command = ActuationCommand(
                    command_id=f"safe-stop-odor-close-{step.logical_valve}-{self._sequence}",
                    execution_epoch=identity.execution_epoch,
                    arm_epoch=self.protocol_state.arm_epoch,
                    sequence=self._sequence,
                    trial_id=None,
                    trial_index=None,
                    valve=step.logical_valve,
                    action=ActuationAction.CLOSE,
                    category=ActuationCategory.SAFETY,
                    expected_ns=int(self._clock_ns()),
                    duration_ns=None,
                    wall_timestamp=float(self._wall_clock()),
                    safety_generation=self.interlock.read()[0],
                    target_device=step.device,
                    target_line=step.line,
                    operation_id=identity.operation_id,
                    generation=identity.generation,
                    step_id=f"odor_close_{step.logical_valve}",
                    action_kind=ActuationAction.CLOSE,
                )
                self._commands_by_id[command.command_id] = command
                self._shutdown_close_pending.add(command.command_id)
                self._enqueue_emergency_locked(command)
            self._condition.notify_all()

    def _begin_emergency_close_all(self) -> None:
        if self._cleaning_snapshot.status in {
            CleaningStatus.PREPARING,
            CleaningStatus.RUNNING,
            CleaningStatus.STOPPING,
        }:
            self._begin_cleaning_stop(
                reason="应用 shutdown 已抢占清洗并请求完整安全收敛。",
                aborted=False,
                recovery_required=True,
            )
        with self._condition:
            self._accepting = False
            self.protocol_state.execution_epoch += 1
            self.protocol_state.arm_epoch += 1
            self.protocol_state.pending_open_command_id = None
            self.protocol_state.pending_close_command_id = None
            cancelled = self._cancel_non_safety_commands_locked(
                reason="紧急关闭已取消尚未执行的普通动作。"
            )
            self._deadline_heap.clear()
            queued_messages = list(self._messages)
            self._messages.clear()
            self.request_ttl_disarm()
            self._shutdown_close_pending.clear()
            self._shutdown_close_started = True
            for step in self._all_configured_close_steps():
                self._sequence += 1
                command = ActuationCommand(
                    command_id=f"shutdown-close-{step.logical_valve}-{self._sequence}",
                    execution_epoch=self.protocol_state.execution_epoch,
                    arm_epoch=self.protocol_state.arm_epoch,
                    sequence=self._sequence,
                    trial_id=None,
                    trial_index=None,
                    valve=step.logical_valve,
                    action=ActuationAction.CLOSE,
                    category=ActuationCategory.SAFETY,
                    expected_ns=int(self._clock_ns()),
                    duration_ns=None,
                    wall_timestamp=float(self._wall_clock()),
                    safety_generation=self.interlock.read()[0],
                    target_device=step.device,
                    target_line=step.line,
                )
                self._commands_by_id[command.command_id] = command
                self._shutdown_close_pending.add(command.command_id)
                self._enqueue_emergency_locked(command)
            self._condition.notify_all()
        self._settle_cancelled_receipts(cancelled)
        self._reject_queued_messages(queued_messages)

    def fallback_close_all_after_handoff(self) -> bool:
        """Rebuild a DO session only after the previous owner has fully released it."""
        if self.isRunning() or not self._do_handed_off or self.valve_service is None:
            return False
        hal = self._writer_hal()
        if hal is None or not hal.prepare_do_output():
            return False
        success = True
        released = False
        try:
            for step in self._all_configured_close_steps():
                self._sequence += 1
                command = ActuationCommand(
                    command_id=f"fallback-close-{step.logical_valve}-{self._sequence}",
                    execution_epoch=self.protocol_state.execution_epoch,
                    arm_epoch=self.protocol_state.arm_epoch,
                    sequence=self._sequence,
                    trial_id=None,
                    trial_index=None,
                    valve=step.logical_valve,
                    action=ActuationAction.CLOSE,
                    category=ActuationCategory.SAFETY,
                    expected_ns=int(self._clock_ns()),
                    duration_ns=None,
                    wall_timestamp=float(self._wall_clock()),
                    safety_generation=self.interlock.read()[0],
                    target_device=step.device,
                    target_line=step.line,
                )
                receipt = self.writer(command)
                success = success and receipt.result == ActuationResult.SUCCESS
                if receipt.result == ActuationResult.SUCCESS:
                    self._confirm_closed(receipt.valve)
                    self.valve_service.commit_receipt(receipt)
        finally:
            released = hal.release_do_output() is True
            self._do_handed_off = released
        return success and released

    def _writer_hal(self):
        hal = getattr(self.writer, "hal", None)
        if hal is not None:
            return hal
        owner = getattr(self.writer, "__self__", None)
        return getattr(owner, "hal", None)

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        with self._condition:
            self._accepting = False
            self._running = False
            cancelled = self._cancel_non_safety_commands_locked(
                reason="动作线程关闭已取消尚未执行的普通动作。"
            )
            self._deadline_heap.clear()
            queued_messages = list(self._messages)
            self._messages.clear()
            self._condition.notify_all()
        self._settle_cancelled_receipts(cancelled)
        self._reject_queued_messages(queued_messages)
        if self.isRunning():
            joined = bool(self.wait(max(1, int(timeout_ms))))
            return joined and self._do_handed_off
        return self._do_handed_off

    def prepare_restart(self) -> bool:
        if self.isRunning() or not self._do_handed_off:
            return False
        with self._condition:
            # A restart is a fresh admission epoch; stopped-run intents must
            # never cross this boundary.
            queued_messages = list(self._messages)
            self._messages.clear()
            cancelled = self._cancel_non_safety_commands_locked(
                reason="动作线程重启已取消上一运行周期的普通动作。"
            )
            self._deadline_heap.clear()
            self._safe_stop_identity = None
            self._safe_stop_plan = None
            self._safe_stop_selector_waiters.clear()
            self._safe_stop_selector_deadlines.clear()
            self._background_safe_stop_plan = None
            self._background_safe_stop_flow_id = None
            self._background_safe_stop_flow_command = None
            self._background_safe_stop_final_flow_id = None
            self._background_safe_stop_final_flow_command = None
            self._background_safe_stop_flow_deadlines.clear()
            self._background_safe_stop_selector_id = None
            self._background_safe_stop_close_pending.clear()
            self._accepting = True
        self._settle_cancelled_receipts(cancelled)
        self._reject_queued_messages(queued_messages)
        return True

    def _pop_ready(self) -> ActuationCommand | tuple[str, dict[str, Any]] | None:
        with self._condition:
            if self._emergency:
                command = self._emergency.popleft()
                return command
            safety_priority_message_kinds = {
                "cleaning_recover",
                "cleaning_stop",
                "emergency_close_all",
                "input_error",
                "interlock_changed",
                "recorder_failed",
                "safe_stop_close_odors",
                "safe_stop_fence",
                "safe_stop_selector",
                "stop",
            }
            # A recorder fence may be queued before a safe-stop Flow receipt.
            # The fence itself waits for the stop transition, so allow only
            # this correlated safety receipt to cross that producer barrier.
            for index, message in enumerate(self._messages):
                if (
                    message[0] == "flow_result"
                    and message[1]["flow_result"].command.source
                    == "safety:safe-stop"
                ):
                    del self._messages[index]
                    return message
            for index, message in enumerate(self._messages):
                if message[0] in {"recorder_bind", "recorder_fence"}:
                    break
                if message[0] in safety_priority_message_kinds:
                    del self._messages[index]
                    return message
            now_ns = int(self._clock_ns())
            normal_head = self._normal_heap[0][:3] if self._normal_heap else None
            deadline_head = self._deadline_heap[0][:3] if self._deadline_heap else None
            if deadline_head is not None and deadline_head[0] <= now_ns and (
                normal_head is None or deadline_head <= normal_head
            ):
                _, _, _, kind, payload = heapq.heappop(self._deadline_heap)
                return kind, payload
            if (
                self._normal_heap
                and self._normal_heap[0][0] <= now_ns
                and self._normal_heap[0][3].action == ActuationAction.CLOSE
            ):
                return heapq.heappop(self._normal_heap)[3]
            next_due_ns = (
                self._normal_heap[0][0]
                if self._normal_heap
                and self._normal_heap[0][3].action == ActuationAction.CLOSE
                else None
            )
            if (
                next_due_ns is not None
                and next_due_ns - now_ns <= self._NORMAL_DEADLINE_RESERVE_NS
            ):
                return None
            priority_message_kinds = {
                "cleaning_recover",
                "cleaning_start",
                "cleaning_stop",
                "emergency_close_all",
                "flow_result",
                "input_error",
                "interlock_changed",
                "load",
                "manual_trigger",
                "mode",
                "pause",
                "readiness",
                "recorder_bind",
                "recorder_failed",
                "recorder_ready",
                "safe_stop_close_odors",
                "safe_stop_fence",
                "safe_stop_selector",
                "start",
                "stop",
            }
            for index, message in enumerate(self._messages):
                if message[0] in {"recorder_bind", "recorder_fence"}:
                    break
                if message[0] in priority_message_kinds:
                    del self._messages[index]
                    return message
            if self._normal_heap and self._normal_heap[0][0] <= now_ns:
                return heapq.heappop(self._normal_heap)[3]
            if self._messages:
                if self._messages[0][0] == "recorder_fence" and (
                    self._normal_heap
                    or self._deadline_heap
                    or self._emergency
                    or self._plan_contexts
                    or self._pending_safe_transition is not None
                ):
                    return None
                kind, payload = self._messages.popleft()
                if kind != "ai_batch":
                    return kind, payload
                samples = list(payload["batch"].samples)
                while (
                    self._messages
                    and self._messages[0][0] == "ai_batch"
                    and self._messages[0][1].get("readiness")
                    == payload.get("readiness")
                ):
                    _, next_payload = self._messages.popleft()
                    samples.extend(next_payload["batch"].samples)
                if len(samples) != len(payload["batch"].samples):
                    payload = {
                        **payload,
                        "batch": replace(payload["batch"], samples=tuple(samples)),
                    }
                return kind, payload
            return None

    def _handle_message(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "safe_stop_fence":
            self._begin_safe_stop_fence(**payload)
            return
        if kind == "safe_stop_selector":
            self._begin_safe_stop_selector(**payload)
            return
        if kind == "safe_stop_close_odors":
            self._begin_safe_stop_odor_close(**payload)
            return
        if kind == "background_safe_stop_timeout":
            self._handle_background_safe_stop_timeout(**payload)
            return
        if kind == "cleaning_start":
            self._begin_cleaning(**payload)
            return
        if kind == "cleaning_stop":
            self._begin_cleaning_stop(
                reason=payload["reason"],
                aborted=payload["aborted"],
            )
            return
        if kind == "cleaning_recover":
            self._retry_cleaning_recovery()
            return
        if kind == "cleaning_deadline":
            self._handle_cleaning_deadline(**payload)
            return
        if kind == "cleaning_flow_ready_timeout":
            self._handle_cleaning_flow_ready_timeout(**payload)
            return
        if kind == "cleaning_flow_receipt_timeout":
            self._handle_cleaning_flow_receipt_timeout(**payload)
            return
        if kind == "flow_result" and self._is_cleaning_flow_result(
            payload["flow_result"]
        ):
            # 先把 maintenance flow 回执交给 Controller/recorder，再允许该回执
            # 推进到终态并发出 producer fence，避免最后一个清零回执落在 fence 之后。
            self.flow_result_ready.emit(payload["flow_result"])
            self._consume_cleaning_flow_result(payload["flow_result"])
            return
        if kind == "flow_result" and self._is_background_safe_stop_flow_result(
            payload["flow_result"]
        ):
            self.flow_result_ready.emit(payload["flow_result"])
            self._consume_background_safe_stop_flow_result(payload["flow_result"])
            return
        if self._cleaning_snapshot.status in {
            CleaningStatus.PREPARING,
            CleaningStatus.RUNNING,
            CleaningStatus.STOPPING,
        }:
            if kind == "stop":
                self._begin_cleaning_stop(
                    reason=payload.get("message", "用户请求停止清洗。"),
                    aborted=True,
                )
                return
            if kind == "recorder_failed":
                self.interlock.update(recording_ready=False, recorder_failed=True)
                self._begin_cleaning_stop(
                    reason=payload["message"],
                    aborted=False,
                    recovery_required=True,
                )
                return
            if kind == "input_error":
                self._begin_cleaning_stop(
                    reason=payload["message"],
                    aborted=False,
                )
                return
            if kind in {"interlock_changed", "readiness"}:
                _, interlock, _unsafe_latched = self.interlock.read()
                if self._cleaning_phase == "flow_wait_safe":
                    if interlock.safety_state == "SAFE":
                        unsafe = interlock.unsafe_reason()
                        if unsafe:
                            self._begin_cleaning_stop(
                                reason=unsafe,
                                aborted=False,
                            )
                            return
                        if not self.interlock.clear_unsafe_latch():
                            self._begin_cleaning_stop(
                                reason="清洗气流恢复后安全锁存无法清除。",
                                aborted=False,
                            )
                            return
                        self._cancel_cleaning_flow_ready_timeout()
                        self._cleaning_phase = "master_open"
                        self._record_cleaning_event(
                            "flow_ready",
                            "success",
                            "清洗气流已达到 SAFE，允许打开主阀。",
                        )
                        self._submit_cleaning_master(ActuationAction.OPEN)
                        return
                    if (
                        interlock.safety_state in {"LOW_FLOW", "DATA_STALE"}
                        and interlock.connected
                        and interlock.hardware_ready
                        and interlock.flow_setpoints_ready
                    ):
                        return
                unsafe = interlock.unsafe_reason()
                if (
                    interlock.safety_state in {"LOW_FLOW", "DATA_STALE"}
                    and self._cleaning_phase in {"initial_close", "flow_start"}
                    and interlock.connected
                    and interlock.hardware_ready
                    and interlock.flow_setpoints_ready
                ):
                    unsafe = ""
                if unsafe:
                    self._begin_cleaning_stop(
                        reason=unsafe,
                        aborted=False,
                    )
                    return
                return
        if kind in {"interlock_changed", "readiness"} and self._cleaning_plan is not None:
            _, interlock, _ = self.interlock.read()
            if (
                interlock.device_lease == "maintenance"
                and self._cleaning_snapshot.status
                in {
                    CleaningStatus.COMPLETED,
                    CleaningStatus.FAILED,
                    CleaningStatus.RECOVERY_REQUIRED,
                }
            ):
                return
        if kind == "recorder_bind":
            with self._condition:
                accepted = (
                    not payload["cancelled"].is_set()
                    and self._session_recorder is None
                    and self.protocol_state.active_valve is None
                    and not self.protocol_state.possibly_open_valves
                )
                if accepted:
                    self._session_recorder = payload["recorder"]
                    self._session_recorder_sequence = 0
                    self._session_recorder_generation = int(payload["generation"])
                    self._recorder_failure_notified = False
                payload["result"]["accepted"] = accepted
                payload["ack"].set()
            return
        if kind == "recorder_fence":
            accepted = self._emit_recorder_fence()
            if payload.get("ack") is not None:
                payload["result"]["accepted"] = accepted
                payload["ack"].set()
            return
        if kind == "recorder_ready":
            accepted = False
            if (
                self.protocol_state.active_valve is None
                and not self.protocol_state.possibly_open_valves
            ):
                accepted = self.interlock.clear_recorder_failure(
                    payload["generation"]
                )
            if payload.get("ack") is not None:
                payload["result"]["accepted"] = accepted
                payload["ack"].set()
            return
        if kind == "recorder_failed":
            current = self.interlock.read()[1]
            self.interlock.update(
                recording_ready=False,
                recorder_failed=True,
                recorder_generation=current.recorder_generation,
            )
            self.invalidate_execution(
                reason=payload["message"],
                close_all_configured=True,
            )
            if self.protocol_executor is not None:
                event = self.protocol_executor._event(
                    "recording_failed",
                    self._wall_clock(),
                    safety_state=self._current_readiness().safety_state,
                    result="blocked",
                    message=payload["message"],
                )
                self._emit_executor_result(
                    self.protocol_executor._result_with_events([event])
                )
                self._emit_snapshot()
            self._session_recorder = None
            self._session_recorder_generation = 0
            return
        if kind == "valve_plan":
            self._begin_valve_plan(**payload)
            return
        if kind == "flow_intent":
            self._authorize_flow_intent(**payload)
            return
        if kind == "flow_result":
            self._consume_flow_result(payload["flow_result"])
            return
        if kind == "snapshot":
            self._emit_snapshot()
            return
        if kind == "gating_thresholds":
            if self.gating_service is not None:
                self.gating_service.set_thresholds(
                    payload["inhale_threshold"],
                    payload["exhale_threshold"],
                )
            self._emit_snapshot()
            return
        if kind == "ttl_arm_ack":
            self._apply_ttl_arm_ack(**payload)
            self._emit_snapshot()
            return
        if kind == "emergency_close_all":
            self._begin_emergency_close_all()
            return
        if kind == "interlock_changed" and self.protocol_executor is None:
            reason = self.interlock.read()[1].unsafe_reason()
            if reason:
                self.invalidate_execution(reason=reason)
            return
        if self.protocol_executor is None:
            return
        executor = self.protocol_executor
        readiness = payload.get("readiness") or self._current_readiness()
        if kind == "interlock_changed":
            kind = "readiness"
            payload = {
                "readiness": readiness,
                "timestamp": payload.get("timestamp"),
            }
        if kind in {"stop", "pause", "mode", "load"}:
            self._begin_safe_transition(kind, payload)
            return
        if kind == "start":
            previous_epoch = executor.state.execution_epoch
            recording_ready = self.interlock.read()[1].recording_ready
            if not recording_ready:
                result = executor._rejected(
                    "start_rejected",
                    self._wall_clock(),
                    safety_state=readiness.safety_state,
                    message="会话尚未进入记录就绪状态，已拒绝协议启动。",
                )
            else:
                if (
                    not readiness.rejection_reason(
                        has_protocol=bool(
                            payload.get("document") or executor.state.document
                        )
                    )
                    and self.protocol_state.active_valve is None
                    and not self.protocol_state.possibly_open_valves
                ):
                    self.interlock.clear_unsafe_latch()
                result = executor.start(
                    payload["document"],
                    readiness=readiness,
                    timestamp=self._wall_clock(),
                )
                if (
                    executor.state.execution_epoch != previous_epoch
                    and executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
                ):
                    self.metrics.reset()
                    executor.state.quality = self.metrics.snapshot()
        elif kind == "manual_trigger":
            result = executor.accept_trigger(
                "manual",
                readiness=readiness,
                timestamp=self._wall_clock(),
                monotonic_ns=int(self._clock_ns()),
            )
            if any(event.event == "trigger_accepted" for event in result.events):
                self._schedule_breath_timeout(readiness)
        elif kind == "ttl_pulse":
            pulse = payload["pulse"]
            timestamp = getattr(pulse, "timestamp", None)
            arm_epoch = getattr(pulse, "arm_epoch", None)
            pulse_sequence = getattr(pulse, "sequence", None)
            monotonic_ns = getattr(pulse, "monotonic_ns", None)
            valid_timestamp = (
                isinstance(timestamp, int | float)
                and not isinstance(timestamp, bool)
                and math.isfinite(float(timestamp))
            )
            valid_identity = (
                isinstance(arm_epoch, int)
                and not isinstance(arm_epoch, bool)
                and arm_epoch > 0
                and isinstance(pulse_sequence, int)
                and not isinstance(pulse_sequence, bool)
                and pulse_sequence > 0
                and isinstance(monotonic_ns, int)
                and not isinstance(monotonic_ns, bool)
                and monotonic_ns > 0
            )
            if not valid_timestamp or not valid_identity:
                result = executor._rejected(
                    "ttl_pulse_rejected",
                    self._wall_clock(),
                    safety_state=readiness.safety_state,
                    message=(
                        "TTL pulse 的 timestamp/epoch/sequence/monotonic "
                        "identity 无效，已拒绝且未推进 trial。"
                    ),
                    trigger_source="ttl",
                    pulse_sequence=(
                        pulse_sequence
                        if isinstance(pulse_sequence, int)
                        and not isinstance(pulse_sequence, bool)
                        and pulse_sequence > 0
                        else None
                    ),
                )
            else:
                result = executor.accept_trigger(
                    "ttl",
                    readiness=readiness,
                    timestamp=float(timestamp),
                    captured_epoch=arm_epoch,
                    sequence=pulse_sequence,
                    monotonic_ns=monotonic_ns,
                )
                result = replace(
                    result,
                    events=[
                        replace(event, monotonic_ns=monotonic_ns)
                        if event.trigger_source == "ttl"
                        else event
                        for event in result.events
                    ],
                )
            if valid_identity and any(
                event.event == "trigger_accepted" for event in result.events
            ):
                self._schedule_breath_timeout(
                    readiness,
                    origin_ns=monotonic_ns,
                )
        elif kind == "ai_batch":
            generation = self.interlock.read()[0]
            batch = payload["batch"].map_values(self._sample_transform)
            result = executor.process_breath_samples(
                batch,
                safety_state=readiness.safety_state,
                readiness=readiness,
                safety_generation=generation,
            )
            for command in result.action_requests:
                self.submit(command)
        elif kind == "readiness":
            readiness = payload["readiness"]
            reason = readiness.rejection_reason(
                has_protocol=bool(executor.state.document),
                require_ttl=(
                    executor.state.current_mode is not None
                    and executor.state.current_mode.value == "ttl"
                ),
            )
            generation = self.interlock.read()[0]
            interlock_snapshot = self.interlock.read()[1]
            convergence_required = bool(
                interlock_snapshot.has_protocol
                or interlock_snapshot.device_lease != "idle"
                or interlock_snapshot.flow_setpoints_ready
                or self.protocol_state.active_valve is not None
                or self.protocol_state.possibly_open_valves
            )
            if (
                reason
                and convergence_required
                and self._unsafe_close_generation != generation
            ):
                self._unsafe_close_generation = generation
                self.invalidate_execution(
                    reason=f"运行就绪条件丢失：{reason}",
                    close_all_configured=True,
                )
                event = executor._event(
                    "blocked",
                    payload.get("timestamp") or self._wall_clock(),
                    safety_state=readiness.safety_state,
                    result="blocked",
                    message=f"运行就绪条件丢失：{reason} 已安全阻断协议执行。",
                )
                result = executor._result_with_events([event])
            else:
                if not reason:
                    self._unsafe_close_generation = None
                result = executor.handle_readiness_lost(
                    readiness,
                    timestamp=payload.get("timestamp") or self._wall_clock(),
                )
        elif kind == "breath_timeout":
            result = executor.handle_breath_timeout_deadline(
                readiness=payload["readiness"],
                timestamp=self._wall_clock(),
                execution_epoch=payload["execution_epoch"],
                arm_epoch=payload["arm_epoch"],
                trial_index=payload["trial_index"],
                trial_id=payload["trial_id"],
                waiting_started_at=payload["waiting_started_at"],
            )
            if any(event.event == "retry" for event in result.events):
                self._schedule_breath_timeout(readiness)
        elif kind == "input_error":
            if executor.state.status in {
                ProtocolExecutionStatus.WAITING_TRIGGER,
                ProtocolExecutionStatus.WAITING_EXHALE,
                ProtocolExecutionStatus.TRIGGERED,
            }:
                self.invalidate_execution(reason=payload["message"])
                event = executor._event(
                    "ttl_input_error",
                    self._wall_clock(),
                    safety_state=readiness.safety_state,
                    result="blocked",
                    message=f"{payload['message']} 已失效当前 TTL 布防并安全阻断协议执行。",
                )
                result = executor._result_with_events([event])
            else:
                result = executor.handle_input_error(
                    payload["message"],
                    safety_state=readiness.safety_state,
                    timestamp=self._wall_clock(),
                )
        elif kind == "resume":
            result = executor.resume_paused(
                readiness=readiness,
                timestamp=self._wall_clock(),
            )
        elif kind == "rearm":
            if self.protocol_state.active_valve is None and not self.protocol_state.possibly_open_valves:
                self.interlock.clear_unsafe_latch()
            if self.protocol_state.quality_resume_status == ProtocolExecutionStatus.COMPLETED:
                reason = readiness.rejection_reason(has_protocol=bool(self.protocol_state.document))
                if reason:
                    result = executor._rejected(
                        "quality_ack_rejected",
                        self._wall_clock(),
                        safety_state=readiness.safety_state,
                        message=reason,
                    )
                else:
                    self.protocol_state.execution_epoch += 1
                    self.metrics.acknowledge_severe()
                    self.protocol_state.quality = self.metrics.snapshot()
                    self.protocol_state.quality_resume_status = None
                    self.protocol_state.quality_block_reason = ""
                    self.protocol_state.status = ProtocolExecutionStatus.COMPLETED
                    event = ProtocolGateEvent(
                        event="quality_acknowledged",
                        timestamp=self._wall_clock(),
                        safety_state=readiness.safety_state,
                        message="已确认严重时序事件；最后一个 trial 保持完成，不重复暴露。",
                    )
                    self.protocol_state.events.append(event)
                    self.protocol_state.recent_event = event
                    result = executor._result_with_events([event])
            else:
                result = executor.rearm_current(
                    readiness=readiness,
                    timestamp=self._wall_clock(),
                )
                if result.state.status == ProtocolExecutionStatus.WAITING_TRIGGER:
                    self.metrics.acknowledge_severe()
                    self.protocol_state.quality = self.metrics.snapshot()
                    self.protocol_state.quality_resume_status = None
                    self.protocol_state.quality_block_reason = ""
        elif kind == "skip":
            result = executor.skip_current(
                safety_state=readiness.safety_state,
                readiness=readiness,
                timestamp=self._wall_clock(),
                message="用户请求跳过当前 trial，准备下一 trial。",
            )
        else:
            return
        self.protocol_state = executor.state
        self._sync_ttl_request()
        if kind == "start":
            self.start_result_ready.emit(
                ProtocolStartAck(
                    accepted=bool(
                        self.protocol_state.status
                        == ProtocolExecutionStatus.WAITING_TRIGGER
                        and self.protocol_state.execution_epoch > previous_epoch
                    ),
                    lease_epoch=(
                        previous_epoch
                        if payload.get("lease_epoch") is None
                        else int(payload["lease_epoch"])
                    ),
                    previous_epoch=previous_epoch,
                    execution_epoch=self.protocol_state.execution_epoch,
                    status=self.protocol_state.status,
                    message=(result.events[-1].message if result.events else ""),
                )
            )
        self._emit_executor_result(result)
        self._emit_snapshot()

    def _begin_safe_transition(self, kind: str, payload: dict[str, Any]) -> None:
        if self.protocol_executor is None:
            return
        if self._pending_safe_transition is not None:
            previous_kind, previous_payload = self._pending_safe_transition
            if kind == "stop":
                if previous_kind == "load":
                    self.document_result_ready.emit(
                        {
                            "document": previous_payload["document"],
                            "success": False,
                            "message": "停止请求已取代待完成的协议加载。",
                        }
                    )
                self._pending_safe_transition = (kind, payload)
                self._maybe_finalize_safe_transition()
                return
            if kind == "load":
                self.document_result_ready.emit(
                    {
                        "document": payload["document"],
                        "success": False,
                        "message": "另一安全状态转换尚未完成，协议未加载。",
                    }
                )
            elif kind in {"pause", "mode"}:
                event = self.protocol_executor._event(
                    f"{kind}_rejected",
                    self._wall_clock(),
                    safety_state=self._current_readiness().safety_state,
                    result="rejected",
                    message="另一安全状态转换尚未完成，该请求已拒绝。",
                )
                self._emit_executor_result(
                    self.protocol_executor._result_with_events([event])
                )
                self._emit_snapshot()
            return
        state = self.protocol_state
        state.execution_epoch += 1
        state.arm_epoch += 1
        state.pending_open_command_id = None
        state.pending_close_command_id = None
        self.request_ttl_disarm()
        with self._condition:
            cancelled = self._cancel_non_safety_commands_locked(
                reason=f"{kind} 请求已取消尚未执行的普通动作。"
            )
        self._settle_cancelled_receipts(cancelled)
        self._pending_safe_transition = (kind, payload)
        if kind == "stop":
            state.status = ProtocolExecutionStatus.BLOCKED
            self._begin_background_safe_stop(
                reason="协议停止正在执行统一 A=0 → selector → odor → B/C 收敛。",
                close_all_configured=True,
            )
            return
        commands = self._submit_all_configured_closes(
            reason=f"{kind} 请求正在等待所有配置阀门安全关闭确认。",
            prefix=f"{kind}-close",
        )
        self._safe_transition_close_pending = {
            command.command_id for command in commands
        }
        if commands:
            state.status = ProtocolExecutionStatus.BLOCKED
        else:
            self._finalize_safe_transition()

    def confirm_protocol_safe_stop_handoff(
        self,
        identity: SafeStopIdentity,
        success: bool,
    ) -> bool:
        plan = self._background_safe_stop_plan
        if (
            plan is None
            or self._safe_transition_handoff_identity is None
            or plan.identity != identity
            or self._safe_transition_handoff_identity != identity
        ):
            return False
        self._safe_transition_handoff_identity = None
        if not success:
            plan.require_recovery("protocol lease handoff 未确认。")
            self.protocol_state.quality_block_reason = (
                f"RECOVERY_REQUIRED：{plan.recovery_reason}"
            )
            return False
        completed = plan.complete(
            odors_closed=not self.protocol_state.possibly_open_valves,
            owners_handed_off=True,
        )
        self.protocol_state.quality_block_reason = (
            "协议停止已完成统一气路收敛与 lease handoff。"
            if completed
            else f"RECOVERY_REQUIRED：{plan.recovery_reason}"
        )
        if completed:
            self._finalize_safe_transition()
        return completed

    def _maybe_finalize_safe_transition(self) -> None:
        if self._pending_safe_transition is not None and self._pending_safe_transition[0] == "stop":
            plan = self._background_safe_stop_plan
            if plan is not None and not plan.safe_terminal:
                return
        if (
            self._pending_safe_transition is not None
            and not self._safe_transition_close_pending
            and self.protocol_state.active_valve is None
            and not self.protocol_state.possibly_open_valves
        ):
            self._finalize_safe_transition()

    def _abort_safe_transition_after_close_failure(self, receipt: ActuationReceipt) -> None:
        """Keep the old logical state and make the explicit safety action retryable."""
        pending = self._pending_safe_transition
        if pending is None:
            return
        self._pending_safe_transition = None
        self._safe_transition_close_pending.clear()
        kind, payload = pending
        if kind == "load":
            self.document_result_ready.emit(
                {
                    "document": payload["document"],
                    "success": False,
                    "message": receipt.message or "安全关闭失败，未替换当前协议。",
                }
            )
        self._emit_snapshot()

    def _finalize_safe_transition(self) -> None:
        pending = self._pending_safe_transition
        if pending is None or self.protocol_executor is None:
            return
        self._pending_safe_transition = None
        kind, payload = pending
        readiness = self._current_readiness()
        now = self._wall_clock()
        if kind == "stop":
            result = self.protocol_executor.stop(
                safety_state=readiness.safety_state,
                timestamp=now,
                message=payload["message"],
            )
        elif kind == "pause":
            result = self.protocol_executor.pause_after_cleanup(
                safety_state=readiness.safety_state,
                timestamp=now,
            )
        elif kind == "mode":
            # Restore a switchable waiting state only after physical cleanup.
            if self.protocol_state.status == ProtocolExecutionStatus.BLOCKED:
                self.protocol_state.status = ProtocolExecutionStatus.WAITING_TRIGGER
            result = self.protocol_executor.set_trigger_mode(
                payload["mode"], readiness=readiness, timestamp=now
            )
        else:
            retained_quality = self.metrics.snapshot()
            result = self.protocol_executor.reset(payload["document"], timestamp=now)
            self.protocol_executor.state.quality = retained_quality
            self.document_result_ready.emit(
                {
                    "document": payload["document"],
                    "success": self.protocol_executor.state.document is payload["document"],
                }
            )
        self.protocol_state = self.protocol_executor.state
        self._sync_ttl_request()
        self._emit_executor_result(result)
        self._emit_snapshot()

    def _emit_snapshot(self) -> None:
        if self.protocol_executor is None:
            return
        self.snapshot_ready.emit(
            self.protocol_executor.snapshot(
                self._wall_clock(),
                readiness=self._current_readiness(),
                monotonic_ns=self._clock_ns(),
            )
        )

    def _begin_cleaning(
        self,
        *,
        plan: CleaningPlan,
        lease_token: DeviceLeaseToken,
        recorder,
    ) -> None:
        _, interlock, unsafe_latched = self.interlock.read()
        active_protocol = self.protocol_state.status in {
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
            ProtocolExecutionStatus.PAUSED,
        }
        reason = ""
        if (
            lease_token.kind != DeviceLeaseKind.MAINTENANCE
            or lease_token.operation_id != plan.identity.operation_id
            or lease_token.generation != plan.identity.generation
        ):
            reason = "maintenance lease identity 不匹配，清洗未启动。"
        elif recorder is None or not interlock.recording_ready or interlock.recorder_failed:
            reason = "maintenance recorder 尚未就绪，清洗未启动。"
        elif interlock.device_lease != "maintenance":
            reason = "maintenance lease 未发布到动作联锁，清洗未启动。"
        elif active_protocol or self._session_recorder is not None:
            reason = "实验 session/协议仍在活动，清洗未启动。"
        elif (
            not interlock.connected
            or not interlock.hardware_ready
            or interlock.safety_state not in {"SAFE", "LOW_FLOW"}
            or (unsafe_latched and interlock.safety_state != "LOW_FLOW")
        ):
            reason = "硬件或气流安全联锁未就绪，清洗未启动。"
        elif self.valve_service is None:
            reason = "阀门目标服务不可用，清洗未启动。"
        if reason:
            self._cleaning_plan = plan
            self._cleaning_lease_token = lease_token
            self._maintenance_recorder = recorder
            self._fail_cleaning_before_open(reason)
            return

        self._cleaning_plan = plan
        self._cleaning_lease_token = lease_token
        self._maintenance_recorder = recorder
        self._maintenance_recorder_sequence = 0
        self._cleaning_phase = "initial_close"
        self._cleaning_step_index = 0
        self._cleaning_expected.clear()
        self._cleaning_receipts.clear()
        self._cleaning_possibly_open.clear()
        self._cleaning_pending_flow_id = None
        self._cleaning_pending_flow_command = None
        self._cleaning_failure_reason = ""
        close_steps = self._all_configured_close_steps()
        if not close_steps:
            self._fail_cleaning_before_open("没有可审计的已配置 DO 目标，清洗未启动。")
            return
        self._publish_cleaning_snapshot(
            status=CleaningStatus.PREPARING,
            identity=plan.identity,
            lease_held=True,
            recording_ready=True,
            close_confirmed=0,
            close_required=len(close_steps),
            flow_zero_confirmed=False,
            selector_safe_confirmed=False,
            possibly_open=(),
            recovery_reason="",
        )
        if not self._record_cleaning_event(
            "cleaning_preparing",
            "success",
            "maintenance lease 与 recorder 已就绪，开始初始全关。",
        ):
            self._fail_cleaning_before_open("maintenance 初始事件无法进入记录队列。")
            return
        for ordinal, step in enumerate(close_steps, 1):
            self._submit_cleaning_target_command(
                command_id=(
                    f"{plan.identity.operation_id}:{plan.identity.generation}:"
                    f"initial-close:{ordinal}"
                ),
                step_id=f"initial-close-{ordinal}",
                valve=step.logical_valve,
                device=step.device,
                line=step.line,
                action=ActuationAction.CLOSE,
                category=ActuationCategory.SAFETY,
                role="initial_close",
            )

    def _submit_cleaning_target_command(
        self,
        *,
        command_id: str,
        step_id: str,
        valve: int,
        device: str | None,
        line: str,
        action: ActuationAction,
        category: ActuationCategory,
        role: str,
        duration_ns: int | None = None,
        expected_ns: int | None = None,
    ) -> bool:
        plan = self._cleaning_plan
        if plan is None:
            return False
        self._sequence += 1
        command = ActuationCommand(
            command_id=command_id,
            execution_epoch=0,
            arm_epoch=0,
            sequence=self._sequence,
            trial_id=None,
            trial_index=None,
            valve=int(valve),
            action=action,
            category=category,
            expected_ns=(
                int(self._clock_ns()) if expected_ns is None else int(expected_ns)
            ),
            duration_ns=duration_ns,
            wall_timestamp=float(self._wall_clock()),
            safety_generation=self.interlock.read()[0],
            target_device=device,
            target_line=line,
            operation_id=plan.identity.operation_id,
            generation=plan.identity.generation,
            step_id=step_id,
            action_kind=action,
        )
        self._cleaning_expected[command_id] = {
            "role": role,
            "command": command,
        }
        if role == "selector_safe":
            self._safe_stop_selector_deadlines[command_id] = (
                int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns
            )
        if not self.submit(command):
            self._cleaning_expected.pop(command_id, None)
            self._begin_cleaning_stop(
                reason=f"清洗命令无法进入动作队列：{command_id}",
                aborted=False,
                recovery_required=(action == ActuationAction.CLOSE),
            )
            return False
        return True

    def _submit_cleaning_plan_step(self, index: int) -> bool:
        plan = self._cleaning_plan
        if plan is None or index < 0 or index >= len(plan.steps):
            return False
        step = plan.steps[index]
        device, line = step.target.split("/", 1)
        self._cleaning_step_index = index
        self._publish_cleaning_snapshot(
            current_step_id=step.step_id,
            current_channel=step.channel,
            remaining_ns=step.duration_ns or 0,
        )
        if not self._record_cleaning_event(
            "step_started",
            "success",
            f"开始清洗步骤 {step.step_id}。",
            {
                "step_id": step.step_id,
                "command_id": step.command_id,
                "target": step.target,
                "action_kind": step.action_kind.value,
                "cycle_index": step.cycle_index,
            },
        ):
            self._begin_cleaning_stop(
                reason="maintenance recorder 拒绝 step start，已在开阀前安全停止。",
                aborted=False,
            )
            return False
        return self._submit_cleaning_target_command(
            command_id=step.command_id,
            step_id=step.step_id,
            valve=step.channel,
            device=device,
            line=line,
            action=step.action_kind,
            category=ActuationCategory.CLEANING,
            role=(
                "step_open"
                if step.action_kind == ActuationAction.OPEN
                else "step_close"
            ),
            duration_ns=step.duration_ns,
        )

    def _submit_cleaning_master(self, action: ActuationAction) -> bool:
        plan = self._cleaning_plan
        if plan is None or self.valve_service is None:
            self._begin_cleaning_stop(
                reason="清洗 selector 目标未配置。",
                aborted=False,
                recovery_required=True,
            )
            return False
        selector = getattr(self.valve_service, "selector", None)
        route = (
            SelectorRoute.ODOR
            if action == ActuationAction.OPEN
            else getattr(selector, "safe_route", SelectorRoute.COMPENSATION)
        )
        try:
            step = self.valve_service.selector_route_step(route)
        except ValueError as exc:
            self._finish_cleaning_recovery(str(exc))
            return False
        role = "selector_odor" if route == SelectorRoute.ODOR else "selector_safe"
        effective_action = ActuationAction.OPEN if step.state else ActuationAction.CLOSE
        return self._submit_cleaning_target_command(
            command_id=(
                f"{plan.identity.operation_id}:{plan.identity.generation}:"
                f"{role}:{self._sequence + 1}"
            ),
            step_id=role,
            valve=0,
            device=step.device,
            line=step.line,
            action=effective_action,
            category=(
                ActuationCategory.CLEANING
                if route == SelectorRoute.ODOR
                else ActuationCategory.SAFETY
            ),
            role=role,
        )

    def _submit_cleaning_flow(self, *, zero: bool) -> bool:
        plan = self._cleaning_plan
        token = self._cleaning_lease_token
        if plan is None or token is None or self._flow_submitter is None:
            self._finish_cleaning_recovery("清洗 flow owner 不可用。")
            return False
        self._sequence += 1
        values = dict(plan.flow_setpoints_sccm)
        command = FlowCommand(
            command_id=(
                f"{plan.identity.operation_id}:{plan.identity.generation}:"
                f"{'flow-zero' if zero else 'flow-start'}:{self._sequence}"
            ),
            execution_epoch=0,
            sequence=self._sequence,
            mode="zero" if zero else "cleaning",
            a=0.0 if zero else values["A"],
            b=0.0 if zero else values["B"],
            c=0.0 if zero else values["C"],
            source="cleaning",
            operation_id=plan.identity.operation_id,
            generation=plan.identity.generation,
            lease_token=token.token,
        )
        self._cleaning_pending_flow_id = command.command_id
        self._cleaning_pending_flow_command = command
        self._cleaning_phase = "flow_zero" if zero else "flow_start"
        if self._flow_submitter(command) is False:
            self._cleaning_pending_flow_id = None
            self._cleaning_pending_flow_command = None
            if zero:
                self._recover_cleaning_without_selector(
                    "A/B/C 清零命令未被 FlowWorker 接受。"
                )
            else:
                self._begin_cleaning_stop(
                    reason="清洗流量命令未被 FlowWorker 接受。",
                    aborted=False,
                )
            return False
        self._sequence += 1
        heapq.heappush(
            self._deadline_heap,
            (
                int(self._clock_ns()) + self._safe_stop_receipt_timeout_ns,
                5,
                self._sequence,
                "cleaning_flow_receipt_timeout",
                {
                    "operation_id": plan.identity.operation_id,
                    "command_id": command.command_id,
                    "zero": bool(zero),
                },
            ),
        )
        return True

    def _is_cleaning_flow_result(self, wrapped: FlowCommandResult) -> bool:
        plan = self._cleaning_plan
        return bool(
            plan is not None
            and wrapped.command.operation_id == plan.identity.operation_id
        )

    def _handle_cleaning_flow_receipt_timeout(
        self,
        *,
        operation_id: str,
        command_id: str,
        zero: bool,
    ) -> None:
        plan = self._cleaning_plan
        if (
            plan is None
            or plan.identity.operation_id != operation_id
            or self._cleaning_pending_flow_id != command_id
        ):
            return
        self._cleaning_pending_flow_id = None
        self._cleaning_pending_flow_command = None
        if zero:
            self._recover_cleaning_without_selector(
                "A 清零 receipt 超时，selector 未切换；需要显式恢复。"
            )
            return
        self._begin_cleaning_stop(
            reason="清洗流量 receipt 超时，已请求安全清零。",
            aborted=False,
        )

    def _consume_cleaning_flow_result(self, wrapped: FlowCommandResult) -> None:
        plan = self._cleaning_plan
        if plan is None:
            return
        expected_command = self._cleaning_pending_flow_command
        if expected_command is None or wrapped.command != expected_command:
            conflicted_phase = self._cleaning_phase
            self._record_cleaning_event(
                "stale_flow_receipt",
                "recovery_required",
                "陈旧或冲突 flow receipt 未推进清洗状态。",
                {"command_id": wrapped.command.command_id},
            )
            self._cleaning_pending_flow_id = None
            self._cleaning_pending_flow_command = None
            if conflicted_phase == "flow_start":
                self._begin_cleaning_stop(
                    reason=(
                        "清洗流量 receipt 迟到或身份冲突（完整 identity），"
                        "已请求 A/B/C 清零后进入恢复态。"
                    ),
                    aborted=False,
                    recovery_required=True,
                )
            else:
                self._recover_cleaning_without_selector(
                    "A 清零 receipt 迟到或身份冲突（完整 identity），安全停止证据已作废。"
                )
            return
        self._cleaning_pending_flow_id = None
        self._cleaning_pending_flow_command = None
        if (
            not wrapped.result.success
            or wrapped.stale
            or (
                self._cleaning_phase == "flow_zero"
                and any(
                    not math.isfinite(float(value)) or abs(float(value)) > 1e-9
                    for value in (
                        wrapped.result.a,
                        wrapped.result.b,
                        wrapped.result.c,
                    )
                )
            )
        ):
            if self._cleaning_phase == "flow_zero":
                self._recover_cleaning_without_selector(
                    wrapped.result.message or "A/B/C 清零回执未确认。"
                )
            else:
                self._begin_cleaning_stop(
                    reason=wrapped.result.message or "清洗流量回执失败。",
                    aborted=False,
                )
            return
        if self._cleaning_phase == "flow_start":
            _, interlock, _unsafe_latched = self.interlock.read()
            if interlock.safety_state == "SAFE":
                if not self.interlock.clear_unsafe_latch():
                    self._begin_cleaning_stop(
                        reason="清洗流量回执成功，但安全锁存无法清除。",
                        aborted=False,
                    )
                    return
                self._cleaning_phase = "master_open"
                self._submit_cleaning_master(ActuationAction.OPEN)
            elif interlock.safety_state in {"LOW_FLOW", "DATA_STALE"}:
                self._cleaning_phase = "flow_wait_safe"
                self._sequence += 1
                heapq.heappush(
                    self._deadline_heap,
                    (
                        int(self._clock_ns()) + self._cleaning_flow_ready_timeout_ns,
                        10,
                        self._sequence,
                        "cleaning_flow_ready_timeout",
                        {
                            "operation_id": plan.identity.operation_id,
                            "generation": plan.identity.generation,
                        },
                    ),
                )
                self._record_cleaning_event(
                    "flow_wait_safe",
                    "waiting",
                    "清洗 setpoint 已确认，等待实际气流达到 SAFE 后再打开主阀。",
                )
            else:
                self._begin_cleaning_stop(
                    reason=f"清洗流量设置后安全状态为 {interlock.safety_state}。",
                    aborted=False,
                )
            return
        if self._cleaning_phase == "flow_zero":
            self._publish_cleaning_snapshot(flow_zero_confirmed=True)
            self._cleaning_phase = "selector_safe"
            self._submit_cleaning_master(ActuationAction.CLOSE)

    def _consume_cleaning_receipt(self, receipt: ActuationReceipt) -> None:
        previous = self._cleaning_receipts.get(receipt.command_id)
        if previous is not None:
            qualifier = "内容冲突" if previous != receipt else "重复投递"
            reason = f"清洗 receipt {qualifier}：{receipt.command_id}"
            if self._cleaning_snapshot.status == CleaningStatus.STOPPING:
                self._finish_cleaning_recovery(reason)
            else:
                self._begin_cleaning_stop(
                    reason=reason,
                    aborted=False,
                    recovery_required=True,
                )
            return
        self._cleaning_receipts[receipt.command_id] = receipt
        self._remember_receipt(receipt)
        expected = self._cleaning_expected.get(receipt.command_id)
        if not self._record_cleaning_receipt(receipt):
            self._begin_cleaning_stop(
                reason="maintenance recorder 拒绝动作回执。",
                aborted=False,
                recovery_required=True,
            )
            return
        self.receipt_ready.emit(receipt)
        if expected is None:
            self._handle_late_cleaning_receipt(receipt)
            self._retire_command(receipt.command_id)
            return
        identity_matches = self._receipt_matches_command(
            receipt,
            expected["command"],
        )
        if not identity_matches:
            self._cleaning_expected.pop(receipt.command_id, None)
            reason = f"清洗 receipt identity 冲突：{receipt.command_id}"
            if self._cleaning_snapshot.status == CleaningStatus.STOPPING:
                self._finish_cleaning_recovery(reason)
            else:
                self._begin_cleaning_stop(
                    reason=reason,
                    aborted=False,
                    recovery_required=True,
                )
            self._retire_command(receipt.command_id)
            return
        self._cleaning_expected.pop(receipt.command_id, None)
        role = expected["role"]
        if self.valve_service is not None:
            self.valve_service.commit_receipt(receipt)
        if receipt.result != ActuationResult.SUCCESS:
            if receipt.valve != 0 and (
                receipt.action == ActuationAction.OPEN or receipt.target
            ):
                if receipt.target:
                    self._cleaning_possibly_open.add(receipt.target)
                    self._publish_cleaning_snapshot(
                        possibly_open=tuple(sorted(self._cleaning_possibly_open))
                    )
            if role == "selector_safe":
                self._recover_cleaning_without_selector(
                    f"selector 安全路线回执失败：{receipt.result.value}"
                )
                self._retire_command(receipt.command_id)
                return
            if role in {"stop_close", "late_close"}:
                self._cleaning_terminal_status = CleaningStatus.RECOVERY_REQUIRED
                self._cleaning_failure_reason = (
                    f"清洗安全关闭回执失败：{receipt.target or receipt.command_id}"
                )
                if not any(
                    item["role"] in {"stop_close", "late_close"}
                    for item in self._cleaning_expected.values()
                ):
                    self._finish_cleaning_recovery(self._cleaning_failure_reason)
                self._retire_command(receipt.command_id)
                return
            self._begin_cleaning_stop(
                reason=f"清洗 {role} 回执失败：{receipt.result.value}",
                aborted=False,
                recovery_required=(receipt.action == ActuationAction.CLOSE),
            )
            self._retire_command(receipt.command_id)
            return
        if receipt.action == ActuationAction.CLOSE and receipt.target:
            self._cleaning_possibly_open.discard(receipt.target)
        if role == "initial_close":
            confirmed = self._cleaning_snapshot.close_confirmed + 1
            self._publish_cleaning_snapshot(close_confirmed=confirmed)
            if not any(
                item["role"] == "initial_close"
                for item in self._cleaning_expected.values()
            ):
                self._submit_cleaning_flow(zero=False)
        elif role == "selector_odor":
            self._cleaning_phase = "running"
            self._publish_cleaning_snapshot(status=CleaningStatus.RUNNING)
            self._record_cleaning_event(
                "cleaning_running",
                "success",
                "清洗流量与主阀已确认，开始单通道序列。",
            )
            self._submit_cleaning_plan_step(0)
        elif role == "step_open":
            if receipt.target:
                self._cleaning_possibly_open.add(receipt.target)
                self._publish_cleaning_snapshot(
                    possibly_open=tuple(sorted(self._cleaning_possibly_open))
                )
            step = self._cleaning_plan.steps[self._cleaning_step_index]
            if receipt.actual_ns is None or step.duration_ns is None:
                self._begin_cleaning_stop(
                    reason="清洗 open receipt 缺少 monotonic deadline 基准。",
                    aborted=False,
                    recovery_required=True,
                )
            else:
                self._sequence += 1
                heapq.heappush(
                    self._deadline_heap,
                    (
                        receipt.actual_ns + step.duration_ns,
                        15,
                        self._sequence,
                        "cleaning_deadline",
                        {
                            "operation_id": receipt.operation_id,
                            "generation": receipt.generation,
                            "open_command_id": receipt.command_id,
                            "close_step_index": self._cleaning_step_index + 1,
                        },
                    ),
                )
        elif role == "step_close":
            next_index = self._cleaning_step_index + 1
            self._record_cleaning_event(
                "step_completed",
                "success",
                f"清洗步骤 {receipt.step_id} 已确认。",
                {"step_id": receipt.step_id, "target": receipt.target},
            )
            if next_index >= len(self._cleaning_plan.steps):
                self._cleaning_phase = "flow_zero"
                self._cleaning_terminal_status = CleaningStatus.COMPLETED
                self._cleaning_stop_outcome = CleaningOutcome.COMPLETED
                self._submit_cleaning_flow(zero=True)
            else:
                self._submit_cleaning_plan_step(next_index)
        elif role == "selector_safe":
            self._publish_cleaning_snapshot(selector_safe_confirmed=True)
            self._cleaning_phase = "stopping_close"
            self._submit_cleaning_odor_closes()
        elif role in {"stop_close", "late_close"}:
            self._publish_cleaning_snapshot(
                close_confirmed=self._cleaning_snapshot.close_confirmed + 1,
                possibly_open=tuple(sorted(self._cleaning_possibly_open)),
            )
            if not any(
                item["role"] in {"stop_close", "late_close"}
                for item in self._cleaning_expected.values()
            ):
                self._finish_cleaning_after_safe_closes()
        self._retire_command(receipt.command_id)

    def _handle_cleaning_deadline(
        self,
        *,
        operation_id: str,
        generation: int,
        open_command_id: str,
        close_step_index: int,
    ) -> None:
        plan = self._cleaning_plan
        if (
            plan is None
            or self._cleaning_snapshot.status != CleaningStatus.RUNNING
            or operation_id != plan.identity.operation_id
            or generation != plan.identity.generation
            or close_step_index >= len(plan.steps)
            or self._cleaning_step_index + 1 != close_step_index
        ):
            self._record_cleaning_event(
                "stale_deadline",
                "ignored",
                "陈旧清洗 deadline 未推进状态。",
                {"open_command_id": open_command_id},
            )
            return
        self._submit_cleaning_plan_step(close_step_index)

    def _handle_cleaning_flow_ready_timeout(
        self,
        *,
        operation_id: str,
        generation: int,
    ) -> None:
        plan = self._cleaning_plan
        if (
            plan is None
            or self._cleaning_phase != "flow_wait_safe"
            or operation_id != plan.identity.operation_id
            or generation != plan.identity.generation
        ):
            return
        self._begin_cleaning_stop(
            reason="清洗 setpoint 已确认，但实际气流未在限定时间内达到 SAFE。",
            aborted=False,
        )

    def _cancel_cleaning_flow_ready_timeout(self) -> None:
        with self._condition:
            self._deadline_heap = [
                item
                for item in self._deadline_heap
                if item[3] != "cleaning_flow_ready_timeout"
            ]
            heapq.heapify(self._deadline_heap)

    def _handle_late_cleaning_receipt(self, receipt: ActuationReceipt) -> None:
        if (
            receipt.action == ActuationAction.OPEN
            and receipt.result == ActuationResult.SUCCESS
            and receipt.target
        ):
            self._cleaning_possibly_open.add(receipt.target)
            self._publish_cleaning_snapshot(
                possibly_open=tuple(sorted(self._cleaning_possibly_open))
            )
            if self._cleaning_snapshot.status == CleaningStatus.STOPPING:
                device, line = receipt.target.split("/", 1)
                self._submit_cleaning_target_command(
                    command_id=(
                        f"{receipt.operation_id}:{receipt.generation}:"
                        f"late-close:{self._sequence + 1}"
                    ),
                    step_id=f"late-close-{self._sequence + 1}",
                    valve=receipt.valve,
                    device=device,
                    line=line,
                    action=ActuationAction.CLOSE,
                    category=ActuationCategory.SAFETY,
                    role="late_close",
                )
            else:
                self._begin_cleaning_stop(
                    reason="收到 late successful open，已请求匹配目标安全关闭。",
                    aborted=False,
                    recovery_required=True,
                )
        elif (
            receipt.action == ActuationAction.CLOSE
            and receipt.result == ActuationResult.SUCCESS
            and receipt.target
        ):
            self._cleaning_possibly_open.discard(receipt.target)
            self._publish_cleaning_snapshot(
                possibly_open=tuple(sorted(self._cleaning_possibly_open))
            )

    def _begin_cleaning_stop(
        self,
        *,
        reason: str,
        aborted: bool,
        recovery_required: bool = False,
    ) -> None:
        plan = self._cleaning_plan
        if plan is None:
            return
        if self._cleaning_snapshot.status in {
            CleaningStatus.COMPLETED,
            CleaningStatus.RECOVERY_REQUIRED,
            CleaningStatus.STOPPING,
        }:
            return
        self._cleaning_failure_reason = str(reason)
        self._cleaning_stop_outcome = (
            CleaningOutcome.ABORTED if aborted else CleaningOutcome.FAILED
        )
        self._cleaning_terminal_status = (
            CleaningStatus.RECOVERY_REQUIRED
            if recovery_required
            else CleaningStatus.COMPLETED
            if aborted
            else CleaningStatus.FAILED
        )
        self._cleaning_phase = "flow_zero"
        with self._condition:
            kept = []
            while self._normal_heap:
                item = heapq.heappop(self._normal_heap)
                command = item[3]
                if command.operation_id == plan.identity.operation_id:
                    self._cleaning_expected.pop(command.command_id, None)
                    self._retire_command(command.command_id)
                else:
                    kept.append(item)
            for item in kept:
                heapq.heappush(self._normal_heap, item)
            self._deadline_heap = [
                item
                for item in self._deadline_heap
                if item[3]
                not in {
                    "cleaning_deadline",
                    "cleaning_flow_ready_timeout",
                    "cleaning_flow_receipt_timeout",
                }
            ]
            heapq.heapify(self._deadline_heap)
            for command_id in tuple(self._cleaning_expected):
                self._cleaning_expected.pop(command_id, None)
        self._publish_cleaning_snapshot(
            status=CleaningStatus.STOPPING,
            recovery_reason=str(reason),
            close_confirmed=0,
            flow_zero_confirmed=False,
            selector_safe_confirmed=False,
        )
        self._record_cleaning_event(
            "cleaning_stopping",
            "aborted" if aborted else "failed",
            str(reason),
        )
        close_steps = self._all_configured_close_steps()
        self._publish_cleaning_snapshot(close_required=len(close_steps))
        # SafeStopPlan invariant: flow-zero receipt is the hard prerequisite
        # for routing the selector to compensation.  Odor closes follow it.
        self.protocol_state.execution_epoch += 1
        self.protocol_state.arm_epoch += 1
        self._submit_cleaning_flow(zero=True)

    def _fail_cleaning_before_open(self, reason: str) -> None:
        identity = None if self._cleaning_plan is None else self._cleaning_plan.identity
        self._cleaning_phase = "failed"
        self._publish_cleaning_snapshot(
            status=CleaningStatus.FAILED,
            identity=identity,
            lease_held=self._cleaning_lease_token is not None,
            recording_ready=False,
            recovery_reason=str(reason),
        )
        if identity is not None:
            self.cleaning_result_ready.emit(
                CleaningResult(
                    identity=identity,
                    status=CleaningStatus.FAILED,
                    outcome=CleaningOutcome.FAILED,
                    reason=str(reason),
                )
            )

    def _finish_cleaning_recovery(self, reason: str) -> None:
        self._complete_cleaning(
            status=CleaningStatus.RECOVERY_REQUIRED,
            outcome=CleaningOutcome.FAILED,
            reason=str(reason),
        )

    def _retry_cleaning_recovery(self) -> None:
        plan = self._cleaning_plan
        if plan is None or self.valve_service is None:
            return
        self._cleaning_terminal_status = CleaningStatus.RECOVERY_REQUIRED
        self._cleaning_stop_outcome = CleaningOutcome.FAILED
        self._cleaning_phase = "flow_zero"
        self._cleaning_expected.clear()
        self._cleaning_pending_flow_id = None
        self._cleaning_pending_flow_command = None
        self._publish_cleaning_snapshot(
            status=CleaningStatus.STOPPING,
            close_confirmed=0,
            recovery_reason="用户已显式触发安全恢复。",
            flow_zero_confirmed=False,
            selector_safe_confirmed=False,
        )
        close_steps = self._all_configured_close_steps()
        self._publish_cleaning_snapshot(close_required=len(close_steps))
        self.protocol_state.execution_epoch += 1
        self.protocol_state.arm_epoch += 1
        self._submit_cleaning_flow(zero=True)

    def _recover_cleaning_without_selector(self, reason: str) -> None:
        """Keep selector unchanged, but still converge every configured odor line."""

        self._cleaning_terminal_status = CleaningStatus.RECOVERY_REQUIRED
        self._cleaning_stop_outcome = CleaningOutcome.FAILED
        self._cleaning_failure_reason = str(reason)
        self._cleaning_phase = "stopping_close"
        self._publish_cleaning_snapshot(
            status=CleaningStatus.STOPPING,
            recovery_reason=str(reason),
            flow_zero_confirmed=False,
            selector_safe_confirmed=False,
        )
        self._submit_cleaning_odor_closes()

    def _submit_cleaning_odor_closes(self) -> None:
        plan = self._cleaning_plan
        if plan is None:
            return
        close_steps = self._all_configured_close_steps()
        if not close_steps:
            self._finish_cleaning_recovery("无法构造清洗气味阀安全关闭目标集。")
            return
        for ordinal, step in enumerate(close_steps, 1):
            self._submit_cleaning_target_command(
                command_id=(
                    f"{plan.identity.operation_id}:{plan.identity.generation}:"
                    f"stop-close:{self._sequence}:{ordinal}"
                ),
                step_id=f"stop-close-{ordinal}",
                valve=step.logical_valve,
                device=step.device,
                line=step.line,
                action=ActuationAction.CLOSE,
                category=ActuationCategory.SAFETY,
                role="stop_close",
            )

    def _finish_cleaning_after_safe_closes(self) -> None:
        if self._cleaning_possibly_open:
            self._finish_cleaning_recovery(
                "气味阀关闭状态仍不确定，清洗需要显式恢复。"
            )
            return
        if self._cleaning_terminal_status == CleaningStatus.COMPLETED:
            self._complete_cleaning(
                status=CleaningStatus.COMPLETED,
                outcome=self._cleaning_stop_outcome,
                reason=self._cleaning_failure_reason,
            )
        elif self._cleaning_terminal_status == CleaningStatus.FAILED:
            self._complete_cleaning(
                status=CleaningStatus.FAILED,
                outcome=CleaningOutcome.FAILED,
                reason=self._cleaning_failure_reason,
            )
        else:
            self._finish_cleaning_recovery(self._cleaning_failure_reason)

    def _complete_cleaning(
        self,
        *,
        status: CleaningStatus,
        outcome: CleaningOutcome,
        reason: str,
    ) -> None:
        plan = self._cleaning_plan
        if plan is None:
            return
        self._cleaning_phase = "terminal"
        self._publish_cleaning_snapshot(
            status=status,
            current_step_id=None,
            current_channel=None,
            remaining_ns=0,
            possibly_open=tuple(sorted(self._cleaning_possibly_open)),
            recovery_reason=str(reason),
        )
        result = CleaningResult(
            identity=plan.identity,
            status=status,
            outcome=outcome,
            reason=str(reason),
        )
        self.cleaning_result_ready.emit(result)

    def _publish_cleaning_snapshot(self, **changes: Any) -> None:
        self._cleaning_snapshot = replace(self._cleaning_snapshot, **changes)
        self.cleaning_snapshot_ready.emit(self._cleaning_snapshot)

    def _record_cleaning_receipt(self, receipt: ActuationReceipt) -> bool:
        recorder = self._maintenance_recorder
        if recorder is None:
            return False
        self._maintenance_recorder_sequence += 1
        return bool(
            recorder.post_receipt(
                receipt,
                producer_sequence=self._maintenance_recorder_sequence,
            )
        )

    def _record_cleaning_event(
        self,
        event: str,
        result: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        recorder = self._maintenance_recorder
        if recorder is None:
            return False
        self._maintenance_recorder_sequence += 1
        return bool(
            recorder.post_event(
                producer="actuation",
                producer_sequence=self._maintenance_recorder_sequence,
                record_type="cleaning_event",
                event=str(event),
                result=str(result),
                message=str(message),
                payload=payload or {},
                timestamp=float(self._wall_clock()),
                monotonic_ns=int(self._clock_ns()),
            )
        )

    def _all_configured_close_steps(self):
        if self.valve_service is None:
            return ()
        resolver = getattr(self.valve_service, "all_configured_close_steps", None)
        if callable(resolver):
            return tuple(resolver())
        return tuple(self.valve_service.emergency_close_steps())

    def _authorize_flow_intent(
        self,
        *,
        mode: str,
        a: float,
        b: float,
        c: float,
        source: str,
    ) -> None:
        self._sequence += 1
        command = FlowCommand(
            command_id=f"flow-{self.protocol_state.execution_epoch}-{self._sequence}",
            execution_epoch=self.protocol_state.execution_epoch,
            sequence=self._sequence,
            mode=str(mode),
            a=float(a),
            b=float(b),
            c=float(c),
            source=str(source),
        )
        _, snapshot, _ = self.interlock.read()
        reason = ""
        if snapshot.device_lease != "idle":
            reason = f"{snapshot.device_lease} 设备租约已占用，已拒绝流量变更。"
        else:
            # An idle MFC command is precisely how LOW_FLOW / setpoint-not-ready
            # is recovered, so those two states must not reject themselves.
            if not snapshot.connected:
                reason = "硬件连接已断开，流量未更改。"
            elif not snapshot.hardware_ready:
                reason = "硬件自检状态已失效，流量未更改。"
            elif snapshot.safety_state not in {"SAFE", "LOW_FLOW"}:
                reason = f"安全状态为 {snapshot.safety_state}，流量未更改。"
        submitted = None if reason or self._flow_submitter is None else self._flow_submitter(command)
        if reason or self._flow_submitter is None or submitted is False:
            message = reason or "串口动作队列不可用，流量未更改。"
            result = FlowApplyResult(False, message, a, b, c, a + c, "rejected")
            self.flow_result_ready.emit(FlowCommandResult(command=command, result=result))

    def _consume_flow_result(self, wrapped: FlowCommandResult) -> None:
        if wrapped.command.execution_epoch != self.protocol_state.execution_epoch:
            # The external correlation owner still needs the terminal result to
            # retire its pending command.  A stale result must not mutate the
            # current epoch's interlock or readiness.
            self.flow_result_ready.emit(replace(wrapped, stale=True))
            return
        self.interlock.update(flow_setpoints_ready=bool(wrapped.result.success))
        if not wrapped.result.success:
            self.invalidate_execution(reason=wrapped.result.message)
        elif self.protocol_state.active_valve is None and not self.protocol_state.possibly_open_valves:
            self.interlock.clear_unsafe_latch()
        self.flow_result_ready.emit(wrapped)

    def _sync_ttl_request(self) -> None:
        state = self.protocol_state
        should_arm = bool(
            state.status == ProtocolExecutionStatus.WAITING_TRIGGER
            and state.current_mode is not None
            and state.current_mode.value == "ttl"
        )
        if should_arm:
            if self._pending_ttl_arm_epoch != state.arm_epoch and not state.ttl_armed:
                self.request_ttl_arm(arm_epoch=state.arm_epoch)
        elif (
            self._pending_ttl_arm_epoch is not None
            or self._ttl_hardware_armed_epoch is not None
            or state.ttl_armed
        ):
            self.request_ttl_disarm()

    def _current_readiness(self):
        from app.models import ProtocolExecutionReadiness

        _, snapshot, _ = self.interlock.read()
        return ProtocolExecutionReadiness(
            connected=snapshot.connected,
            hardware_ready=snapshot.hardware_ready,
            flow_setpoints_ready=snapshot.flow_setpoints_ready,
            safety_state=snapshot.safety_state,
            ttl_input_ready=snapshot.ttl_input_ready,
        )

    def _begin_valve_plan(self, *, plan, category: ActuationCategory, request_id: str) -> None:
        if request_id in self._plan_contexts:
            return
        self._plan_contexts[request_id] = {
            "plan": plan,
            "category": category,
            "next_index": 0,
            "opened_commands": [],
            "rolling_back": False,
            "rollback_failed": False,
        }
        self._enqueue_next_plan_step(request_id)

    def _enqueue_next_plan_step(self, request_id: str) -> None:
        context = self._plan_contexts.get(request_id)
        if context is None:
            return
        plan = context["plan"]
        index = context["next_index"]
        if index >= len(plan.steps):
            self._plan_contexts.pop(request_id, None)
            self.plan_result_ready.emit(
                {"request_id": request_id, "success": True, "message": "阀门动作已完成。"}
            )
            return
        step = plan.steps[index]
        context["next_index"] = index + 1
        self._sequence += 1
        command = ActuationCommand(
            command_id=f"{request_id}-{self._sequence}",
            # Plans may run before the first protocol arm (whose state epoch is
            # still zero), but persisted actuation evidence requires a positive
            # execution epoch.  Command ids remain the canonical discriminator
            # when the subsequent protocol arm also advances to epoch one.
            execution_epoch=max(1, self.protocol_state.execution_epoch),
            arm_epoch=self.protocol_state.arm_epoch,
            sequence=self._sequence,
            trial_id=None,
            trial_index=None,
            valve=step.logical_valve,
            action=ActuationAction.OPEN if step.state else ActuationAction.CLOSE,
            category=context["category"],
            expected_ns=int(self._clock_ns()),
            duration_ns=None,
            wall_timestamp=float(self._wall_clock()),
            safety_generation=self.interlock.read()[0],
            target_device=step.device,
            target_line=step.line,
            operation_id=request_id if step.logical_valve == 0 else None,
            generation=(
                self.protocol_state.execution_epoch
                if step.logical_valve == 0
                else None
            ),
            step_id="selector_odor" if step.logical_valve == 0 else None,
            action_kind=(
                ActuationAction.OPEN if step.state else ActuationAction.CLOSE
            )
            if step.logical_valve == 0
            else None,
        )
        self._plan_by_command[command.command_id] = request_id
        if not self.submit(command):
            self._plan_by_command.pop(command.command_id, None)
            self._fail_plan(
                request_id,
                "动作队列繁忙或设备租约冲突，阀门计划未执行。",
            )

    def _handle_plan_receipt(self, receipt: ActuationReceipt) -> None:
        request_id = self._plan_by_command.pop(receipt.command_id, None)
        if request_id is None:
            return
        if self.valve_service is not None:
            self.valve_service.commit_receipt(receipt)
        context = self._plan_contexts.get(request_id)
        if context is None:
            return
        if context["rolling_back"]:
            if receipt.result != ActuationResult.SUCCESS:
                context["rollback_failed"] = True
            self._enqueue_next_plan_rollback(request_id)
            return
        if receipt.result != ActuationResult.SUCCESS:
            self._fail_plan(
                request_id,
                receipt.message or "阀门写入失败。",
                uncertain_command=(
                    self._commands_by_id.get(receipt.command_id)
                    if receipt.result
                    in {
                        ActuationResult.FAILED,
                        ActuationResult.UNCERTAIN,
                        ActuationResult.TIMEOUT,
                        ActuationResult.MEASUREMENT_FAULT,
                    }
                    else None
                ),
            )
            return
        source = self._commands_by_id.get(receipt.command_id)
        if source is not None and source.action == ActuationAction.OPEN:
            context["opened_commands"].append(source)
        elif source is not None and source.action == ActuationAction.CLOSE:
            context["opened_commands"] = [
                opened
                for opened in context["opened_commands"]
                if not (
                    opened.valve == source.valve
                    and opened.target_device == source.target_device
                    and opened.target_line == source.target_line
                )
            ]
        self._enqueue_next_plan_step(request_id)

    def _fail_plan(
        self,
        request_id: str,
        message: str,
        *,
        uncertain_command: ActuationCommand | None = None,
    ) -> None:
        context = self._plan_contexts.get(request_id)
        if context is None:
            return
        targets = list(context["opened_commands"])
        if uncertain_command is not None and uncertain_command.action == ActuationAction.OPEN:
            targets.append(uncertain_command)
        if targets or uncertain_command is not None:
            self._plan_contexts.pop(request_id, None)
            self.plan_result_ready.emit(
                {
                    "request_id": request_id,
                    "success": False,
                    "message": f"{message}；已进入异常安全停止并等待 A=0 证据。",
                }
            )
            self.invalidate_execution(
                reason=message or "阀门计划失败，已进入异常安全停止。",
                close_all_configured=True,
            )
            return
        unique_targets: dict[tuple[int, str | None, str | None], ActuationCommand] = {}
        for command in targets:
            unique_targets[(command.valve, command.target_device, command.target_line)] = command
        context["rolling_back"] = True
        context["failure_message"] = message
        context["rollback_commands"] = list(reversed(unique_targets.values()))
        self._enqueue_next_plan_rollback(request_id)

    def _enqueue_next_plan_rollback(self, request_id: str) -> None:
        context = self._plan_contexts.get(request_id)
        if context is None:
            return
        pending = context["rollback_commands"]
        if not pending:
            self._plan_contexts.pop(request_id, None)
            message = context["failure_message"]
            if context["rollback_failed"]:
                message = f"{message}；补偿关闭失败，阀门状态不确定，请立即人工确认。"
            else:
                message = f"{message}；此前已打开的阀门均已补偿关闭。"
            self.plan_result_ready.emit(
                {"request_id": request_id, "success": False, "message": message}
            )
            return
        opened = pending.pop(0)
        self._sequence += 1
        command = replace(
            opened,
            command_id=f"{request_id}-rollback-{self._sequence}",
            execution_epoch=self.protocol_state.execution_epoch,
            arm_epoch=self.protocol_state.arm_epoch,
            sequence=self._sequence,
            action=ActuationAction.CLOSE,
            category=ActuationCategory.SAFETY,
            expected_ns=int(self._clock_ns()),
            duration_ns=None,
            wall_timestamp=float(self._wall_clock()),
            safety_generation=self.interlock.read()[0],
        )
        self._plan_by_command[command.command_id] = request_id
        self.submit(command)

    def _schedule_breath_timeout(self, readiness, *, origin_ns: int | None = None) -> None:
        if (
            self.protocol_executor is None
            or self.protocol_executor.state.status != ProtocolExecutionStatus.WAITING_EXHALE
        ):
            return
        state = self.protocol_executor.state
        trial = state.current_trial
        if trial is None or state.waiting_started_at is None:
            return
        self._sequence += 1
        deadline_origin = int(self._clock_ns()) if origin_ns is None else int(origin_ns)
        deadline = deadline_origin + (
            self.protocol_executor.config.breath_gate_timeout_ms * 1_000_000
        )
        heapq.heappush(
            self._deadline_heap,
            (
                deadline,
                20,
                self._sequence,
                "breath_timeout",
                {
                    "readiness": readiness,
                    "execution_epoch": state.execution_epoch,
                    "arm_epoch": state.arm_epoch,
                    "trial_index": state.trial_index,
                    "trial_id": trial.trial_id,
                    "waiting_started_at": state.waiting_started_at,
                },
            ),
        )

    def _execute(self, command: ActuationCommand) -> None:
        if (
            command.category == ActuationCategory.NORMAL
            and command.execution_epoch != self.protocol_state.execution_epoch
        ):
            self._clear_cancelled_pending(command)
            self._emit_receipt(
                ActuationReceipt.from_write(
                    command=command,
                    started_ns=None,
                    actual_ns=None,
                    wall_timestamp=self._wall_clock(),
                    result=ActuationResult.CANCELLED,
                    message="动作 epoch 已失效，未写入硬件。",
                    stale=True,
                )
            )
            return

        before_generation, snapshot, unsafe_latched = self.interlock.read()
        rejection = snapshot.command_rejection_reason(command)
        selector_safe_route = bool(
            command.category == ActuationCategory.SAFETY and command.valve == 0
        )
        rejected_open = (
            command.action == ActuationAction.OPEN
            and not selector_safe_route
            and (
            command.safety_generation != before_generation
            or unsafe_latched
            or rejection
            )
        )
        rejected_close = (
            command.action == ActuationAction.CLOSE
            and (
                not snapshot.recording_ready
                or snapshot.recorder_failed
                or snapshot.session_closing
                or command.category
                in {ActuationCategory.MANUAL, ActuationCategory.PRETEST}
            )
            and bool(rejection)
        )
        if rejected_open or rejected_close:
            self._clear_cancelled_pending(command)
            message = rejection or "安全联锁已锁存，取消开阀。"
            if command.category == ActuationCategory.NORMAL:
                self._block(message)
            cancelled = ActuationReceipt.from_write(
                command=command,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                result=ActuationResult.CANCELLED,
                message=message,
            )
            if command.category == ActuationCategory.CLEANING:
                self.consume_receipt(cancelled)
                return
            self._handle_plan_receipt(cancelled)
            self._emit_receipt(cancelled)
            if (
                (snapshot.recorder_failed or snapshot.session_closing)
                and command.category == ActuationCategory.NORMAL
                and command.action == ActuationAction.CLOSE
            ):
                self.protocol_state.possibly_open_valves.add(command.valve)
                self.submit_emergency_close(
                    command.valve,
                    reason="会话关闭/记录器失败后普通关闭已升级为紧急安全关闭。",
                )
            return

        try:
            receipt = self.writer(command)
            if not isinstance(receipt, ActuationReceipt):
                raise TypeError("DO writer 未返回 ActuationReceipt")
            if not self._receipt_matches_command(receipt, command):
                receipt = ActuationReceipt.from_write(
                    command=command,
                    started_ns=int(self._clock_ns()),
                    actual_ns=None,
                    wall_timestamp=self._wall_clock(),
                    result=ActuationResult.UNCERTAIN,
                    message="DO receipt 身份冲突，硬件结果按不确定处理。",
                    stale=True,
                )
        except Exception as exc:
            receipt = ActuationReceipt.from_write(
                command=command,
                started_ns=int(self._clock_ns()),
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                result=ActuationResult.UNCERTAIN,
                message=f"DO 写入异常：{exc}",
            )

        after_generation, _, after_unsafe = self.interlock.read()
        if command.action == ActuationAction.OPEN and not selector_safe_route and (
            after_generation != before_generation or after_unsafe
        ):
            if command.valve == 0:
                if self.valve_service is not None:
                    self.valve_service.mark_selector_unknown()
            else:
                self.protocol_state.possibly_open_valves.add(command.valve)
            self._block("开阀写入期间 safety/readiness 发生变化，已请求紧急关闭。")
            self._clear_cancelled_pending(command)
            uncertain = replace(
                receipt,
                result=ActuationResult.UNCERTAIN,
                stale=True,
                message="开阀写入期间 safety/readiness 发生变化，结果按不确定处理并回滚。",
            )
            if command.category == ActuationCategory.CLEANING:
                self.consume_receipt(uncertain)
                self._retire_command(command.command_id)
                return
            if command.command_id in self._plan_by_command:
                self._handle_plan_receipt(uncertain)
            else:
                self.invalidate_execution(
                    reason=self.protocol_state.quality_block_reason,
                    close_all_configured=True,
                )
            self._emit_receipt(uncertain)
            self._remember_receipt(uncertain)
            self._retire_command(command.command_id)
            return
        self.consume_receipt(receipt)

    def _schedule_normal_close(
        self,
        open_receipt: ActuationReceipt,
        *,
        source_command: ActuationCommand | None,
    ) -> None:
        assert open_receipt.actual_ns is not None
        if self.protocol_executor is not None:
            self._sequence += 1
            close = self.protocol_executor.create_close_request(
                open_receipt,
                sequence=self._sequence,
                safety_generation=self.interlock.read()[0],
            )
            if not self.submit(close):
                self.protocol_state.possibly_open_valves.add(open_receipt.valve)
            return
        duration_ns = source_command.duration_ns if source_command is not None else None
        if duration_ns is None:
            self._block("开阀回执缺少原命令时长，已请求安全关闭。")
            self.protocol_state.possibly_open_valves.add(open_receipt.valve)
            self.submit_emergency_close(
                open_receipt.valve,
                reason=self.protocol_state.quality_block_reason,
            )
            return
        self._sequence += 1
        deadline = open_receipt.actual_ns + duration_ns
        close = ActuationCommand(
            command_id=f"close-{open_receipt.command_id}-{self._sequence}",
            execution_epoch=open_receipt.execution_epoch,
            arm_epoch=open_receipt.arm_epoch,
            sequence=self._sequence,
            trial_id=open_receipt.trial_id,
            trial_index=open_receipt.trial_index,
            valve=open_receipt.valve,
            action=ActuationAction.CLOSE,
            category=ActuationCategory.NORMAL,
            expected_ns=deadline,
            duration_ns=None,
            wall_timestamp=self._wall_clock(),
            safety_generation=self.interlock.read()[0],
        )
        self.protocol_state.close_deadline_ns = deadline
        if not self.submit(close):
            self.protocol_state.possibly_open_valves.add(open_receipt.valve)

    def _consume_executor_receipt(
        self,
        receipt: ActuationReceipt,
        *,
        source_command: ActuationCommand | None,
    ) -> None:
        from app.models import ProtocolExecutionReadiness

        if receipt.action == ActuationAction.CLOSE and receipt.result == ActuationResult.SUCCESS:
            actual_open_ns = self.protocol_state.actual_open_ns
            if actual_open_ns is None or receipt.actual_ns is None or receipt.actual_ns < actual_open_ns:
                receipt = replace(
                    receipt,
                    result=ActuationResult.MEASUREMENT_FAULT,
                    offset_ms=None,
                    jitter_ms=None,
                    message="关闭回执时间早于开阀回执，已排除质量样本并阻断。",
                )
            else:
                receipt = replace(
                    receipt,
                    actual_duration_ms=(receipt.actual_ns - actual_open_ns) / 1_000_000,
                )

        _, interlock, _ = self.interlock.read()
        readiness = ProtocolExecutionReadiness(
            connected=interlock.connected,
            hardware_ready=interlock.hardware_ready,
            flow_setpoints_ready=interlock.flow_setpoints_ready,
            safety_state=interlock.safety_state,
            ttl_input_ready=interlock.ttl_input_ready,
        )
        result = self.protocol_executor.consume_actuation_receipt(
            receipt,
            readiness=readiness,
        )
        update = self.metrics.record(receipt)
        self.protocol_state.quality = update.snapshot
        quality_event = self._quality_event(receipt, update)
        self.protocol_state.events.append(quality_event)
        self.protocol_state.recent_event = quality_event
        result.events.append(quality_event)
        if receipt.action == ActuationAction.OPEN:
            if receipt.result != ActuationResult.SUCCESS:
                self._mark_target_uncertain(receipt.valve)
                self.invalidate_execution(
                    reason="开阀失败或状态不确定，已进入异常安全停止。",
                    close_all_configured=True,
                )
            elif update.severe:
                self._block(
                    "阀门时序严重超限，已暂停新的阀门动作并请求安全关闭。"
                    "请检查系统负载和设备状态，确认所有阀门关闭后重新布防。"
                )
                self._mark_target_uncertain(receipt.valve)
                self.invalidate_execution(
                    reason=self.protocol_state.quality_block_reason,
                    close_all_configured=True,
                )
            else:
                self._schedule_normal_close(receipt, source_command=source_command)
        elif receipt.result == ActuationResult.SUCCESS and update.severe:
            # Executor first confirms closed and advances exactly once; then quality blocks re-arm.
            trial_id = receipt.trial_id
            if trial_id:
                self.protocol_state.executed_quality_failed_trials.add(trial_id)
            self.protocol_state.quality_resume_status = self.protocol_state.status
            self._block(
                "关闭动作时序严重超限；已请求全部配置目标紧急关闭，请确认后重新布防。"
            )
            self.invalidate_execution(
                reason=self.protocol_state.quality_block_reason,
                close_all_configured=True,
            )
        elif receipt.action == ActuationAction.CLOSE and receipt.result != ActuationResult.SUCCESS:
            self._mark_target_uncertain(receipt.valve)
            self.invalidate_execution(
                reason="正常关闭失败或状态不确定，已进入异常安全停止。",
                close_all_configured=True,
            )
        self._sync_ttl_request()
        self._emit_executor_result(result)
        self._emit_receipt(receipt)

    def _quality_event(self, receipt: ActuationReceipt, update) -> ProtocolGateEvent:
        transition_message = ""
        if update.warning_transitions:
            parts = []
            for transition in update.warning_transitions:
                state = "进入超限警告" if transition.active else "恢复正常"
                parts.append(f"{transition.stream} p95 {state}（{transition.p95_ms:.3f}ms）")
            transition_message = "；".join(parts)
        if update.severe:
            transition_message = (
                "阀门时序严重超限，已暂停新的阀门动作并请求安全关闭。"
                "请检查系统负载和设备状态，确认所有阀门关闭后重新布防。"
            )
        quality = update.snapshot
        return ProtocolGateEvent(
            event="actuation_receipt",
            timestamp=receipt.wall_timestamp,
            trial_id=receipt.trial_id,
            trial_index=receipt.trial_index,
            valve=receipt.valve,
            safety_state=self.interlock.read()[1].safety_state,
            result=receipt.result.value,
            message=transition_message or receipt.message,
            actual_duration_ms=receipt.actual_duration_ms,
            command_id=receipt.command_id,
            execution_epoch=receipt.execution_epoch,
            arm_epoch=receipt.arm_epoch,
            action_sequence=receipt.sequence,
            action=receipt.action.value,
            action_category=receipt.category.value,
            expected_ns=receipt.expected_ns,
            started_ns=receipt.started_ns,
            actual_ns=receipt.actual_ns,
            offset_ms=receipt.offset_ms,
            jitter_ms=receipt.jitter_ms,
            p95_open_ms=quality.open.p95_ms,
            p95_close_ms=quality.close.p95_ms,
            p95_combined_ms=quality.combined.p95_ms,
            sample_count_open=quality.open.sample_count,
            sample_count_close=quality.close.sample_count,
            sample_count_combined=quality.combined.sample_count,
            warning=bool(update.warning_transitions or quality.open.warning or quality.close.warning or quality.combined.warning),
            severe=bool(update.severe),
            measurement_point=receipt.measurement_point,
            quality_transitions=tuple(
                (
                    transition.stream,
                    "entered" if transition.active else "recovered",
                    float(transition.p95_ms),
                )
                for transition in update.warning_transitions
            ),
        )

    def _enqueue_emergency_locked(self, command: ActuationCommand) -> None:
        # Every safety intent retains its own command identity and receipt.  Do
        # not silently merge same-valve requests: callers and shutdown waiters
        # must be able to account for each accepted close.
        self._emergency.append(command)

    def _confirm_closed(self, valve: int) -> None:
        self.protocol_state.possibly_open_valves.discard(valve)
        if self.protocol_state.active_valve == valve:
            self.protocol_state.active_valve = None
        self.protocol_state.pending_close_command_id = None
        if not self.protocol_state.possibly_open_valves and self.protocol_state.active_valve is None:
            self.protocol_state.close_deadline_ns = None
            self.protocol_state.actual_open_ns = None

    def _mark_target_uncertain(self, valve: int) -> None:
        if valve == 0:
            if self.valve_service is not None:
                self.valve_service.mark_selector_unknown()
            return
        self.protocol_state.possibly_open_valves.add(valve)

    def _clear_cancelled_pending(self, command: ActuationCommand) -> None:
        if self.protocol_state.pending_open_command_id == command.command_id:
            self.protocol_state.pending_open_command_id = None
        if self.protocol_state.pending_close_command_id == command.command_id:
            self.protocol_state.pending_close_command_id = None

    def _remember_receipt(self, receipt: ActuationReceipt) -> None:
        """Keep an exact, bounded replay window while retiring full command payloads."""
        self._seen_receipts[receipt.command_id] = receipt
        self._seen_receipt_order.append((receipt.command_id, receipt.execution_epoch))
        while len(self._seen_receipt_order) > self._receipt_history_limit:
            command_id, _ = self._seen_receipt_order.popleft()
            self._seen_receipts.pop(command_id, None)

    def _enforce_selector_deadline(
        self,
        receipt: ActuationReceipt,
    ) -> ActuationReceipt:
        deadline_ns = self._safe_stop_selector_deadlines.get(receipt.command_id)
        if deadline_ns is None:
            return receipt
        completed_ns = (
            int(receipt.actual_ns)
            if receipt.actual_ns is not None
            else int(self._clock_ns())
        )
        if completed_ns <= deadline_ns and int(self._clock_ns()) <= deadline_ns:
            return receipt
        return replace(
            receipt,
            result=ActuationResult.UNCERTAIN,
            stale=True,
            message="selector receipt 超过单调 deadline，迟到成功不得作为安全证据。",
        )

    def _handle_duplicate_receipt(
        self,
        receipt: ActuationReceipt,
        *,
        conflicting: bool,
    ) -> None:
        del conflicting
        reason = (
            f"RECOVERY_REQUIRED：动作 receipt 内容冲突：{receipt.command_id}；"
            "既有安全证据已作废。"
        )
        self._block(reason)
        self._mark_target_uncertain(receipt.valve)
        plans = (self._safe_stop_plan, self._background_safe_stop_plan)
        for plan in plans:
            if plan is not None and receipt.operation_id == plan.identity.operation_id:
                plan.require_recovery(reason)
        self.protocol_state.quality_block_reason = reason

    @staticmethod
    def _receipt_matches_command(
        receipt: ActuationReceipt,
        command: ActuationCommand,
    ) -> bool:
        return (
            receipt.command_id == command.command_id
            and receipt.execution_epoch == command.execution_epoch
            and receipt.arm_epoch == command.arm_epoch
            and receipt.sequence == command.sequence
            and receipt.trial_id == command.trial_id
            and receipt.trial_index == command.trial_index
            and receipt.valve == command.valve
            and receipt.action == command.action
            and receipt.category == command.category
            and receipt.expected_ns == command.expected_ns
            and receipt.target_device == command.target_device
            and receipt.target_line == command.target_line
            and receipt.operation_id == command.operation_id
            and receipt.generation == command.generation
            and receipt.step_id == command.step_id
            and receipt.action_kind == command.action_kind
            and receipt.safety_generation == command.safety_generation
        )

    def _retire_command(self, command_id: str) -> None:
        self._commands_by_id.pop(command_id, None)
        self._safe_stop_selector_deadlines.pop(command_id, None)

    def _cancel_non_safety_commands_locked(self, *, reason: str) -> list[ActuationReceipt]:
        cancelled = [
            item[3]
            for item in self._normal_heap
            if item[3].category != ActuationCategory.SAFETY
        ]
        self._normal_heap = [
            item for item in self._normal_heap if item[3].category == ActuationCategory.SAFETY
        ]
        heapq.heapify(self._normal_heap)
        receipts = []
        for command in cancelled:
            self._clear_cancelled_pending(command)
            receipts.append(
                ActuationReceipt.from_write(
                    command=command,
                    started_ns=None,
                    actual_ns=None,
                    wall_timestamp=self._wall_clock(),
                    result=ActuationResult.CANCELLED,
                    message=reason,
                    stale=True,
                )
            )
        return receipts

    def _settle_cancelled_receipts(self, receipts: list[ActuationReceipt]) -> None:
        for receipt in receipts:
            self._handle_plan_receipt(receipt)
            self._emit_receipt(receipt)
            self._retire_command(receipt.command_id)

    def _reject_queued_messages(self, messages: list[tuple[str, dict[str, Any]]]) -> None:
        for kind, payload in messages:
            self._reject_stopped_message(kind, payload)

    def _block(self, reason: str) -> None:
        self.protocol_state.status = ProtocolExecutionStatus.BLOCKED
        self.protocol_state.quality_block_reason = reason
