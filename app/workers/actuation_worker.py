from __future__ import annotations

import heapq
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
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolGateEvent,
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
        unsafe = self.unsafe_reason()
        if unsafe and command.action == ActuationAction.OPEN:
            return unsafe
        if command.category == ActuationCategory.NORMAL:
            return self.rejection_reason()
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
        return self.publish(candidate)

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
            previous = self._snapshot.safety_state
        safety_state = self._safety_manager.evaluate(
            airflow,
            timestamp=timestamp,
            previous_state=previous,
            hardware_state=hardware_state,
        )
        return self.publish(
            InterlockSnapshot(
                connected=bool(connected),
                hardware_ready=bool(hardware_ready),
                flow_setpoints_ready=bool(flow_setpoints_ready),
                safety_state=safety_state,
                ttl_input_ready=bool(ttl_input_ready),
                has_protocol=bool(has_protocol),
                device_lease=str(device_lease),
            )
        )

    def read(self) -> tuple[int, InterlockSnapshot, bool]:
        with self._lock:
            return self._generation, self._snapshot, self._unsafe_latched

    def clear_unsafe_latch(self) -> bool:
        with self._lock:
            if self._snapshot.unsafe_reason():
                return False
            self._unsafe_latched = False
            return True


Writer = Callable[[ActuationCommand], ActuationReceipt]


