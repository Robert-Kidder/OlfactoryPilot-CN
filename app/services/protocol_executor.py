from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.models import (
    ProtocolDocument,
    ProtocolExecutionSnapshot,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolGateEvent,
    ProtocolTrial,
)
from app.services.gating_service import GatingService, GatingState, GatingTransition

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


class ProtocolExecutor:
    def __init__(
        self,
        *,
        gating_service: GatingService,
        valve_writer: ValveWriter,
        config: ProtocolExecutionConfig | dict[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.gating_service = gating_service
        self.valve_writer = valve_writer
        self.config = (
            config
            if isinstance(config, ProtocolExecutionConfig)
            else ProtocolExecutionConfig.from_mapping(config)
        )
        self.clock = clock or time.time
        self.state = ProtocolExecutionState()

    def empty_result(self) -> ProtocolExecutorResult:
        return ProtocolExecutorResult(state=self.state)

    def reset(self, document: ProtocolDocument | None = None) -> ProtocolExecutorResult:
        status = ProtocolExecutionStatus.READY if document and document.trials else ProtocolExecutionStatus.IDLE
        self.state = ProtocolExecutionState(document=document, status=status)
        return self.empty_result()

    def start(
        self,
        document: ProtocolDocument | None = None,
        *,
        safety_state: str,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if document is not None:
            self.state.document = document
            self.state.trial_index = 0
            self.state.retry_count = 0

        if not self.state.document or not self.state.document.trials:
            self.state.status = ProtocolExecutionStatus.IDLE
            return self._result_with_events(
                [
                    self._event(
                        "invalid_protocol",
                        now,
                        safety_state=safety_state,
                        result="blocked",
                        message="请先加载有效协议，然后再开始呼吸门控。",
                    )
                ]
            )

        if safety_state != "SAFE":
            self.state.status = ProtocolExecutionStatus.BLOCKED
            return self._result_with_events(
                [
                    self._event(
                        "safety_block",
                        now,
                        safety_state=safety_state,
                        result="blocked",
                        message=f"安全状态为 {safety_state}，请恢复 SAFE 后再开始。",
                    )
                ]
            )

        self.state.trial_index = 0
        self.state.retry_count = 0
        return self._enter_waiting(now, safety_state=safety_state)

    def process_breath_samples(
        self,
        samples: list[float],
        *,
        safety_state: str,
        timestamp_start: float,
        dt: float = 0.01,
    ) -> ProtocolExecutorResult:
        if not samples:
            return self.empty_result()
        if not all(math.isfinite(float(sample)) for sample in samples):
            return self._block(
                timestamp_start,
                safety_state=safety_state,
                message="呼吸样本包含无效数值，已停止门控；请检查采集信号。",
            )

        transitions = self.gating_service.process_batch(
            [float(sample) for sample in samples],
            safety_state,
            timestamp_start=timestamp_start,
            dt=dt,
        )
        if safety_state != "SAFE":
            blocked = self.handle_safety_update(safety_state, timestamp=timestamp_start)
            return ProtocolExecutorResult(
                state=blocked.state,
                events=blocked.events,
                transitions=transitions,
            )

        events: list[ProtocolGateEvent] = []
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
                )
                events.extend(triggered.events)
            elif self.gating_service.current_state == GatingState.EXHALE:
                sample = float(samples[-1])
                triggered = self._trigger_current(
                    timestamp_start + (len(samples) - 1) * dt,
                    safety_state=safety_state,
                    sample_value=sample,
                    gate_state=GatingState.EXHALE.value,
                )
                events.extend(triggered.events)

        return ProtocolExecutorResult(state=self.state, events=events, transitions=transitions)

    def tick(
        self,
        *,
        safety_state: str,
        timestamp: float | None = None,
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if safety_state != "SAFE":
            return self.handle_safety_update(safety_state, timestamp=now)

        if self.state.status == ProtocolExecutionStatus.WAITING_EXHALE:
            started = self.state.waiting_started_at
            if started is not None and (now - started) * 1000 >= self.config.breath_gate_timeout_ms:
                return self._handle_timeout(now, safety_state=safety_state)

        if self.state.status == ProtocolExecutionStatus.TRIGGERED:
            trial = self.state.current_trial
            started = self.state.triggered_at
            elapsed_ms = (now - started) * 1000 if started is not None else 0.0
            if trial and started is not None and elapsed_ms + 1e-9 >= float(trial.duration_ms):
                return self._finish_triggered_trial(now, safety_state=safety_state)

        return self.empty_result()

    def skip_current(
        self,
        *,
        safety_state: str,
        timestamp: float | None = None,
        message: str = "当前 trial 已跳过，准备下一 trial。",
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        if not self.state.document:
            return self.start(None, safety_state=safety_state, timestamp=now)
        events = [self._event("skip", now, safety_state=safety_state, message=message)]
        self.state.status = ProtocolExecutionStatus.SKIPPED
        self.state.trial_index += 1
        self.state.retry_count = 0
        events.extend(self._prepare_after_advance(now, safety_state=safety_state).events)
        return self._result_with_events(events)

    def stop(
        self,
        *,
        safety_state: str,
        timestamp: float | None = None,
        message: str = "门控流程已停止，危险输出已关闭或保持关闭。",
    ) -> ProtocolExecutorResult:
        now = self._now(timestamp)
        events: list[ProtocolGateEvent] = []
        if self.state.active_valve is not None:
            ok, close_message = self.valve_writer(self.state.active_valve, False)
            if not ok:
                message = f"{message} 关闭阀门失败：{close_message}"
        self.state.status = ProtocolExecutionStatus.STOPPED
        self.state.active_valve = None
        events.append(self._event("stopped", now, safety_state=safety_state, message=message))
        return self._result_with_events(events)

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
        }:
            return self.empty_result()
        now = self._now(timestamp)
        if self.state.active_valve is not None:
            self.valve_writer(self.state.active_valve, False)
            self.state.active_valve = None
        self.state.status = ProtocolExecutionStatus.BLOCKED
        self.state.waiting_started_at = None
        self.state.triggered_at = None
        return self._result_with_events(
            [
                self._event(
                    "safety_block",
                    now,
                    safety_state=safety_state,
                    result="blocked",
                    message=f"安全状态变为 {safety_state}，已中断门控并关闭危险输出。",
                )
            ]
        )

    def snapshot(self, timestamp: float | None = None) -> ProtocolExecutionSnapshot:
        now = self._now(timestamp)
        trial = self.state.current_trial
        total = len(self.state.document.trials) if self.state.document else 0
        wait_elapsed_ms = 0
        if self.state.status == ProtocolExecutionStatus.WAITING_EXHALE and self.state.waiting_started_at:
            wait_elapsed_ms = max(0, int((now - self.state.waiting_started_at) * 1000))
        recent = self.state.recent_event.message if self.state.recent_event else "-"
        return ProtocolExecutionSnapshot(
            status=self.state.status,
            status_text=_STATUS_TEXT.get(self.state.status, self.state.status.value),
            has_protocol=bool(self.state.document and self.state.document.trials),
            can_start=bool(
                self.state.document
                and self.state.document.trials
                and self.state.status in {ProtocolExecutionStatus.IDLE, ProtocolExecutionStatus.READY}
            ),
            can_stop=self.state.status
            in {ProtocolExecutionStatus.WAITING_EXHALE, ProtocolExecutionStatus.TRIGGERED},
            can_advance=self.state.status == ProtocolExecutionStatus.WAITING_EXHALE,
            trial_label=f"{self.state.trial_index + 1}/{total}" if trial else "-",
            trial_id=trial.trial_id if trial else "-",
            valve=trial.valve if trial else None,
            trigger=trial.trigger.value if trial else "-",
            wait_elapsed_ms=wait_elapsed_ms,
            planned_duration_ms=float(trial.duration_ms) if trial else None,
            recent_event=recent,
        )

    def _enter_waiting(self, timestamp: float, *, safety_state: str) -> ProtocolExecutorResult:
        trial = self.state.current_trial
        invalid = self._validate_trial(trial)
        if invalid:
            return self._block(timestamp, safety_state=safety_state, message=invalid)
        self.state.status = ProtocolExecutionStatus.WAITING_EXHALE
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
    ) -> ProtocolExecutorResult:
        trial = self.state.current_trial
        invalid = self._validate_trial(trial)
        if invalid:
            return self._block(timestamp, safety_state=safety_state, message=invalid)
        assert trial is not None
        ok, message = self.valve_writer(trial.valve, True)
        if not ok:
            return self._block(timestamp, safety_state=safety_state, message=message)
        self.state.status = ProtocolExecutionStatus.TRIGGERED
        self.state.triggered_at = timestamp
        self.state.waiting_started_at = None
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
                )
            ]
        )

    def _finish_triggered_trial(self, timestamp: float, *, safety_state: str) -> ProtocolExecutorResult:
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
                        result="blocked",
                        message=f"关闭阀门失败：{message}",
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
        events.extend(self._prepare_after_advance(timestamp, safety_state=safety_state).events)
        return self._result_with_events(events)

    def _handle_timeout(self, timestamp: float, *, safety_state: str) -> ProtocolExecutorResult:
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
        events.extend(self.skip_current(safety_state=safety_state, timestamp=timestamp, message=reason).events)
        return self._result_with_events(events)

    def _prepare_after_advance(self, timestamp: float, *, safety_state: str) -> ProtocolExecutorResult:
        if not self.state.document or self.state.trial_index >= len(self.state.document.trials):
            self.state.status = ProtocolExecutionStatus.COMPLETED
            return self._result_with_events(
                [
                    self._event(
                        "completed",
                        timestamp,
                        safety_state=safety_state,
                        message="协议门控流程已完成。",
                    )
                ]
            )
        return self._enter_waiting(timestamp, safety_state=safety_state)

    def _block(self, timestamp: float, *, safety_state: str, message: str) -> ProtocolExecutorResult:
        if self.state.active_valve is not None:
            self.valve_writer(self.state.active_valve, False)
            self.state.active_valve = None
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
        )

    def _result_with_events(self, events: list[ProtocolGateEvent]) -> ProtocolExecutorResult:
        for event in events:
            self.state.recent_event = event
            self.state.events.append(event)
        return ProtocolExecutorResult(state=self.state, events=events)

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
    ProtocolExecutionStatus.WAITING_EXHALE: "等待呼气",
    ProtocolExecutionStatus.TRIGGERED: "已触发",
    ProtocolExecutionStatus.SKIPPED: "已跳过",
    ProtocolExecutionStatus.COMPLETED: "已完成",
    ProtocolExecutionStatus.BLOCKED: "已阻断",
    ProtocolExecutionStatus.STOPPED: "已停止",
}
