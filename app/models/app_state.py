from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.protocol import ProtocolDocument
from app.models.safe_stop import (
    SelectorConfig,
    SelectorRoute,
    normalize_digital_target,
)
from app.models.safety_state import SafetyState
from app.models.self_check import SelfCheckResult

LOG = logging.getLogger(__name__)


@dataclass
class Telemetry:
    airflow: float = 0.0
    safety_state: str = "SAFE"
    safety_reason: str = ""
    gating_state: str = "NEUTRAL"
    connected: bool = False
    timestamp: float = 0.0


@dataclass
class AppState:
    language: str = "zh-CN"
    window_title: str = "OlfactoryPilot 控制台"
    log_level: str = "INFO"
    status_message: str = "等待硬件连接..."
    hardware_variant: str = "20-channel"
    low_flow_threshold: float = 0.2
    inhale_threshold: float = 0.2
    exhale_threshold: float = -0.2
    signal_offset: float = 0.0
    signal_gain: float = 1.0
    applied_a: float = 0.0
    applied_b: float = 0.0
    applied_c: float = 0.0
    applied_a_comp: float = 0.0
    flow_setpoints_ready: bool = False
    telemetry: Telemetry = field(default_factory=Telemetry)
    hardware_ready: bool = False
    self_check_results: list[SelfCheckResult] = field(default_factory=list)
    config_path: Path | None = None
    manual_path: Path | None = None
    valve_variants: dict[str, dict[int, str]] = field(default_factory=dict)
    selector: SelectorConfig | None = None
    master_valve_line: str = ""
    last_shutdown_event: dict | None = None
    simulation_mode: bool = False
    loaded_protocol: ProtocolDocument | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> AppState:
        manual_path_value = config.get("manual_path")
        manual_path: Path | None = None
        manual_anchor = config.get("_config_path") or config.get("_local_config_path") or config.get("_user_config_path")
        if manual_path_value:
            manual_candidate = Path(manual_path_value)
            if not manual_candidate.is_absolute() and manual_anchor:
                manual_candidate = Path(manual_anchor).parent.parent / manual_candidate
            manual_path = manual_candidate

        valve_cfg = config.get("valve_mapping") or {}
        selector_raw = valve_cfg.get("selector") or {}
        selector_target = ""
        selector: SelectorConfig | None = None
        if isinstance(selector_raw, dict):
            selector_target = str(selector_raw.get("target", "") or "")
        if not selector_target:
            selector_target = str(valve_cfg.get("master_valve", "") or "")
        if selector_target:
            safe_route_raw = str(
                selector_raw.get("safe_route", SelectorRoute.COMPENSATION.value)
                if isinstance(selector_raw, dict)
                else SelectorRoute.COMPENSATION.value
            )
            try:
                safe_route = SelectorRoute(safe_route_raw)
            except ValueError:
                safe_route = None
                LOG.error(
                    "Invalid selector safe route %s; selector disabled",
                    safe_route_raw,
                )
            if safe_route is None:
                selector_target = ""
            else:
                try:
                    safe_level = selector_raw.get("safe_level", False)
                    odor_level = selector_raw.get("odor_level", True)
                    if type(safe_level) is not bool or type(odor_level) is not bool:
                        raise ValueError("selector safe_level/odor_level 必须是 JSON boolean")
                    selector = SelectorConfig(
                        target=selector_target,
                        safe_route=safe_route,
                        safe_level=safe_level,
                        odor_level=odor_level,
                    )
                except ValueError as exc:
                    LOG.error("Invalid selector configuration: %s", exc)
                    selector = None
                    selector_target = ""
        variants_raw = valve_cfg.get("variants") or {}
        valve_variants: dict[str, dict[int, str]] = {}
        for variant_name, mapping in variants_raw.items():
            mapped: dict[int, str] = {}
            if isinstance(mapping, dict):
                for key, target in mapping.items():
                    try:
                        channel = int(key)
                        normalized_target = normalize_digital_target(str(target))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not 1 <= channel <= 20:
                        LOG.error(
                            "Invalid odor valve id %s in variant %s; only 1-20 are allowed",
                            key,
                            variant_name,
                        )
                        continue
                    if normalized_target in {
                        normalize_digital_target(existing)
                        for existing in mapped.values()
                    }:
                        LOG.error(
                            "Duplicate physical odor target %s in variant %s; channel %s ignored",
                            target,
                            variant_name,
                            channel,
                        )
                        continue
                    mapped[channel] = str(target)
            valve_variants[str(variant_name)] = mapped
        selector_identity = (
            None
            if selector is None
            else normalize_digital_target(selector.target)
        )
        if selector_identity is not None and any(
            normalize_digital_target(target) == selector_identity
            for mapping in valve_variants.values()
            for target in mapping.values()
        ):
            LOG.error(
                "Selector target %s is also mapped as an odor valve; selector disabled",
                selector.target,
            )
            selector = None
            selector_target = ""

        hardware_variant_raw = config.get("hardware_variant", "20-channel")
        hardware_variant = hardware_variant_raw
        if hardware_variant not in valve_variants and "20-channel" in valve_variants:
            LOG.warning(
                "Hardware variant %s not found in valve variants, falling back to 20-channel",
                hardware_variant_raw,
            )
            hardware_variant = "20-channel"
        elif hardware_variant not in valve_variants:
            LOG.warning(
                "Hardware variant %s missing from valve variants; valve map will be empty",
                hardware_variant_raw,
            )

        return cls(
            language=config.get("language", "zh-CN"),
            window_title=config.get("window_title", "OlfactoryPilot 控制台"),
            log_level=config.get("log_level", "INFO"),
            low_flow_threshold=float(config.get("low_flow_threshold", 0.2)),
            inhale_threshold=float(config.get("inhale_threshold", 0.2)),
            exhale_threshold=float(config.get("exhale_threshold", -0.2)),
            signal_offset=float(config.get("signal_offset", 0.0)),
            signal_gain=float(config.get("signal_gain", 1.0)),
            telemetry=Telemetry(safety_state=config.get("safety_state", "SAFE")),
            config_path=(
                config.get("_config_write_path")
                or config.get("_user_config_path")
                or config.get("_local_config_path")
                or config.get("_config_path")
            ),
            manual_path=manual_path,
            hardware_variant=hardware_variant,
            valve_variants=valve_variants,
            selector=selector,
            # Compatibility alias for existing Story 4.1/session assets.  New
            # routing logic consumes ``selector`` and never counts it as valve 21.
            master_valve_line=selector_target,
            simulation_mode=bool(config.get("simulation_mode", False)),
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
        self.telemetry.safety_reason = data.get("safety_reason", self.telemetry.safety_reason)

    def apply_safety_state(self, safety_state: SafetyState) -> None:
        self.telemetry.safety_state = safety_state.state
        self.telemetry.safety_reason = safety_state.reason
        self.telemetry.timestamp = safety_state.updated_at

    def update_self_check(self, results: Iterable[SelfCheckResult], ready: bool) -> None:
        self.self_check_results = list(results)
        self.hardware_ready = ready

    def format_self_check_summary(self) -> str:
        if not self.self_check_results:
            return "尚未进行硬件自检"
        parts: list[str] = []
        for item in self.self_check_results:
            parts.append(
                f"{item.name}: {item.status}（{item.reason}，建议：{item.suggestion}，时间戳 {item.checked_at:.0f}）"
            )
        return " | ".join(parts)

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            LOG.warning("Invalid telemetry value %r, keeping previous %s", value, default)
            return default

    def apply_calibration(self, value: float) -> float:
        """Apply the current signal calibration (offset then gain)."""
        return (value + self.signal_offset) * self.signal_gain

    def get_active_valve_map(self) -> dict[int, str]:
        return self.valve_variants.get(self.hardware_variant, {})

    def resolve_valve_line(self, channel_id: int) -> str | None:
        return self.get_active_valve_map().get(int(channel_id))

    def has_active_valve_map(self) -> bool:
        """Check whether the current hardware variant has a mapping configured."""
        return bool(self.get_active_valve_map())
