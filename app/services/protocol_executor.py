from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    ProtocolDocument,
    ProtocolExecutionReadiness,
    ProtocolExecutionSnapshot,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolGateEvent,
    ProtocolTrial,
    TriggerMode,
    duration_ms_to_ns,
)
from app.services.gating_service import GatingService, GatingState, GatingTransition
from app.services.hal import BreathSampleBatch

ValveWriter = Callable[[int, bool], tuple[bool, str]]


@dataclass(frozen=True)
class ProtocolExecutionConfig:
    breath_gate_timeout_ms: int = 5000
    breath_gate_timeout_action: str = "skip"
    breath_gate_max_retries: int = 1

    @classmethod
    def from_mapping(cls, config: dict[str, Any] | None) -> ProtocolExecutionConfig:
        config = config or {}
        timeout = _safe_int(config.get("breath_gate_timeout_ms"), 5000)
        if timeout <= 0:
            timeout = 5000
        action = str(config.get("breath_gate_timeout_action", "skip")).lower()
        if action not in {"skip", "retry"}:
            action = "skip"
        retries = _safe_int(config.get("breath_gate_max_retries"), 1)
        if retries < 0:
            retries = 1
        return cls(
            breath_gate_timeout_ms=timeout,
            breath_gate_timeout_action=action,
            breath_gate_max_retries=retries,
        )


@dataclass(frozen=True)
class ProtocolExecutorResult:
    state: ProtocolExecutionState
    events: list[ProtocolGateEvent] = field(default_factory=list)
    transitions: list[GatingTransition] = field(default_factory=list)
    action_requests: tuple[ActuationCommand, ...] = ()


