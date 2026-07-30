from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.models.actuation import ActuationQualitySnapshot
from app.models.protocol import ProtocolDocument, ProtocolTrial, TriggerMode


class ProtocolExecutionStatus(StrEnum):
    IDLE = "idle"
    READY = "ready"
    WAITING_TRIGGER = "waiting_trigger"
    WAITING_EXHALE = "waiting_exhale"
    TRIGGERED = "triggered"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ProtocolExecutionReadiness:
    connected: bool = False
    hardware_ready: bool = False
    flow_setpoints_ready: bool = False
    safety_state: str = "UNKNOWN"
    ttl_input_ready: bool = False

    def rejection_reason(self, *, has_protocol: bool, require_ttl: bool = False) -> str:
        if not has_protocol:
            return "请先加载有效协议。"
        if not self.connected:
            return "硬件尚未连接，请先连接设备。"
        if not self.hardware_ready:
            return "基础硬件或 AI0 自检未通过，请先完成自检。"
        if not self.flow_setpoints_ready:
            return "MFC 流量设定尚未建立，请先设置并确认流量。"
        if self.safety_state != "SAFE":
            return f"安全状态为 {self.safety_state}，请恢复 SAFE 后再继续。"
        if require_ttl and not self.ttl_input_ready:
            return "TTL 输入 AI6 尚未就绪，请检查共享模拟输入采集链路。"
        return ""


@dataclass(frozen=True)
class ProtocolGateEvent:
    event: str
    timestamp: float
    monotonic_ns: int | None = None
    trial_id: str | None = None
    trial_index: int | None = None
    valve: int | None = None
    gate_state: str | None = None
    sample_value: float | None = None
    exhale_threshold: float | None = None
    safety_state: str | None = None
    result: str = "success"
    message: str = ""
    planned_duration_ms: float | None = None
    actual_duration_ms: float | None = None
    protocol_mode: str | None = None
    current_mode: str | None = None
    trigger_source: str | None = None
    arm_epoch: int | None = None
    pulse_sequence: int | None = None
    trigger_reason: str | None = None
    command_id: str | None = None
    execution_epoch: int | None = None
    action_sequence: int | None = None
    action: str | None = None
    action_category: str | None = None
    expected_ns: int | None = None
    started_ns: int | None = None
    actual_ns: int | None = None
    offset_ms: float | None = None
    jitter_ms: float | None = None
    p95_open_ms: float | None = None
    p95_close_ms: float | None = None
    p95_combined_ms: float | None = None
    sample_count_open: int = 0
    sample_count_close: int = 0
    sample_count_combined: int = 0
    warning: bool = False
    severe: bool = False
    measurement_point: str | None = None
    quality_transitions: tuple[tuple[str, str, float], ...] = ()

    def as_dict(self) -> dict:
        return {
            "event": self.event,
            "trial_id": self.trial_id,
            "trial_index": self.trial_index,
            "valve": self.valve,
            "timestamp": self.timestamp,
            "monotonic_ns": self.monotonic_ns,
            "gate_state": self.gate_state,
            "sample_value": self.sample_value,
            "exhale_threshold": self.exhale_threshold,
            "safety_state": self.safety_state,
            "result": self.result,
            "message": self.message,
            "planned_duration_ms": self.planned_duration_ms,
            "actual_duration_ms": self.actual_duration_ms,
            "protocol_mode": self.protocol_mode,
            "current_mode": self.current_mode,
            "trigger_source": self.trigger_source,
            "arm_epoch": self.arm_epoch,
            "pulse_sequence": self.pulse_sequence,
            "trigger_reason": self.trigger_reason,
            "command_id": self.command_id,
            "execution_epoch": self.execution_epoch,
            "action_sequence": self.action_sequence,
            "action": self.action,
            "action_category": self.action_category,
            "expected_ns": self.expected_ns,
            "started_ns": self.started_ns,
            "actual_ns": self.actual_ns,
            "offset_ms": self.offset_ms,
            "jitter_ms": self.jitter_ms,
            "p95_open_ms": self.p95_open_ms,
            "p95_close_ms": self.p95_close_ms,
            "p95_combined_ms": self.p95_combined_ms,
            "sample_count_open": self.sample_count_open,
            "sample_count_close": self.sample_count_close,
            "sample_count_combined": self.sample_count_combined,
            "warning": self.warning,
            "severe": self.severe,
            "measurement_point": self.measurement_point,
            "quality_transitions": [
                {
                    "stream": stream,
                    "direction": direction,
                    "p95_ms": p95_ms,
                }
                for stream, direction, p95_ms in self.quality_transitions
            ],
        }


@dataclass
class ProtocolExecutionState:
    document: ProtocolDocument | None = None
    status: ProtocolExecutionStatus = ProtocolExecutionStatus.IDLE
    trial_index: int = 0
    retry_count: int = 0
    declared_mode: TriggerMode | None = None
    current_mode: TriggerMode | None = None
    mode_override: TriggerMode | None = None
    arm_epoch: int = 0
    execution_epoch: int = 0
    waiting_trigger_started_at: float | None = None
    waiting_started_at: float | None = None
    triggered_at: float | None = None
    active_valve: int | None = None
    possibly_open_valves: set[int] = field(default_factory=set)
    pending_open_command_id: str | None = None
    pending_close_command_id: str | None = None
    close_deadline_ns: int | None = None
    actual_open_ns: int | None = None
    expected_open_ns: int | None = None
    quality: ActuationQualitySnapshot = field(default_factory=ActuationQualitySnapshot)
    quality_block_reason: str = ""
    quality_resume_status: ProtocolExecutionStatus | None = None
    executed_quality_failed_trials: set[str] = field(default_factory=set)
    trigger_source: str | None = None
    last_ttl_timestamp: float | None = None
    last_pulse_sequence: int = -1
    ttl_armed: bool = False
    recent_event: ProtocolGateEvent | None = None
    events: list[ProtocolGateEvent] = field(default_factory=list)

    @property
    def current_trial(self) -> ProtocolTrial | None:
        if self.document is None:
            return None
        if self.trial_index < 0 or self.trial_index >= len(self.document.trials):
            return None
        return self.document.trials[self.trial_index]


@dataclass(frozen=True)
class ProtocolExecutionSnapshot:
    status: ProtocolExecutionStatus
    status_text: str
    has_protocol: bool
    can_start: bool
    can_stop: bool
    can_advance: bool
    trial_label: str = "-"
    trial_id: str = "-"
    valve: int | None = None
    trigger: str = "-"
    wait_elapsed_ms: int = 0
    planned_duration_ms: float | None = None
    recent_event: str = "-"
    protocol_mode: str = "-"
    current_mode: str = "-"
    can_select_mode: bool = False
    can_select_manual_mode: bool = False
    can_select_ttl_mode: bool = False
    can_manual_trigger: bool = False
    can_rearm: bool = False
    ttl_armed: bool = False
    waiting_external_ttl: bool = False
    readiness_reason: str = ""
    trigger_source: str = "-"
    last_ttl_timestamp: float | None = None
    arm_epoch: int = 0
    execution_epoch: int = 0
    can_pause: bool = False
    can_resume: bool = False
    next_odor: str = "-"
    last_jitter_ms: float | None = None
    p95_open_ms: float | None = None
    p95_close_ms: float | None = None
    p95_combined_ms: float | None = None
    sample_count_open: int = 0
    sample_count_close: int = 0
    sample_count_combined: int = 0
    remaining_ms: float | None = None
    quality_block_reason: str = ""
