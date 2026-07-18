from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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

    def as_dict(self) -> dict:
        return {
            "event": self.event,
            "trial_id": self.trial_id,
            "trial_index": self.trial_index,
            "valve": self.valve,
            "timestamp": self.timestamp,
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
    waiting_trigger_started_at: float | None = None
    waiting_started_at: float | None = None
    triggered_at: float | None = None
    active_valve: int | None = None
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