class ProtocolExecutor:
    def __init__(
        self,
        *,
        gating_service: GatingService,
        valve_writer: ValveWriter,
        config: ProtocolExecutionConfig | dict[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        deferred_actuation: bool = False,
    ) -> None:
        self.gating_service = gating_service
        self.valve_writer = valve_writer
        self.config = (
            config
            if isinstance(config, ProtocolExecutionConfig)
            else ProtocolExecutionConfig.from_mapping(config)
        )
        self.clock = clock or time.time
        self.deferred_actuation = bool(deferred_actuation)
        self._action_sequence = 0
        self.state = ProtocolExecutionState()

    def empty_result(self) -> ProtocolExecutorResult:
        return ProtocolExecutorResult(state=self.state)

    def reset(
        self,
        document: ProtocolDocument | None = None,
        *,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        if self.state.active_valve is not None:
            return self._rejected(
                "reset_rejected",
                self._now(timestamp),
                safety_state=None,
                message="仍有活动阀门未关闭，不能重置；请先停止并重试安全关闭。",
            )
        status = ProtocolExecutionStatus.READY if document and document.trials else ProtocolExecutionStatus.IDLE
        invalidated_epoch = self.state.arm_epoch + 1
        self.state = ProtocolExecutionState(
            document=document,
            status=status,
            arm_epoch=invalidated_epoch,
            execution_epoch=self.state.execution_epoch + 1,
        )
        self._sync_trial_mode(clear_override=True)
        return self.empty_result()

    def start(
        self,
        document: ProtocolDocument | None = None,
        *,
        readiness: ProtocolExecutionReadiness | None = None,
        safety_state: str | None = None,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        ready = self._readiness(readiness, safety_state)
        if self.state.active_valve is not None:
            return self._rejected(
                "start_rejected",
                now,
                safety_state=ready.safety_state,
                message="仍有活动阀门未关闭，不能开始；请先停止并重试安全关闭。",
            )
        candidate_document = document if document is not None else self.state.document
        if not candidate_document or not candidate_document.trials:
            return self._rejected(
                "invalid_protocol",
                now,
                safety_state=ready.safety_state,
                message="请先加载有效协议，然后再开始协议执行。",
            )

        allowed_start_states = (
            {ProtocolExecutionStatus.IDLE, ProtocolExecutionStatus.READY}
            if document is not None
            else {ProtocolExecutionStatus.READY, ProtocolExecutionStatus.STOPPED}
        )
        if self.state.status not in allowed_start_states:
            return self._rejected(
                "start_rejected",
                now,
                safety_state=ready.safety_state,
                message=f"当前状态为 {_STATUS_TEXT[self.state.status]}，不能开始协议。",
            )
        if document is not None or self.state.status == ProtocolExecutionStatus.STOPPED:
            prospective_mode = candidate_document.trials[0].trigger
        else:
            prospective_mode = self.state.current_mode
        reason = ready.rejection_reason(
            has_protocol=True,
            require_ttl=prospective_mode == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "start_rejected",
                now,
                safety_state=ready.safety_state,
                message=reason,
            )

        previous_execution_epoch = self.state.execution_epoch
        if document is not None:
            invalidated_epoch = self.state.arm_epoch + 1
            self.state = ProtocolExecutionState(
                document=candidate_document,
                status=ProtocolExecutionStatus.READY,
                arm_epoch=invalidated_epoch,
                execution_epoch=previous_execution_epoch,
            )
            self._sync_trial_mode(clear_override=True)
        elif self.state.status == ProtocolExecutionStatus.STOPPED:
            self.state.trial_index = 0
            self._sync_trial_mode(clear_override=True)
        self.state.retry_count = 0
        self.state.execution_epoch = previous_execution_epoch + 1
        return self._enter_trigger_waiting(now, readiness=ready)

    def accept_trigger(
        self,
        source: TriggerMode | str,
        *,
        readiness: ProtocolExecutionReadiness,
        timestamp: float | None = None,
        captured_epoch: int | None = None,
        sequence: int | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        try:
            trigger_source = source if isinstance(source, TriggerMode) else TriggerMode(str(source).lower())
        except ValueError:
            return self._rejected(
                "trigger_rejected",
                now,
                safety_state=readiness.safety_state,
                message="触发来源无效，已拒绝该事件。",
            )
        reason = readiness.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=trigger_source == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "trigger_rejected",
                now,
                safety_state=readiness.safety_state,
                trigger_source=trigger_source.value,
                message=reason,
            )
        if self.state.status != ProtocolExecutionStatus.WAITING_TRIGGER:
            return self._rejected(
                "trigger_ignored",
                now,
                safety_state=readiness.safety_state,
                trigger_source=trigger_source.value,
                result="ignored",
                message="当前不在等待触发状态，已忽略重复或过期触发。",
            )
        if trigger_source != self.state.current_mode:
            return self._rejected(
                "trigger_ignored",
                now,
                safety_state=readiness.safety_state,
                trigger_source=trigger_source.value,
                result="ignored",
                message="触发来源与当前运行模式不匹配，已忽略。",
            )
        if trigger_source == TriggerMode.TTL:
            if captured_epoch != self.state.arm_epoch:
                return self._rejected(
                    "ttl_pulse_ignored",
                    now,
                    safety_state=readiness.safety_state,
                    trigger_source=trigger_source.value,
                    result="ignored",
                    pulse_sequence=sequence,
                    message="TTL pulse 属于陈旧布防代次，已忽略。",
                )
            if sequence is None or sequence <= self.state.last_pulse_sequence:
                return self._rejected(
                    "ttl_pulse_ignored",
                    now,
                    safety_state=readiness.safety_state,
                    trigger_source=trigger_source.value,
                    result="ignored",
                    pulse_sequence=sequence,
                    message="TTL pulse 序号重复或无效，已忽略。",
                )
            self.state.last_pulse_sequence = sequence
            self.state.last_ttl_timestamp = now
        self.state.trigger_source = trigger_source.value
        self.state.ttl_armed = False
        self.state.waiting_trigger_started_at = None
        waiting = self._enter_waiting(now, safety_state=readiness.safety_state)
        accepted = self._event(
            "trigger_accepted",
            now,
            safety_state=readiness.safety_state,
            result="success",
            message="触发已接受，开始等待呼气阈值。",
            trigger_source=trigger_source.value,
            pulse_sequence=sequence,
        )
        return self._result_with_events([*waiting.events, accepted])

    def set_trigger_mode(
        self,
        mode: TriggerMode | str,
        *,
        readiness: ProtocolExecutionReadiness,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        try:
            target = mode if isinstance(mode, TriggerMode) else TriggerMode(str(mode).lower())
        except ValueError:
            return self._rejected(
                "mode_rejected",
                now,
                safety_state=readiness.safety_state,
                message="触发模式无效，只能选择 manual 或 ttl。",
            )
        if target == self.state.current_mode:
            return self.empty_result()
        reason = readiness.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=target == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "mode_rejected",
                now,
                safety_state=readiness.safety_state,
                message=reason,
            )
        allowed = {
            ProtocolExecutionStatus.READY,
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
        }
        if self.state.status not in allowed:
            return self._rejected(
                "mode_rejected",
                now,
                safety_state=readiness.safety_state,
                message=f"当前状态为 {_STATUS_TEXT[self.state.status]}，不能切换触发模式。",
            )
        old_mode = self.state.current_mode
        if self.state.active_valve is not None:
            ok, close_message = self.valve_writer(self.state.active_valve, False)
            if not ok:
                self.state.status = ProtocolExecutionStatus.BLOCKED
                return self._result_with_events(
                    [
                        self._event(
                            "mode_switch_failed",
                            now,
                            safety_state=readiness.safety_state,
                            result="close_failed",
                            message=f"关闭活动阀门失败：{close_message}；触发模式未切换。",
                        )
                    ]
                )
            self.state.active_valve = None
        self._invalidate_arm()
        self.state.current_mode = target
        self.state.mode_override = target
        self.state.waiting_started_at = None
        self.state.triggered_at = None
        self.state.retry_count = 0
        self.state.trigger_source = None
        if self.state.status != ProtocolExecutionStatus.READY:
            self.state.status = ProtocolExecutionStatus.WAITING_TRIGGER
            self.state.waiting_trigger_started_at = now
            self.state.ttl_armed = target == TriggerMode.TTL
        return self._result_with_events(
            [
                self._event(
                    "mode_changed",
                    now,
                    safety_state=readiness.safety_state,
                    message=f"触发模式已从 {old_mode.value if old_mode else '-'} 切换为 {target.value}。",
                )
            ]
        )

    def rearm_current(
        self,
        *,
        readiness: ProtocolExecutionReadiness,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if self.state.status != ProtocolExecutionStatus.BLOCKED or self.state.active_valve is not None:
            return self._rejected(
                "rearm_rejected",
                now,
                safety_state=readiness.safety_state,
                message="当前状态不能重新布防；如有活动阀门请先停止并重试安全关闭。",
            )
        reason = readiness.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=self.state.current_mode == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "rearm_rejected",
                now,
                safety_state=readiness.safety_state,
                message=reason,
            )
        self.state.execution_epoch += 1
        self.state.retry_count = 0
        return self._enter_trigger_waiting(now, readiness=readiness)

    def handle_readiness_lost(
        self,
        readiness: ProtocolExecutionReadiness,
        *,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        running = {
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
        }
        if self.state.status not in running:
            return self.empty_result()
        reason = readiness.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=self.state.current_mode == TriggerMode.TTL,
        )
        if not reason:
            return self.empty_result()
        return self._block(
            self._now(timestamp),
            safety_state=readiness.safety_state,
            message=f"运行就绪条件丢失：{reason} 已安全阻断协议执行。",
        )

    def handle_input_error(
        self,
        message: str,
        *,
        safety_state: str,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        running = {
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
        }
        if self.state.status not in running:
            return self._rejected(
                "ttl_input_error_ignored",
                self._now(timestamp),
                safety_state=safety_state,
                result="ignored",
                message=f"{message} 当前协议未运行，未改变执行状态。",
            )
        return self._block(
            self._now(timestamp),
            safety_state=safety_state,
            message=f"{message} 已失效当前 TTL 布防并安全阻断协议执行。",
        )

    def process_breath_samples(
        self,
        samples: list[float] | BreathSampleBatch,
        *,
        safety_state: str,
        readiness: ProtocolExecutionReadiness | None = None,
        timestamp_start: float | None = None,
        dt: float = 0.01,
        safety_generation: int = 0,
    ) -> ProtocolExecutorResult:
        structured = samples if isinstance(samples, BreathSampleBatch) else None
        values = [sample.value for sample in structured.samples] if structured else samples
        if not values:
            return self.empty_result()
        if structured:
            timestamp_start = structured.samples[0].timestamp
        if timestamp_start is None:
            raise ValueError("timestamp_start 不能为空。")
        ready = self._readiness(readiness, safety_state)
        readiness_result = self.handle_readiness_lost(ready, timestamp=timestamp_start)
        if readiness_result.events:
            return readiness_result
        if not all(math.isfinite(float(sample)) for sample in values):
            return self._block(
                timestamp_start,
                safety_state=safety_state,
                message="呼吸样本包含无效数值，已停止门控；请检查采集信号。",
            )

        transitions = (
            self.gating_service.process_sample_batch(structured, safety_state=safety_state)
            if structured
            else self.gating_service.process_batch(
                [float(sample) for sample in values],
                safety_state,
                timestamp_start=timestamp_start,
                dt=dt,
            )
        )
        if safety_state != "SAFE":
            blocked = self.handle_safety_update(safety_state, timestamp=timestamp_start)
            return ProtocolExecutorResult(
                state=blocked.state,
                events=blocked.events,
                transitions=transitions,
            )

        events: list[ProtocolGateEvent] = []
        action_requests: list[ActuationCommand] = []
        if self.state.status == ProtocolExecutionStatus.WAITING_EXHALE:
            trigger_transition = next(
                (transition for transition in transitions if transition.state == GatingState.EXHALE),
                None,
            )
            if trigger_transition is not None:
                triggered = self._trigger_current(
                    trigger_transition.timestamp,
                    safety_state=safety_state,
                    sample_value=trigger_transition.sample_value,
                    gate_state=trigger_transition.state.value,
                    expected_open_ns=trigger_transition.monotonic_ns or None,
                    trigger_reason="exhale_transition",
                    safety_generation=safety_generation,
                )
                events.extend(triggered.events)
                action_requests.extend(triggered.action_requests)
            elif self.gating_service.current_state == GatingState.EXHALE:
                sample = float(values[-1])
                fallback_timestamp = (
                    structured.samples[-1].timestamp
                    if structured
                    else timestamp_start + (len(values) - 1) * dt
                )
                fallback_monotonic_ns = (
                    structured.samples[-1].monotonic_ns if structured else None
                )
                triggered = self._trigger_current(
                    fallback_timestamp,
                    safety_state=safety_state,
                    sample_value=sample,
                    gate_state=GatingState.EXHALE.value,
                    expected_open_ns=fallback_monotonic_ns,
                    trigger_reason="exhale_state_fallback",
                    safety_generation=safety_generation,
                )
                events.extend(triggered.events)
                action_requests.extend(triggered.action_requests)

        return ProtocolExecutorResult(
            state=self.state,
            events=events,
            transitions=transitions,
            action_requests=tuple(action_requests),
        )

    def tick(
        self,
        *,
        safety_state: str,
        readiness: ProtocolExecutionReadiness | None = None,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        ready = self._readiness(readiness, safety_state)
        readiness_result = self.handle_readiness_lost(ready, timestamp=now)
        if readiness_result.events:
            return readiness_result
        if ready.safety_state != "SAFE":
            return self.handle_safety_update(ready.safety_state, timestamp=now)

        if self.state.status == ProtocolExecutionStatus.WAITING_EXHALE:
            started = self.state.waiting_started_at
            if started is not None and (now - started) * 1000 >= self.config.breath_gate_timeout_ms:
                return self._handle_timeout(now, readiness=ready)

        if self.state.status == ProtocolExecutionStatus.TRIGGERED:
            trial = self.state.current_trial
            started = self.state.triggered_at
            elapsed_ms = (now - started) * 1000 if started is not None else 0.0
            if trial and started is not None and elapsed_ms + 1e-9 >= float(trial.duration_ms):
                return self._finish_triggered_trial(now, readiness=ready)

        return self.empty_result()

    def handle_breath_timeout_deadline(
        self,
        *,
        readiness: ProtocolExecutionReadiness,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        """Owner-scheduled monotonic deadline entry; wall time is logging only."""
        if self.state.status != ProtocolExecutionStatus.WAITING_EXHALE:
            return self.empty_result()
        return self._handle_timeout(self._now(timestamp), readiness=readiness)

    def skip_current(
        self,
        *,
        safety_state: str,
        readiness: ProtocolExecutionReadiness | None = None,
        timestamp: float | None = None,
        message: str = "当前 trial 已跳过，准备下一 trial。",
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        ready = self._readiness(readiness, safety_state)
        if ready.safety_state != "SAFE":
            return self._safety_block(
                now,
                safety_state=ready.safety_state,
                message=f"安全状态为 {ready.safety_state}，不能推进 trial；请恢复 SAFE 后再继续。",
            )
        readiness_result = self.handle_readiness_lost(ready, timestamp=now)
        if readiness_result.events:
            return readiness_result
        allowed = {
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
        }
        if self.state.active_valve is not None or self.state.status not in allowed:
            return self._rejected(
                "skip_rejected",
                now,
                safety_state=ready.safety_state,
                message=(
                    "当前 trial 正在刺激或存在未关闭阀门，不能跳过；请先停止并确认安全关闭。"
                    if self.state.active_valve is not None
                    else f"当前状态为 {_STATUS_TEXT[self.state.status]}，不能跳过 trial。"
                ),
            )
        if not self.state.document:
            return self.start(None, readiness=ready, timestamp=now)
        common_reason = ready.rejection_reason(has_protocol=True)
        if common_reason:
            return self._block(
                now,
                safety_state=ready.safety_state,
                message=f"运行就绪条件丢失：{common_reason} 已阻断 trial 推进。",
            )
        events = [self._event("skip", now, safety_state=ready.safety_state, message=message)]
        self.state.status = ProtocolExecutionStatus.SKIPPED
        self.state.trial_index += 1
        self.state.retry_count = 0
        events.extend(self._prepare_after_advance(now, readiness=ready).events)
        return self._result_with_events(events)

    def stop(
        self,
        *,
        safety_state: str,
        timestamp: float | None = None,
        message: str = "门控流程已停止，危险输出已关闭或保持关闭。",
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        self._invalidate_arm()
        events: list[ProtocolGateEvent] = []
        if self.state.active_valve is not None:
            ok, close_message = self.valve_writer(self.state.active_valve, False)
            if not ok:
                self.state.status = ProtocolExecutionStatus.BLOCKED
                events.append(
                    self._event(
                        "stopped",
                        now,
                        safety_state=safety_state,
                        result="close_failed",
                        message=f"{message} 关闭活动阀门失败：{close_message}；请检查硬件后再次停止。",
                    )
                )
                return self._result_with_events(events)
            self.state.active_valve = None
            message = f"{message} 已关闭活动阀门。"
        self.state.status = ProtocolExecutionStatus.STOPPED
        events.append(self._event("stopped", now, safety_state=safety_state, message=message))
        return self._result_with_events(events)

    def pause_after_cleanup(
        self,
        *,
        safety_state: str,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if self.state.active_valve is not None or self.state.possibly_open_valves:
            return self._block(
                now,
                safety_state=safety_state,
                message="暂停前的安全关闭尚未确认。",
            )
        self._invalidate_arm()
        self.state.status = ProtocolExecutionStatus.PAUSED
        self.state.waiting_started_at = None
        self.state.waiting_trigger_started_at = None
        self.state.triggered_at = None
        return self._result_with_events(
            [self._event("paused", now, safety_state=safety_state, message="协议已暂停，所有阀门已确认关闭。")]
        )

    def resume_paused(
        self,
        *,
        readiness: ProtocolExecutionReadiness,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if self.state.status != ProtocolExecutionStatus.PAUSED:
            return self._rejected(
                "resume_rejected",
                now,
                safety_state=readiness.safety_state,
                message="当前协议未处于暂停状态。",
            )
        reason = readiness.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=self.state.current_mode == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "resume_rejected",
                now,
                safety_state=readiness.safety_state,
                message=reason,
            )
        self.state.execution_epoch += 1
        self._invalidate_arm()
        self.state.retry_count = 0
        self.state.trigger_source = None
        result = self._enter_trigger_waiting(now, readiness=readiness)
        return self._result_with_events(
            [self._event("resumed", now, safety_state=readiness.safety_state, message="协议已恢复并重新等待触发。"), *result.events]
        )

    def handle_safety_update(
        self,
        safety_state: str,
        *,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        if safety_state == "SAFE":
            return self.empty_result()
        if self.state.status in {
            ProtocolExecutionStatus.IDLE,
            ProtocolExecutionStatus.COMPLETED,
            ProtocolExecutionStatus.BLOCKED,
            ProtocolExecutionStatus.STOPPED,
        } and self.state.active_valve is None:
            return self.empty_result()
        now = self._now(timestamp)
        return self._safety_block(
            now,
            safety_state=safety_state,
            message=f"安全状态变为 {safety_state}，已中断门控。",
        )

    def snapshot(
        self,
        timestamp: float | None = None,
        *,
        readiness: ProtocolExecutionReadiness | None = None,
        safety_state: str | None = None,
        monotonic_ns: int | None = None,
    ) -> ProtocolExecutionSnapshot:
        now = self._now(timestamp)
        ready = self._readiness(readiness, safety_state)
        trial = self.state.current_trial
        total = len(self.state.document.trials) if self.state.document else 0
        wait_elapsed_ms = 0
        if self.state.status == ProtocolExecutionStatus.WAITING_EXHALE and self.state.waiting_started_at:
            wait_elapsed_ms = max(0, int((now - self.state.waiting_started_at) * 1000))
        elif (
            self.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
            and self.state.waiting_trigger_started_at is not None
        ):
            wait_elapsed_ms = max(
                0,
                int((now - self.state.waiting_trigger_started_at) * 1000),
            )
        recent = self.state.recent_event.message if self.state.recent_event else "-"
        common_reason = ready.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials)
        )
        ttl_reason = ready.rejection_reason(
            has_protocol=bool(self.state.document and self.state.document.trials),
            require_ttl=True,
        )
        common_ready = not common_reason
        ttl_ready = not ttl_reason
        execution_ready = ttl_ready if self.state.current_mode == TriggerMode.TTL else common_ready
        can_select_state = self.state.status in {
            ProtocolExecutionStatus.READY,
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
        }
        next_trial = None
        if self.state.document and self.state.trial_index + 1 < len(self.state.document.trials):
            next_trial = self.state.document.trials[self.state.trial_index + 1]
        next_odor = "-"
        if next_trial is not None:
            next_odor = next_trial.metadata.get("odor") or next_trial.metadata.get("label") or "-"
        quality = self.state.quality
        remaining_ms = None
        if self.state.close_deadline_ns is not None and monotonic_ns is not None:
            remaining_ms = max(
                0.0,
                (self.state.close_deadline_ns - int(monotonic_ns)) / 1_000_000,
            )
        return ProtocolExecutionSnapshot(
            status=self.state.status,
            status_text=_STATUS_TEXT.get(self.state.status, self.state.status.value),
            has_protocol=bool(self.state.document and self.state.document.trials),
            can_start=bool(
                execution_ready
                and self.state.document
                and self.state.document.trials
                and self.state.status in {ProtocolExecutionStatus.READY, ProtocolExecutionStatus.STOPPED}
            ),
            can_stop=self.state.status
            in {
                ProtocolExecutionStatus.WAITING_TRIGGER,
                ProtocolExecutionStatus.WAITING_EXHALE,
                ProtocolExecutionStatus.TRIGGERED,
            }
            or (self.state.status == ProtocolExecutionStatus.BLOCKED and self.state.active_valve is not None),
            can_advance=common_ready
            and self.state.status
            in {ProtocolExecutionStatus.WAITING_TRIGGER, ProtocolExecutionStatus.WAITING_EXHALE},
            trial_label=f"{self.state.trial_index + 1}/{total}" if trial else "-",
            trial_id=trial.trial_id if trial else "-",
            valve=trial.valve if trial else None,
            trigger=trial.trigger.value if trial else "-",
            wait_elapsed_ms=wait_elapsed_ms,
            planned_duration_ms=float(trial.duration_ms) if trial else None,
            recent_event=recent,
            protocol_mode=self.state.declared_mode.value if self.state.declared_mode else "-",
            current_mode=self.state.current_mode.value if self.state.current_mode else "-",
            can_select_mode=common_ready and can_select_state,
            can_select_manual_mode=common_ready and can_select_state,
            can_select_ttl_mode=ttl_ready and can_select_state,
            can_manual_trigger=bool(
                common_ready
                and self.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
                and self.state.current_mode == TriggerMode.MANUAL
            ),
            can_rearm=bool(
                execution_ready
                and self.state.status == ProtocolExecutionStatus.BLOCKED
                and self.state.active_valve is None
            ),
            ttl_armed=self.state.ttl_armed,
            waiting_external_ttl=bool(
                self.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
                and self.state.current_mode == TriggerMode.TTL
                and self.state.ttl_armed
            ),
            readiness_reason=ttl_reason if self.state.current_mode == TriggerMode.TTL else common_reason,
            trigger_source=self.state.trigger_source or "-",
            last_ttl_timestamp=self.state.last_ttl_timestamp,
            arm_epoch=self.state.arm_epoch,
            execution_epoch=self.state.execution_epoch,
            can_pause=self.state.status
            in {
                ProtocolExecutionStatus.WAITING_TRIGGER,
                ProtocolExecutionStatus.WAITING_EXHALE,
                ProtocolExecutionStatus.TRIGGERED,
            },
            can_resume=common_ready and self.state.status == ProtocolExecutionStatus.PAUSED,
            next_odor=next_odor,
            last_jitter_ms=quality.last_jitter_ms,
            p95_open_ms=quality.open.p95_ms,
            p95_close_ms=quality.close.p95_ms,
            p95_combined_ms=quality.combined.p95_ms,
            sample_count_open=quality.open.sample_count,
            sample_count_close=quality.close.sample_count,
            sample_count_combined=quality.combined.sample_count,
            remaining_ms=remaining_ms,
            quality_block_reason=self.state.quality_block_reason,
        )

    def _enter_trigger_waiting(
        self,
        timestamp: float,
        *,
        readiness: ProtocolExecutionReadiness,
    ) -> ProtocolExecutorResult:
        trial = self.state.current_trial
        invalid = self._validate_trial(trial)
        if invalid:
            return self._block(timestamp, safety_state=readiness.safety_state, message=invalid)
        if self.state.current_mode is None:
            self._sync_trial_mode(clear_override=False)
        reason = readiness.rejection_reason(
            has_protocol=True,
            require_ttl=self.state.current_mode == TriggerMode.TTL,
        )
        if reason:
            return self._rejected(
                "arm_rejected",
                timestamp,
                safety_state=readiness.safety_state,
                message=reason,
            )
        self._invalidate_arm()
        self.state.status = ProtocolExecutionStatus.WAITING_TRIGGER
        self.state.waiting_trigger_started_at = timestamp
        self.state.waiting_started_at = None
        self.state.triggered_at = None
        self.state.retry_count = 0
        self.state.trigger_source = None
        self.state.ttl_armed = self.state.current_mode == TriggerMode.TTL
        return self._result_with_events(
            [
                self._event(
                    "trigger_wait_start",
                    timestamp,
                    safety_state=readiness.safety_state,
                    message=(
                        "已布防，等待外部 TTL 上升沿。"
                        if self.state.ttl_armed
                        else "开始等待手动触发。"
                    ),
                )
            ]
        )

    def _enter_waiting(self, timestamp: float, *, safety_state: str) -> ProtocolExecutorResult:
        if safety_state != "SAFE":
            return self._safety_block(
                timestamp,
                safety_state=safety_state,
                message=f"安全状态为 {safety_state}，不能进入等待呼气；请恢复 SAFE 后再继续。",
            )
        trial = self.state.current_trial
        invalid = self._validate_trial(trial)
        if invalid:
            return self._block(timestamp, safety_state=safety_state, message=invalid)
        self.state.status = ProtocolExecutionStatus.WAITING_EXHALE
        self.state.waiting_trigger_started_at = None
        self.state.waiting_started_at = timestamp
        self.state.triggered_at = None
        self.state.active_valve = None
        return self._result_with_events(
            [
                self._event(
                    "wait_start",
                    timestamp,
                    safety_state=safety_state,
                    message="开始等待呼气阈值。",
                )
            ]
        )

    def _trigger_current(
        self,
        timestamp: float,
        *,
        safety_state: str,
        sample_value: float,
        gate_state: str,
        expected_open_ns: int | None = None,
        trigger_reason: str = "exhale_transition",
        safety_generation: int = 0,
    ) -> ProtocolExecutorResult:
        trial = self.state.current_trial
        invalid = self._validate_trial(trial)
        if invalid:
            return self._block(timestamp, safety_state=safety_state, message=invalid)
        assert trial is not None
        self.state.status = ProtocolExecutionStatus.TRIGGERED
        self.state.triggered_at = timestamp
        self.state.expected_open_ns = expected_open_ns
        self.state.waiting_started_at = None
        if self.deferred_actuation:
            if expected_open_ns is None or expected_open_ns < 0:
                return self._block(
                    timestamp,
                    safety_state=safety_state,
                    message="呼吸样本缺少有效 monotonic_ns，已阻断开阀。",
                )
            if self.state.pending_open_command_id is not None:
                return self.empty_result()
            try:
                duration_ns = duration_ms_to_ns(float(trial.duration_ms))
            except ValueError as exc:
                return self._block(timestamp, safety_state=safety_state, message=str(exc))
            self._action_sequence += 1
            command = ActuationCommand(
                command_id=(
                    f"protocol-{self.state.execution_epoch}-open-{self._action_sequence}"
                ),
                execution_epoch=self.state.execution_epoch,
                arm_epoch=self.state.arm_epoch,
                sequence=self._action_sequence,
                trial_id=trial.trial_id,
                trial_index=self.state.trial_index,
                valve=trial.valve,
                action=ActuationAction.OPEN,
                category=ActuationCategory.NORMAL,
                expected_ns=expected_open_ns,
                duration_ns=duration_ns,
                wall_timestamp=timestamp,
                safety_generation=safety_generation,
            )
            self.state.pending_open_command_id = command.command_id
            return self._result_with_events(
                [
                    self._event(
                        "open_requested",
                        timestamp,
                        safety_state=safety_state,
                        gate_state=gate_state,
                        sample_value=sample_value,
                        result="pending",
                        message="呼气条件已满足，已提交开阀请求等待硬件回执。",
                        planned_duration_ms=float(trial.duration_ms),
                        trigger_reason=trigger_reason,
                        command_id=command.command_id,
                    )
                ],
                action_requests=(command,),
            )
        ok, message = self.valve_writer(trial.valve, True)
        if not ok:
            return self._block(timestamp, safety_state=safety_state, message=message)
        self.state.active_valve = trial.valve
        return self._result_with_events(
            [
                self._event(
                    "exhale_trigger",
                    timestamp,
                    safety_state=safety_state,
                    gate_state=gate_state,
                    sample_value=sample_value,
                    result="success",
                    message="已达到呼气阈值并触发当前 trial。",
                    planned_duration_ms=float(trial.duration_ms),
                    trigger_reason=trigger_reason,
                )
            ]
        )

    def create_close_request(
        self,
        open_receipt: ActuationReceipt,
        *,
        sequence: int,
        safety_generation: int,
    ) -> ActuationCommand:
        if open_receipt.actual_ns is None:
            raise ValueError("open receipt 缺少 actual_ns，不能安排 close。")
        trial = self.state.current_trial
        if trial is None:
            raise ValueError("当前 trial 不存在，不能安排 close。")
        duration_ns = duration_ms_to_ns(float(trial.duration_ms))
        expected_ns = open_receipt.actual_ns + duration_ns
        command = ActuationCommand(
            command_id=f"protocol-{open_receipt.execution_epoch}-close-{sequence}",
            execution_epoch=open_receipt.execution_epoch,
            arm_epoch=open_receipt.arm_epoch,
            sequence=sequence,
            trial_id=open_receipt.trial_id,
            trial_index=open_receipt.trial_index,
            valve=open_receipt.valve,
            action=ActuationAction.CLOSE,
            category=ActuationCategory.NORMAL,
            expected_ns=expected_ns,
            duration_ns=None,
            wall_timestamp=open_receipt.wall_timestamp,
            safety_generation=safety_generation,
        )
        self.state.pending_close_command_id = command.command_id
        self.state.close_deadline_ns = expected_ns
        return command

    def consume_actuation_receipt(
        self,
        receipt: ActuationReceipt,
        *,
        readiness: ProtocolExecutionReadiness,
    ) -> ProtocolExecutorResult:
        pending_id = (
            self.state.pending_open_command_id
            if receipt.action == ActuationAction.OPEN
            else self.state.pending_close_command_id
        )
        if (
            receipt.stale
            or receipt.execution_epoch != self.state.execution_epoch
            or receipt.command_id != pending_id
        ):
            return self._result_with_events(
                [
                    self._event(
                        "actuation_receipt_stale",
                        receipt.wall_timestamp,
                        safety_state=readiness.safety_state,
                        result="stale",
                        message="动作回执身份或 execution epoch 已过期，未推进协议状态。",
                        command_id=receipt.command_id,
                    )
                ]
            )
        if receipt.action == ActuationAction.OPEN:
            self.state.pending_open_command_id = None
            if receipt.result != ActuationResult.SUCCESS:
                self.state.possibly_open_valves.add(receipt.valve)
                self.state.status = ProtocolExecutionStatus.BLOCKED
                return self._result_with_events(
                    [
                        self._event(
                            "blocked",
                            receipt.wall_timestamp,
                            safety_state=readiness.safety_state,
                            result=receipt.result.value,
                            message="开阀失败或结果不确定，已阻断并要求安全关闭。",
                            command_id=receipt.command_id,
                        )
                    ]
                )
            self.state.active_valve = receipt.valve
            self.state.actual_open_ns = receipt.actual_ns
            self.state.status = ProtocolExecutionStatus.TRIGGERED
            return self._result_with_events(
                [
                    self._event(
                        "exhale_trigger",
                        receipt.wall_timestamp,
                        safety_state=readiness.safety_state,
                        result="success",
                        message="开阀硬件回执成功，当前 trial 已进入刺激阶段。",
                        command_id=receipt.command_id,
                    )
                ]
            )

        if receipt.result != ActuationResult.SUCCESS:
            self.state.status = ProtocolExecutionStatus.BLOCKED
            self.state.possibly_open_valves.add(receipt.valve)
            return self._result_with_events(
                [
                    self._event(
                        "blocked",
                        receipt.wall_timestamp,
                        safety_state=readiness.safety_state,
                        result="close_failed",
                        message="关闭回执失败，保留活动阀事实并等待安全重试。",
                        command_id=receipt.command_id,
                    )
                ]
            )
        if (
            self.state.actual_open_ns is None
            or receipt.actual_ns is None
            or receipt.actual_ns < self.state.actual_open_ns
        ):
            self.state.status = ProtocolExecutionStatus.BLOCKED
            return self._result_with_events(
                [
                    self._event(
                        "measurement_fault",
                        receipt.wall_timestamp,
                        safety_state=readiness.safety_state,
                        result="measurement_fault",
                        message="关闭回执时间早于开阀回执，已排除质量样本并阻断。",
                        command_id=receipt.command_id,
                    )
                ]
            )
        trial = self.state.current_trial
        actual_duration_ms = (receipt.actual_ns - self.state.actual_open_ns) / 1_000_000
        self.state.pending_close_command_id = None
        self.state.active_valve = None
        self.state.possibly_open_valves.discard(receipt.valve)
        self.state.close_deadline_ns = None
        self.state.actual_open_ns = None
        events = [
            self._event(
                "stimulus_end",
                receipt.wall_timestamp,
                safety_state=readiness.safety_state,
                result="success",
                message="关闭硬件回执成功，当前 trial 已完成。",
                planned_duration_ms=float(trial.duration_ms) if trial else None,
                actual_duration_ms=actual_duration_ms,
                command_id=receipt.command_id,
            )
        ]
        self.state.trial_index += 1
        self.state.retry_count = 0
        events.extend(
            self._prepare_after_advance(receipt.wall_timestamp, readiness=readiness).events
        )
        return self._result_with_events(events)

    def _finish_triggered_trial(
        self,
        timestamp: float,
        *,
        readiness: ProtocolExecutionReadiness,
    ) -> ProtocolExecutorResult:
        safety_state = readiness.safety_state
        trial = self.state.current_trial
        if trial is None or self.state.active_valve is None:
            return self._block(timestamp, safety_state=safety_state, message="当前 trial 状态无效，已阻断。")
        ok, message = self.valve_writer(self.state.active_valve, False)
        if not ok:
            self.state.status = ProtocolExecutionStatus.BLOCKED
            return self._result_with_events(
                [
                    self._event(
                        "blocked",
                        timestamp,
                        safety_state=safety_state,
                        result="close_failed",
                        message=f"关闭阀门失败：{message}；已保持阻断状态，请再次停止以重试安全关闭。",
                    )
                ]
            )
        actual_ms = (
            (timestamp - self.state.triggered_at) * 1000
            if self.state.triggered_at is not None
            else None
        )
        self.state.active_valve = None
        events = [
            self._event(
                "stimulus_end",
                timestamp,
                safety_state=safety_state,
                message="刺激时长结束，已关闭当前阀门。",
                planned_duration_ms=float(trial.duration_ms),
                actual_duration_ms=actual_ms,
            )
        ]
        self.state.trial_index += 1
        self.state.retry_count = 0
        events.extend(self._prepare_after_advance(timestamp, readiness=readiness).events)
        return self._result_with_events(events)

    def _handle_timeout(
        self,
        timestamp: float,
        *,
        readiness: ProtocolExecutionReadiness,
    ) -> ProtocolExecutorResult:
        safety_state = readiness.safety_state
        events = [
            self._event(
                "timeout",
                timestamp,
                safety_state=safety_state,
                result="timeout",
                message="等待呼气超时。",
            )
        ]
        if (
            self.config.breath_gate_timeout_action == "retry"
            and self.state.retry_count < self.config.breath_gate_max_retries
        ):
            self.state.retry_count += 1
            self.state.waiting_started_at = timestamp
            events.append(
                self._event(
                    "retry",
                    timestamp,
                    safety_state=safety_state,
                    result="retry",
                    message=f"等待呼气超时，正在第 {self.state.retry_count} 次重试。",
                )
            )
            return self._result_with_events(events)
        reason = "已超过最大重试次数，跳过当前 trial。" if self.config.breath_gate_timeout_action == "retry" else "等待呼气超时，跳过当前 trial。"
        events.extend(
            self.skip_current(
                safety_state=safety_state,
                readiness=readiness,
                timestamp=timestamp,
                message=reason,
            ).events
        )
        return self._result_with_events(events)

    def _prepare_after_advance(
        self,
        timestamp: float,
        *,
        readiness: ProtocolExecutionReadiness,
    ) -> ProtocolExecutorResult:
        if not self.state.document or self.state.trial_index >= len(self.state.document.trials):
            self._invalidate_arm()
            self.state.status = ProtocolExecutionStatus.COMPLETED
            self.state.waiting_started_at = None
            self.state.triggered_at = None
            return self._result_with_events(
                [
                    self._event(
                        "completed",
                        timestamp,
                        safety_state=readiness.safety_state,
                        message="协议门控流程已完成。",
                    )
                ]
            )
        self._sync_trial_mode(clear_override=True)
        reason = readiness.rejection_reason(
            has_protocol=True,
            require_ttl=self.state.current_mode == TriggerMode.TTL,
        )
        if reason:
            return self._block(
                timestamp,
                safety_state=readiness.safety_state,
                message=f"下一 trial 无法布防：{reason}",
            )
        return self._enter_trigger_waiting(
            timestamp,
            readiness=readiness,
        )

    def _block(self, timestamp: float, *, safety_state: str, message: str) -> ProtocolExecutorResult:
        self._invalidate_arm()
        if self.state.active_valve is not None:
            ok, close_message = self.valve_writer(self.state.active_valve, False)
            if ok:
                self.state.active_valve = None
                message = f"{message} 已关闭活动阀门。"
            else:
                message = f"{message} 关闭活动阀门失败：{close_message}；请再次停止以重试安全关闭。"
                result = "close_failed"
                self.state.status = ProtocolExecutionStatus.BLOCKED
                self.state.waiting_started_at = None
                self.state.triggered_at = None
                return self._result_with_events(
                    [
                        self._event(
                            "blocked",
                            timestamp,
                            safety_state=safety_state,
                            result=result,
                            message=message,
                        )
                    ]
                )
        self.state.status = ProtocolExecutionStatus.BLOCKED
        self.state.waiting_started_at = None
        self.state.triggered_at = None
        return self._result_with_events(
            [
                self._event(
                    "blocked",
                    timestamp,
                    safety_state=safety_state,
                    result="blocked",
                    message=message,
                )
            ]
        )

    def _safety_block(
        self,
        timestamp: float,
        *,
        safety_state: str,
        message: str,
    ) -> ProtocolExecutorResult:
        self._invalidate_arm()
        result = "blocked"
        if self.state.active_valve is not None:
            ok, close_message = self.valve_writer(self.state.active_valve, False)
            if ok:
                self.state.active_valve = None
                message = f"{message} 已关闭活动阀门。"
            else:
                result = "close_failed"
                message = f"{message} 关闭活动阀门失败：{close_message}；请再次停止以重试安全关闭。"
        self.state.status = ProtocolExecutionStatus.BLOCKED
        self.state.waiting_started_at = None
        self.state.triggered_at = None
        return self._result_with_events(
            [
                self._event(
                    "safety_block",
                    timestamp,
                    safety_state=safety_state,
                    result=result,
                    message=message,
                )
            ]
        )

    def _event(
        self,
        event: str,
        timestamp: float,
        *,
        safety_state: str | None = None,
        gate_state: str | None = None,
        sample_value: float | None = None,
        result: str = "success",
        message: str = "",
        planned_duration_ms: float | None = None,
        actual_duration_ms: float | None = None,
        trigger_source: str | None = None,
        pulse_sequence: int | None = None,
        trigger_reason: str | None = None,
        command_id: str | None = None,
    ) -> ProtocolGateEvent:
        trial = self.state.current_trial
        return ProtocolGateEvent(
            event=event,
            timestamp=timestamp,
            trial_id=trial.trial_id if trial else None,
            trial_index=self.state.trial_index if trial else None,
            valve=trial.valve if trial else self.state.active_valve,
            gate_state=gate_state,
            sample_value=sample_value,
            exhale_threshold=self.gating_service.exhale_threshold,
            safety_state=safety_state,
            result=result,
            message=message,
            planned_duration_ms=planned_duration_ms,
            actual_duration_ms=actual_duration_ms,
            protocol_mode=self.state.declared_mode.value if self.state.declared_mode else None,
            current_mode=self.state.current_mode.value if self.state.current_mode else None,
            trigger_source=trigger_source or self.state.trigger_source,
            arm_epoch=self.state.arm_epoch,
            pulse_sequence=pulse_sequence,
            trigger_reason=trigger_reason,
            command_id=command_id,
            execution_epoch=self.state.execution_epoch,
        )

    def _rejected(
        self,
        event: str,
        timestamp: float,
        *,
        safety_state: str | None,
        message: str,
        result: str = "rejected",
        trigger_source: str | None = None,
        pulse_sequence: int | None = None,
    ) -> ProtocolExecutorResult:
        return self._result_with_events(
            [
                self._event(
                    event,
                    timestamp,
                    safety_state=safety_state,
                    result=result,
                    message=message,
                    trigger_source=trigger_source,
                    pulse_sequence=pulse_sequence,
                )
            ]
        )

    def _invalidate_arm(self) -> None:
        self.state.arm_epoch += 1
        self.state.ttl_armed = False
        self.state.waiting_trigger_started_at = None

    def _sync_trial_mode(self, *, clear_override: bool) -> None:
        trial = self.state.current_trial
        self.state.declared_mode = trial.trigger if trial else None
        if clear_override:
            self.state.mode_override = None
        self.state.current_mode = self.state.mode_override or self.state.declared_mode
        self.state.trigger_source = None
        self.state.last_pulse_sequence = -1

    @staticmethod
    def _readiness(
        readiness: ProtocolExecutionReadiness | None,
        safety_state: str | None,
    ) -> ProtocolExecutionReadiness:
        if readiness is not None:
            return readiness
        return ProtocolExecutionReadiness(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=True,
            safety_state=safety_state or "SAFE",
            ttl_input_ready=True,
        )

    def _result_with_events(
        self,
        events: list[ProtocolGateEvent],
        *,
        action_requests: tuple[ActuationCommand, ...] = (),
    ) -> ProtocolExecutorResult:
        for event in events:
            self.state.recent_event = event
            self.state.events.append(event)
        return ProtocolExecutorResult(
            state=self.state,
            events=events,
            action_requests=action_requests,
        )

    @staticmethod
    def _validate_trial(trial: ProtocolTrial | None) -> str:
        if trial is None:
            return "当前 trial 不存在，已阻断。"
        try:
            duration = float(trial.duration_ms)
        except (TypeError, ValueError):
            return "当前 trial 的 duration_ms 无效，已阻断。"
        if not math.isfinite(duration) or duration <= 0:
            return "当前 trial 的 duration_ms 必须大于 0，已阻断。"
        return ""

    def _now(self, timestamp: float | None = None) -> float:
        return float(self.clock() if timestamp is None else timestamp)


def _safe_int(value: Any, default: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return int(number)


_STATUS_TEXT = {
    ProtocolExecutionStatus.IDLE: "空闲",
    ProtocolExecutionStatus.READY: "已就绪",
    ProtocolExecutionStatus.WAITING_TRIGGER: "等待触发",
    ProtocolExecutionStatus.WAITING_EXHALE: "等待呼气",
    ProtocolExecutionStatus.TRIGGERED: "已触发",
    ProtocolExecutionStatus.SKIPPED: "已跳过",
    ProtocolExecutionStatus.COMPLETED: "已完成",
    ProtocolExecutionStatus.BLOCKED: "已阻断",
    ProtocolExecutionStatus.STOPPED: "已停止",
    ProtocolExecutionStatus.PAUSED: "已暂停",
}
