from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SelfCheckResult:
    name: str
    type: str
    status: str
    reason: str
    suggestion: str
    checked_at: float
