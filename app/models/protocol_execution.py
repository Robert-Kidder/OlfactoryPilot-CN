from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.models.protocol import ProtocolDocument, ProtocolTrial


class ProtocolExecutionStatus(StrEnum):
    IDLE = "idle"
    READY = "ready"
    WAITING_EXHALE = "waiting_exhale"
    TRIGGERED = "triggered"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    STOPPED = "stopped"


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
        }


@dataclass
class ProtocolExecutionState:
    document: ProtocolDocument | None = None
    status: ProtocolExecutionStatus = ProtocolExecutionStatus.IDLE
    trial_index: int = 0
    retry_count: int = 0
    waiting_started_at: float | None = None
    triggered_at: float | None = None
    active_valve: int | None = None
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