class ActuationWorker(QThread):
    """Single owner for protocol actuation state, quality metrics and DO scheduling."""

    receipt_ready = Signal(object)
    snapshot_ready = Signal(object)
    status_message = Signal(str)
    executor_result_ready = Signal(object)
    plan_result_ready = Signal(object)
    flow_result_ready = Signal(object)
    document_result_ready = Signal(object)
    ttl_arm_requested = Signal(int)
    ttl_disarm_requested = Signal()

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
        self._condition = threading.Condition(threading.RLock())
        self._normal_heap: list[tuple[int, int, int, ActuationCommand]] = []
        self._messages: deque[tuple[str, dict[str, Any]]] = deque()
        self._deadline_heap: list[tuple[int, int, int, str, dict[str, Any]]] = []
        self._emergency: deque[ActuationCommand] = deque()
        self._emergency_channels: set[int] = set()
        self._sequence = 1_000_000
        self._running = False
        self._accepting = True
        self._pending_ttl_arm_epoch: int | None = None
        self._seen_receipts: set[str] = set()
        self._commands_by_id: dict[str, ActuationCommand] = {}
        self._plan_contexts: dict[str, dict[str, Any]] = {}
        self._plan_by_command: dict[str, str] = {}
        self._shutdown_close_pending: set[str] = set()
        self._shutdown_close_failed = False
        self._shutdown_close_started = False
        self._do_handed_off = True
        self._pending_safe_transition: tuple[str, dict[str, Any]] | None = None

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
            if not self._accepting and command.category != ActuationCategory.SAFETY:
                return False
            if command.category == ActuationCategory.SAFETY:
                self._commands_by_id[command.command_id] = command
                self._enqueue_emergency_locked(command)
                self._condition.notify_all()
                return True
            if len(self._normal_heap) >= self._normal_capacity:
                self._block("动作队列已满，已阻断新的阀门打开；请停止并检查系统负载。")
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

    def post_start(self, *, document, readiness) -> None:
        self._post_message("start", {"document": document, "readiness": readiness})

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
        if not self.isRunning():
            self.process_ready_with_do_ownership()

    def _post_message(self, kind: str, payload: dict[str, Any]) -> None:
        with self._condition:
            self._messages.append((kind, payload))
            self._condition.notify_all()

    def submit_emergency_close(self, valve: int, *, reason: str) -> ActuationCommand:
        with self._condition:
            self._sequence += 1
            command = ActuationCommand(
                command_id=f"safety-close-{valve}-{self._sequence}",
                execution_epoch=self.protocol_state.execution_epoch,
                arm_epoch=self.protocol_state.arm_epoch,
                sequence=self._sequence,
                trial_id=(
                    self.protocol_state.current_trial.trial_id
                    if self.protocol_state.current_trial
                    else None
                ),
                trial_index=self.protocol_state.trial_index,
                valve=valve,
                action=ActuationAction.CLOSE,
                category=ActuationCategory.SAFETY,
                expected_ns=int(self._clock_ns()),
                duration_ns=None,
                wall_timestamp=float(self._wall_clock()),
                safety_generation=self.interlock.read()[0],
            )
            self._enqueue_emergency_locked(command)
            self.protocol_state.quality_block_reason = reason
            self._condition.notify_all()
            return command

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
        try:
            return self.process_ready(max_items=max_items)
        finally:
            if hal is not None:
                hal.release_do_output()

    def consume_receipt(self, receipt: ActuationReceipt) -> None:
        if receipt.command_id in self._seen_receipts:
            if receipt.action == ActuationAction.CLOSE and receipt.result == ActuationResult.SUCCESS:
                self._confirm_closed(receipt.valve)
            return
        self._seen_receipts.add(receipt.command_id)
        source_command = self._commands_by_id.get(receipt.command_id)

        if receipt.category == ActuationCategory.SAFETY:
            if receipt.result == ActuationResult.SUCCESS:
                self._confirm_closed(receipt.valve)
            else:
                self.protocol_state.possibly_open_valves.add(receipt.valve)
                self._block(
                    f"安全关闭阀门 {receipt.valve} 失败，硬件状态不确定；请人工确认并重试。"
                )
                self._abort_safe_transition_after_close_failure(receipt)
            with self._condition:
                if receipt.command_id in self._shutdown_close_pending:
                    self._shutdown_close_pending.discard(receipt.command_id)
                    if receipt.result != ActuationResult.SUCCESS:
                        self._shutdown_close_failed = True
                    self._condition.notify_all()
            self._handle_plan_receipt(receipt)
            self.receipt_ready.emit(receipt)
            self._maybe_finalize_safe_transition()
            return

        if receipt.category != ActuationCategory.NORMAL:
            self._handle_plan_receipt(receipt)
            self.receipt_ready.emit(receipt)
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
                self.protocol_state.possibly_open_valves.add(receipt.valve)
                self.submit_emergency_close(receipt.valve, reason="收到陈旧成功开阀回执，已请求补偿关闭。")
            elif receipt.action == ActuationAction.CLOSE and receipt.result == ActuationResult.SUCCESS:
                self._confirm_closed(receipt.valve)
            self.receipt_ready.emit(replace(receipt, stale=True))
            return

        if self.protocol_executor is not None:
            self._consume_executor_receipt(receipt, source_command=source_command)
            return

        if receipt.action == ActuationAction.OPEN:
            self.protocol_state.pending_open_command_id = None
            if receipt.result != ActuationResult.SUCCESS:
                self.protocol_state.possibly_open_valves.add(receipt.valve)
                self._block("开阀写入失败或结果不确定，已请求紧急关闭。")
                self.submit_emergency_close(receipt.valve, reason=self.protocol_state.quality_block_reason)
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
                    self.protocol_state.possibly_open_valves.add(receipt.valve)
                    self.submit_emergency_close(
                        receipt.valve,
                        reason=self.protocol_state.quality_block_reason,
                    )
                else:
                    self._schedule_normal_close(receipt, source_command=source_command)
        else:
            if receipt.result != ActuationResult.SUCCESS:
                self._block("定时关闭写入失败，保留活动阀状态并等待安全重试。")
                self.protocol_state.possibly_open_valves.add(receipt.valve)
            else:
                self.protocol_state.pending_close_command_id = None
                self._confirm_closed(receipt.valve)
                update = self.metrics.record(receipt)
                self.protocol_state.quality = update.snapshot
                if update.severe:
                    self._block("关闭动作时序严重超限；阀门已确认关闭，请显式重新布防。")
        self.receipt_ready.emit(receipt)

    def invalidate_execution(self, *, reason: str) -> None:
        with self._condition:
            self.protocol_state.execution_epoch += 1
            self.protocol_state.arm_epoch += 1
            self.protocol_state.pending_open_command_id = None
            self.protocol_state.pending_close_command_id = None
            self._normal_heap = [
                item for item in self._normal_heap if item[3].category != ActuationCategory.NORMAL
            ]
            heapq.heapify(self._normal_heap)
            self.protocol_state.ttl_armed = False
            self._pending_ttl_arm_epoch = None
            self.ttl_disarm_requested.emit()
            valves = set(self.protocol_state.possibly_open_valves)
            if self.protocol_state.active_valve is not None:
                valves.add(self.protocol_state.active_valve)
            for valve in valves:
                self.submit_emergency_close(valve, reason=reason)
            self._block(reason)
            self._condition.notify_all()

    def request_ttl_arm(self, *, arm_epoch: int) -> None:
        self.protocol_state.ttl_armed = False
        self._pending_ttl_arm_epoch = int(arm_epoch)
        self.ttl_arm_requested.emit(int(arm_epoch))

    def consume_ttl_arm_ack(self, arm_epoch: int, armed: bool) -> None:
        self._post_message(
            "ttl_arm_ack",
            {"arm_epoch": int(arm_epoch), "armed": bool(armed)},
        )
        if not self.isRunning():
            self.process_ready_with_do_ownership()

    def _apply_ttl_arm_ack(self, *, arm_epoch: int, armed: bool) -> None:
        if armed and arm_epoch == self._pending_ttl_arm_epoch == self.protocol_state.arm_epoch:
            self.protocol_state.ttl_armed = True

    def request_ttl_disarm(self) -> None:
        self._pending_ttl_arm_epoch = None
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
            if hal is not None:
                hal.release_do_output()
            with self._condition:
                self._do_handed_off = True
                self._condition.notify_all()

    def emergency_close_all(self, timeout_ms: int = 500) -> bool:
        """Invalidate normal work and await bounded close acks for every DO target."""
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

    def _begin_emergency_close_all(self) -> None:
        with self._condition:
            self._accepting = False
            self.protocol_state.execution_epoch += 1
            self.protocol_state.arm_epoch += 1
            self.protocol_state.pending_open_command_id = None
            self.protocol_state.pending_close_command_id = None
            self._normal_heap.clear()
            self._deadline_heap.clear()
            self.request_ttl_disarm()
            self._shutdown_close_pending.clear()
            self._shutdown_close_started = True
            for step in self.valve_service.emergency_close_steps():
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

    def fallback_close_all_after_handoff(self) -> bool:
        """Rebuild a DO session only after the previous owner has fully released it."""
        if self.isRunning() or not self._do_handed_off or self.valve_service is None:
            return False
        hal = self._writer_hal()
        if hal is None or not hal.prepare_do_output():
            return False
        success = True
        try:
            for step in self.valve_service.emergency_close_steps():
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
            hal.release_do_output()
        return success

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
            self._condition.notify_all()
        if self.isRunning():
            return bool(self.wait(max(1, int(timeout_ms))))
        return True

    def prepare_restart(self) -> bool:
        if self.isRunning() or not self._do_handed_off:
            return False
        with self._condition:
            self._accepting = True
        return True

    def _pop_ready(self) -> ActuationCommand | tuple[str, dict[str, Any]] | None:
        with self._condition:
            if self._emergency:
                command = self._emergency.popleft()
                self._emergency_channels.discard(command.valve)
                return command
            priority_message_kinds = {
                "emergency_close_all",
                "flow_result",
                "input_error",
                "load",
                "mode",
                "pause",
                "readiness",
                "stop",
            }
            for index, message in enumerate(self._messages):
                if message[0] in priority_message_kinds:
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
            if self._normal_heap and self._normal_heap[0][0] <= now_ns:
                return heapq.heappop(self._normal_heap)[3]
            if self._messages:
                return self._messages.popleft()
            return None

    def _handle_message(self, kind: str, payload: dict[str, Any]) -> None:
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
        if kind == "ttl_arm_ack":
            self._apply_ttl_arm_ack(**payload)
            self._emit_snapshot()
            return
        if kind == "emergency_close_all":
            self._begin_emergency_close_all()
            return
        if self.protocol_executor is None:
            return
        executor = self.protocol_executor
        readiness = payload.get("readiness") or self._current_readiness()
        if kind in {"stop", "pause", "mode", "load"}:
            self._begin_safe_transition(kind, payload)
            return
        if kind == "start":
            if (
                not readiness.rejection_reason(has_protocol=bool(payload.get("document") or executor.state.document))
                and self.protocol_state.active_valve is None
                and not self.protocol_state.possibly_open_valves
            ):
                self.interlock.clear_unsafe_latch()
            previous_epoch = executor.state.execution_epoch
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
        elif kind == "manual_trigger":
            result = executor.accept_trigger(
                "manual",
                readiness=readiness,
                timestamp=self._wall_clock(),
            )
            self._schedule_breath_timeout(readiness)
        elif kind == "ttl_pulse":
            pulse = payload["pulse"]
            result = executor.accept_trigger(
                "ttl",
                readiness=readiness,
                timestamp=pulse.timestamp,
                captured_epoch=pulse.arm_epoch,
                sequence=pulse.sequence,
            )
            self._schedule_breath_timeout(readiness)
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
            if reason and executor.state.status in {
                ProtocolExecutionStatus.WAITING_TRIGGER,
                ProtocolExecutionStatus.WAITING_EXHALE,
                ProtocolExecutionStatus.TRIGGERED,
            }:
                self.invalidate_execution(reason=f"运行就绪条件丢失：{reason}")
                event = executor._event(
                    "blocked",
                    payload.get("timestamp") or self._wall_clock(),
                    safety_state=readiness.safety_state,
                    result="blocked",
                    message=f"运行就绪条件丢失：{reason} 已安全阻断协议执行。",
                )
                result = executor._result_with_events([event])
            else:
                result = executor.handle_readiness_lost(
                    readiness,
                    timestamp=payload.get("timestamp") or self._wall_clock(),
                )
        elif kind == "breath_timeout":
            if payload["execution_epoch"] != executor.state.execution_epoch:
                return
            result = executor.handle_breath_timeout_deadline(
                readiness=payload["readiness"],
                timestamp=self._wall_clock(),
            )
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
        self.executor_result_ready.emit(result)
        self._emit_snapshot()

    def _begin_safe_transition(self, kind: str, payload: dict[str, Any]) -> None:
        if self.protocol_executor is None or self._pending_safe_transition is not None:
            return
        state = self.protocol_state
        state.execution_epoch += 1
        state.arm_epoch += 1
        state.pending_open_command_id = None
        state.pending_close_command_id = None
        state.ttl_armed = False
        self._pending_ttl_arm_epoch = None
        self.ttl_disarm_requested.emit()
        with self._condition:
            self._normal_heap = [item for item in self._normal_heap if item[3].category != ActuationCategory.NORMAL]
            heapq.heapify(self._normal_heap)
        self._pending_safe_transition = (kind, payload)
        valves = set(state.possibly_open_valves)
        if state.active_valve is not None:
            valves.add(state.active_valve)
        if valves:
            state.status = ProtocolExecutionStatus.BLOCKED
            for valve in valves:
                self.submit_emergency_close(valve, reason=f"{kind} 请求正在等待安全关闭确认。")
        else:
            self._finalize_safe_transition()

    def _maybe_finalize_safe_transition(self) -> None:
        if (
            self._pending_safe_transition is not None
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
            result = self.protocol_executor.reset(payload["document"], timestamp=now)
            self.document_result_ready.emit(
                {
                    "document": payload["document"],
                    "success": self.protocol_executor.state.document is payload["document"],
                }
            )
        self.protocol_state = self.protocol_executor.state
        self._sync_ttl_request()
        self.executor_result_ready.emit(result)
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
        is_safety = source == "safety" or source.startswith("safety:")
        if not is_safety and snapshot.device_lease == "protocol":
            reason = "协议运行中设备租约已占用，已拒绝流量变更。"
        elif not is_safety:
            reason = snapshot.unsafe_reason()
            # This command establishes MFC readiness, so that field cannot reject itself.
            if reason.startswith("MFC"):
                reason = ""
        submitted = None if reason or self._flow_submitter is None else self._flow_submitter(command)
        if reason or self._flow_submitter is None or submitted is False:
            message = reason or "串口动作队列不可用，流量未更改。"
            result = FlowApplyResult(False, message, a, b, c, a + c, "rejected")
            self.flow_result_ready.emit(FlowCommandResult(command=command, result=result))

    def _consume_flow_result(self, wrapped: FlowCommandResult) -> None:
        if wrapped.command.execution_epoch != self.protocol_state.execution_epoch:
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
        elif self._pending_ttl_arm_epoch is not None or state.ttl_armed:
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
            execution_epoch=self.protocol_state.execution_epoch,
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
        )
        self._plan_by_command[command.command_id] = request_id
        if not self.submit(command):
            self._plan_contexts.pop(request_id, None)
            self.plan_result_ready.emit(
                {
                    "request_id": request_id,
                    "success": False,
                    "message": "动作队列繁忙或设备租约冲突，阀门计划未执行。",
                }
            )

    def _handle_plan_receipt(self, receipt: ActuationReceipt) -> None:
        request_id = self._plan_by_command.pop(receipt.command_id, None)
        if request_id is None:
            return
        if self.valve_service is not None:
            self.valve_service.commit_receipt(receipt)
        if receipt.result != ActuationResult.SUCCESS:
            self._plan_contexts.pop(request_id, None)
            self.plan_result_ready.emit(
                {
                    "request_id": request_id,
                    "success": False,
                    "message": receipt.message or "阀门写入失败。",
                }
            )
            return
        self._enqueue_next_plan_step(request_id)

    def _schedule_breath_timeout(self, readiness) -> None:
        if (
            self.protocol_executor is None
            or self.protocol_executor.state.status != ProtocolExecutionStatus.WAITING_EXHALE
        ):
            return
        self._sequence += 1
        deadline = int(self._clock_ns()) + (
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
                    "execution_epoch": self.protocol_executor.state.execution_epoch,
                },
            ),
        )

    def _execute(self, command: ActuationCommand) -> None:
        if (
            command.category == ActuationCategory.NORMAL
            and command.execution_epoch != self.protocol_state.execution_epoch
        ):
            self._clear_cancelled_pending(command)
            self.receipt_ready.emit(
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
        if command.action == ActuationAction.OPEN and (
            command.safety_generation != before_generation
            or unsafe_latched
            or rejection
        ):
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
            self._handle_plan_receipt(cancelled)
            self.receipt_ready.emit(cancelled)
            return

        try:
            receipt = self.writer(command)
            if not isinstance(receipt, ActuationReceipt):
                raise TypeError("DO writer 未返回 ActuationReceipt")
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
        if command.action == ActuationAction.OPEN and (
            after_generation != before_generation or after_unsafe
        ):
            self.protocol_state.possibly_open_valves.add(command.valve)
            self._block("开阀写入期间 safety/readiness 发生变化，已请求紧急关闭。")
            self.submit_emergency_close(command.valve, reason=self.protocol_state.quality_block_reason)
            self._clear_cancelled_pending(command)
            self.receipt_ready.emit(replace(receipt, stale=True))
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
            self.submit(close)
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
        self.submit(close)

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
                self.submit_emergency_close(
                    receipt.valve,
                    reason="开阀失败或状态不确定，已请求紧急关闭。",
                )
            elif update.severe:
                self._block(
                    "阀门时序严重超限，已暂停新的阀门动作并请求安全关闭。"
                    "请检查系统负载和设备状态，确认所有阀门关闭后重新布防。"
                )
                self.protocol_state.possibly_open_valves.add(receipt.valve)
                self.submit_emergency_close(
                    receipt.valve,
                    reason=self.protocol_state.quality_block_reason,
                )
            else:
                self._schedule_normal_close(receipt, source_command=source_command)
        elif receipt.result == ActuationResult.SUCCESS and update.severe:
            # Executor first confirms closed and advances exactly once; then quality blocks re-arm.
            trial_id = receipt.trial_id
            if trial_id:
                self.protocol_state.executed_quality_failed_trials.add(trial_id)
            self.protocol_state.quality_resume_status = self.protocol_state.status
            self._block("关闭动作时序严重超限；阀门已确认关闭，请显式重新布防。")
        self.executor_result_ready.emit(result)
        self.receipt_ready.emit(receipt)

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
        )

    def _enqueue_emergency_locked(self, command: ActuationCommand) -> None:
        if command.valve in self._emergency_channels:
            return
        self._emergency_channels.add(command.valve)
        self._emergency.append(command)

    def _confirm_closed(self, valve: int) -> None:
        self.protocol_state.possibly_open_valves.discard(valve)
        if self.protocol_state.active_valve == valve:
            self.protocol_state.active_valve = None
        if not self.protocol_state.possibly_open_valves and self.protocol_state.active_valve is None:
            self.protocol_state.close_deadline_ns = None

    def _clear_cancelled_pending(self, command: ActuationCommand) -> None:
        if self.protocol_state.pending_open_command_id == command.command_id:
            self.protocol_state.pending_open_command_id = None
        if self.protocol_state.pending_close_command_id == command.command_id:
            self.protocol_state.pending_close_command_id = None

    def _block(self, reason: str) -> None:
        self.protocol_state.status = ProtocolExecutionStatus.BLOCKED
        self.protocol_state.quality_block_reason = reason
