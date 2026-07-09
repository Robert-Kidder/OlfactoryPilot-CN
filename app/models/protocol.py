from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class TriggerMode(StrEnum):
    MANUAL = "manual"
    TTL = "ttl"


ProtocolMetadata = dict[str, str]


@dataclass(frozen=True)
class ProtocolTrial:
    trial_id: str
    timing_ms: int | float
    duration_ms: int | float
    valve: int
    trigger: TriggerMode
    metadata: ProtocolMetadata = field(default_factory=dict)
    line_number: int | None = None


@dataclass(frozen=True)
class ProtocolDocument:
    source_path: Path
    source_name: str
    metadata: ProtocolMetadata = field(default_factory=dict)
    trials: list[ProtocolTrial] = field(default_factory=list)

    @property
    def trigger_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for trial in self.trials:
            key = trial.trigger.value
            summary[key] = summary.get(key, 0) + 1
        return summary
