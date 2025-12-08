from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.models.self_check import SelfCheckResult

LOG = logging.getLogger(__name__)


@dataclass
class Telemetry:
    airflow: float = 0.0
    safety_state: str = "SAFE"
    connected: bool = False
    timestamp: float = 0.0


@dataclass
class AppState:
    language: str = "zh-CN"
    window_title: str = "OlfactoryPilot 控制台"
    log_level: str = "INFO"
    status_message: str = "等待硬件连接..."
    low_flow_threshold: float = 0.2
    telemetry: Telemetry = field(default_factory=Telemetry)
    hardware_ready: bool = False
    self_check_results: list[SelfCheckResult] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> AppState:
        return cls(
            language=config.get("language", "zh-CN"),
            window_title=config.get("window_title", "OlfactoryPilot 控制台"),
            log_level=config.get("log_level", "INFO"),
            low_flow_threshold=float(config.get("low_flow_threshold", 0.2)),
            telemetry=Telemetry(safety_state=config.get("safety_state", "SAFE")),
        )

    def update_status(self, message: str) -> None:
        self.status_message = message

    def update_telemetry(self, data: dict[str, Any]) -> None:
        self.telemetry.airflow = self._coerce_float(data.get("airflow"), self.telemetry.airflow)
        self.telemetry.safety_state = data.get("safety_state", self.telemetry.safety_state)
        self.telemetry.connected = bool(data.get("connected", self.telemetry.connected))
        self.telemetry.timestamp = self._coerce_float(
            data.get("timestamp"), self.telemetry.timestamp
        )

    def update_self_check(self, results: Iterable[SelfCheckResult], ready: bool) -> None:
        self.self_check_results = list(results)
        self.hardware_ready = ready

    def format_self_check_summary(self) -> str:
        if not self.self_check_results:
            return "尚未进行硬件自检"
        parts: list[str] = []
        for item in self.self_check_results:
            parts.append(
                f"{item.name}: {item.status}（{item.reason}，建议：{item.suggestion}，时间戳 "
                f"{item.checked_at:.0f}）"
            )
        return " | ".join(parts)

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            LOG.warning("Invalid telemetry value %r, keeping previous %s", value, default)
            return default
