from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SafetyState:
    state: str
    airflow: float
    threshold: float
    updated_at: float
    reason: str

    def is_safe(self) -> bool:
        return self.state == "SAFE"

    def is_low_flow(self) -> bool:
        return self.state == "LOW_FLOW"

    def is_stale(self) -> bool:
        return self.state == "DATA_STALE"
