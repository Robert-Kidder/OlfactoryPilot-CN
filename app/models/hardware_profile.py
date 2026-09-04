from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .safe_stop import SelectorConfig, SelectorRoute, normalize_digital_target

HARDWARE_PROFILE_SCHEMA_VERSION = 1
EXTERNAL_PORTS = tuple(range(1, 21))
DEFAULT_MAX_FLOW_SCCM = 5000.0
DEFAULT_NI_DEVICE_IDS = ("Dev1", "Dev2")


@dataclass(frozen=True, slots=True)
class ValveTargetPreset:
    """Read-only standard wiring preset used to resolve controller channels."""

    variant: str
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.variant != "20-channel":
            raise ValueError("普通气口设置只支持 20-channel target preset。")
        if len(self.targets) != len(EXTERNAL_PORTS):
            raise ValueError("20-channel target preset 必须完整包含控制通道 1–20。")
        normalized = tuple(normalize_digital_target(target) for target in self.targets)
        if len(set(normalized)) != len(normalized):
            raise ValueError("20-channel target preset 的 NI target 不得重复。")
        object.__setattr__(self, "targets", tuple(str(target).strip() for target in self.targets))

    def target_for(self, internal_valve: int | None) -> str:
        if internal_valve is None:
            return ""
        valve = _strict_int(internal_valve, "控制通道")
        if valve not in EXTERNAL_PORTS:
            raise ValueError("控制通道必须位于 1–20。")
        return self.targets[valve - 1]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ValveTargetPreset | None:
        valve_mapping = config.get("valve_mapping")
        if not isinstance(valve_mapping, Mapping):
            return None
        variants = valve_mapping.get("variants")
        if not isinstance(variants, Mapping):
            return None
        raw = variants.get("20-channel")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("valve_mapping.variants.20-channel 必须是对象。")
        expected = {str(port) for port in EXTERNAL_PORTS}
        if set(raw) != expected:
            raise ValueError("20-channel target preset 必须且只能包含控制通道 1–20。")
        return cls(
            variant="20-channel",
            targets=tuple(str(raw[str(port)]) for port in EXTERNAL_PORTS),
        )


class VerificationStatus(StrEnum):
    PENDING = "pending"
    MOCK_VERIFIED = "mock_verified"
    PHYSICAL_VERIFIED = "physical_verified"
    MAPPING_CHANGED = "mapping_changed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HardwareConnectionConfig:
    """Immutable local connection identifiers; construction never probes hardware."""

    serial_port: str | None = None
    ni_device_ids: tuple[str, ...] = DEFAULT_NI_DEVICE_IDS
    alicat_a_unit_id: str = "a"
    alicat_b_unit_id: str = "b"
    alicat_c_unit_id: str = "c"

    def __post_init__(self) -> None:
        serial_port = self.serial_port
        if serial_port is not None:
            if not isinstance(serial_port, str):
                raise ValueError("COM 端口必须是字符串或 null。")
            serial_port = serial_port.strip().upper() or None
        if serial_port is not None:
            match = re.fullmatch(r"COM([1-9]\d{0,2})", serial_port)
            if match is None or int(match.group(1)) > 256:
                raise ValueError("COM 端口必须使用 COM1–COM256 格式，或留空。")
        object.__setattr__(self, "serial_port", serial_port)

        if not isinstance(self.ni_device_ids, tuple | list) or not self.ni_device_ids:
            raise ValueError("NI device IDs 必须是非空数组。")
        devices: list[str] = []
        identities: set[str] = set()
        for value in self.ni_device_ids:
            if not isinstance(value, str):
                raise ValueError("每个 NI device ID 必须是字符串。")
            device = value.strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", device) is None:
                raise ValueError("NI device ID 仅允许字母开头及字母、数字、下划线、连字符。")
            identity = device.casefold()
            if identity in identities:
                raise ValueError("NI device IDs 不得重复（忽略大小写）。")
            identities.add(identity)
            devices.append(device)
        object.__setattr__(self, "ni_device_ids", tuple(devices))

        units = (
            self.alicat_a_unit_id,
            self.alicat_b_unit_id,
            self.alicat_c_unit_id,
        )
        normalized_units: list[str] = []
        for channel, value in zip(("A", "B", "C"), units, strict=True):
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9]", value) is None:
                raise ValueError(f"Alicat {channel} unit ID 必须是单个 ASCII 字母或数字。")
            normalized_units.append(value)
        if len({value.casefold() for value in normalized_units}) != 3:
            raise ValueError("Alicat A/B/C unit IDs 不得重复（忽略大小写）。")
        object.__setattr__(self, "alicat_a_unit_id", normalized_units[0])
        object.__setattr__(self, "alicat_b_unit_id", normalized_units[1])
        object.__setattr__(self, "alicat_c_unit_id", normalized_units[2])

    @property
    def alicat_unit_ids(self) -> dict[str, str]:
        return {
            "A": self.alicat_a_unit_id,
            "B": self.alicat_b_unit_id,
            "C": self.alicat_c_unit_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial_port": self.serial_port,
            "ni_devices": list(self.ni_device_ids),
            "alicat_unit_ids": self.alicat_unit_ids,
        }


@dataclass(frozen=True, slots=True)
class ChannelVerification:
    status: VerificationStatus = VerificationStatus.PENDING
    fingerprint: str = ""
    verified_at: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", VerificationStatus(self.status))
        fingerprint = str(self.fingerprint).strip().lower()
        if fingerprint and (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("验证指纹必须是 64 位 SHA-256 十六进制字符串。")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "verified_at", str(self.verified_at).strip())
        object.__setattr__(self, "note", str(self.note).strip())

    @property
    def verified(self) -> bool:
        return self.status in {
            VerificationStatus.MOCK_VERIFIED,
            VerificationStatus.PHYSICAL_VERIFIED,
        }

    def to_dict(self) -> dict[str, str]:
        value = {
            "status": self.status.value,
            "fingerprint": self.fingerprint,
            "verified_at": self.verified_at,
        }
        if self.note:
            value["note"] = self.note
        return value


@dataclass(frozen=True, slots=True)
class ChannelDescriptor:
    external_port: int
    internal_valve: int | None = None
    target: str = ""
    active_high: bool = True
    enabled: bool = False
    display_name: str = ""
    verification: ChannelVerification = ChannelVerification()

    def __post_init__(self) -> None:
        external_port = _strict_int(self.external_port, "机外气口")
        if external_port not in EXTERNAL_PORTS:
            raise ValueError("机外气口必须位于 1–20。")
        object.__setattr__(self, "external_port", external_port)

        internal_valve = self.internal_valve
        if internal_valve is not None:
            internal_valve = _strict_int(internal_valve, "内部阀位")
            if internal_valve not in EXTERNAL_PORTS:
                raise ValueError("内部阀位必须位于 1–20。")
        object.__setattr__(self, "internal_valve", internal_valve)

        if type(self.active_high) is not bool or type(self.enabled) is not bool:
            raise ValueError("active_high/enabled 必须是 JSON boolean。")
        target = str(self.target).strip()
        if bool(target) != (internal_valve is not None):
            raise ValueError("内部阀位和 NI target 必须同时填写或同时留空。")
        if target:
            normalize_digital_target(target)
        object.__setattr__(self, "target", target)

        display_name = str(self.display_name).strip()
        if len(display_name) > 80 or any(ord(character) < 32 for character in display_name):
            raise ValueError("显示名称不得超过 80 个字符或包含控制字符。")
        object.__setattr__(self, "display_name", display_name)
        if self.enabled and not target:
            raise ValueError("启用的机外气口必须配置内部阀位和 NI target。")
        if not isinstance(self.verification, ChannelVerification):
            raise ValueError("verification 必须是 ChannelVerification。")

    @property
    def software_id(self) -> int | None:
        """Compatibility name used by the existing valve services."""

        return self.internal_valve

    @property
    def mapping_fingerprint(self) -> str:
        if self.internal_valve is None or not self.target:
            return ""
        payload = {
            "active_high": self.active_high,
            "external_port": self.external_port,
            "internal_valve": self.internal_valve,
            "target": normalize_digital_target(self.target),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def verification_valid(self) -> bool:
        return bool(
            self.mapping_fingerprint
            and self.verification.verified
            and self.verification.fingerprint == self.mapping_fingerprint
        )

    def verification_valid_for(self, *, allow_mock: bool = False) -> bool:
        if not self.verification_valid:
            return False
        if self.verification.status is VerificationStatus.PHYSICAL_VERIFIED:
            return True
        return allow_mock and self.verification.status is VerificationStatus.MOCK_VERIFIED

    @property
    def available(self) -> bool:
        """Production-safe availability; Mock evidence is never sufficient here."""

        return self.enabled and self.verification_valid_for()

    def invalidate_stale_verification(self) -> ChannelDescriptor:
        if not self.verification.verified or self.verification_valid:
            return self
        return replace(
            self,
            verification=replace(
                self.verification,
                status=VerificationStatus.MAPPING_CHANGED,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_port": self.external_port,
            "internal_valve": self.internal_valve,
            "target": self.target,
            "active_high": self.active_high,
            "enabled": self.enabled,
            "display_name": self.display_name,
            "verification": self.verification.to_dict(),
        }


class ChannelRegistry:
    """Immutable external-port ↔ valve-position ↔ NI-target registry."""

    def __init__(self, channels: Iterable[ChannelDescriptor]) -> None:
        ordered = tuple(sorted(channels, key=lambda channel: channel.external_port))
        if tuple(channel.external_port for channel in ordered) != EXTERNAL_PORTS:
            raise ValueError("HardwareProfile 必须且只能包含机外气口 1–20。")
        internal = [
            channel.internal_valve
            for channel in ordered
            if channel.internal_valve is not None
        ]
        if len(set(internal)) != len(internal):
            raise ValueError("内部阀位不得重复映射。")
        targets = [
            normalize_digital_target(channel.target)
            for channel in ordered
            if channel.target
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("NI target 不得重复映射。")
        self._channels = ordered
        self._by_external = {channel.external_port: channel for channel in ordered}
        self._by_internal = {
            channel.internal_valve: channel
            for channel in ordered
            if channel.internal_valve is not None
        }

    @property
    def channels(self) -> tuple[ChannelDescriptor, ...]:
        return self._channels

    def available_external_ports(self, *, allow_mock: bool = False) -> tuple[int, ...]:
        return tuple(
            channel.external_port
            for channel in self._channels
            if channel.enabled and channel.verification_valid_for(allow_mock=allow_mock)
        )

    @property
    def enabled_external_ports(self) -> tuple[int, ...]:
        return tuple(channel.external_port for channel in self._channels if channel.enabled)

    def by_external_port(self, external_port: int) -> ChannelDescriptor:
        try:
            return self._by_external[int(external_port)]
        except (KeyError, TypeError, ValueError) as exc:
            raise KeyError(f"未知机外气口：{external_port}") from exc

    def by_internal_valve(self, internal_valve: int) -> ChannelDescriptor:
        try:
            return self._by_internal[int(internal_valve)]
        except (KeyError, TypeError, ValueError) as exc:
            raise KeyError(f"未知内部阀位：{internal_valve}") from exc

    def target_for_external_port(
        self,
        external_port: int,
        *,
        require_verified: bool = True,
        allow_mock: bool = False,
    ) -> str | None:
        channel = self.by_external_port(external_port)
        if not channel.enabled or (
            require_verified
            and not channel.verification_valid_for(allow_mock=allow_mock)
        ):
            return None
        return channel.target or None

    def internal_valve_targets(
        self,
        *,
        available_only: bool = True,
        allow_mock: bool = False,
    ) -> dict[int, str]:
        return {
            int(channel.internal_valve): channel.target
            for channel in self._channels
            if channel.internal_valve is not None
            and channel.target
            and (
                (
                    channel.enabled
                    and channel.verification_valid_for(allow_mock=allow_mock)
                )
                or not available_only
            )
        }


@dataclass(frozen=True, slots=True)
class FlowSetpoints:
    sample_a_sccm: float
    main_b_sccm: float
    vacuum_c_sccm: float
    max_total_sccm: float = DEFAULT_MAX_FLOW_SCCM
    max_sample_a_sccm: float = DEFAULT_MAX_FLOW_SCCM
    max_vacuum_c_sccm: float = DEFAULT_MAX_FLOW_SCCM

    def __post_init__(self) -> None:
        sample = _finite_number(self.sample_a_sccm, "A 样品流量")
        main = _finite_number(self.main_b_sccm, "B 主气流")
        vacuum = _finite_number(self.vacuum_c_sccm, "C 真空流量")
        max_total = _positive_finite(self.max_total_sccm, "A+B 总送风上限")
        max_sample = _positive_finite(self.max_sample_a_sccm, "A 流量上限")
        max_vacuum = _positive_finite(self.max_vacuum_c_sccm, "C 流量上限")
        if sample < 0 or main < 0 or vacuum < 0:
            raise ValueError("A/B/C 流量不得为负数。")
        if sample + main > max_total:
            raise ValueError("A+B 总送风超出 HardwareProfile 批准范围。")
        if sample > max_sample or vacuum > max_vacuum:
            raise ValueError("A/C 流量超出 HardwareProfile 批准范围。")
        object.__setattr__(self, "sample_a_sccm", sample)
        object.__setattr__(self, "main_b_sccm", main)
        object.__setattr__(self, "vacuum_c_sccm", vacuum)
        object.__setattr__(self, "max_total_sccm", max_total)
        object.__setattr__(self, "max_sample_a_sccm", max_sample)
        object.__setattr__(self, "max_vacuum_c_sccm", max_vacuum)

    @property
    def derived_total_sccm(self) -> float:
        """用户设定的总送风；只用于展示和已确认的联合上限。"""

        return self.sample_a_sccm + self.main_b_sccm

    def as_mfc_setpoints(self) -> tuple[tuple[str, float], ...]:
        return (
            ("A", self.sample_a_sccm),
            ("B", self.main_b_sccm),
            ("C", self.vacuum_c_sccm),
        )


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    schema_version: int
    profile_name: str
    channels: tuple[ChannelDescriptor, ...]
    selector: SelectorConfig | None
    connections: HardwareConnectionConfig = HardwareConnectionConfig()
    max_total_sccm: float = DEFAULT_MAX_FLOW_SCCM
    max_sample_a_sccm: float = DEFAULT_MAX_FLOW_SCCM
    max_vacuum_c_sccm: float = DEFAULT_MAX_FLOW_SCCM
    target_preset: ValveTargetPreset | None = None

    def __post_init__(self) -> None:
        version = _strict_int(self.schema_version, "HardwareProfile schema_version")
        if version != HARDWARE_PROFILE_SCHEMA_VERSION:
            raise ValueError(
                f"不支持 HardwareProfile schema_version={version}；当前仅支持 1。"
            )
        object.__setattr__(self, "schema_version", version)
        name = str(self.profile_name).strip()
        if not name or len(name) > 80:
            raise ValueError("HardwareProfile 名称必须为 1–80 个字符。")
        object.__setattr__(self, "profile_name", name)
        if not isinstance(self.connections, HardwareConnectionConfig):
            raise ValueError("connections 必须是 HardwareConnectionConfig。")
        if self.target_preset is not None and not isinstance(
            self.target_preset, ValveTargetPreset
        ):
            raise ValueError("target_preset 必须是 ValveTargetPreset。")
        normalized_channels = tuple(
            channel.invalidate_stale_verification() for channel in self.channels
        )
        registry = ChannelRegistry(normalized_channels)
        object.__setattr__(self, "channels", registry.channels)
        if self.selector is not None:
            selector_identity = normalize_digital_target(self.selector.target)
            if any(
                channel.target
                and normalize_digital_target(channel.target) == selector_identity
                for channel in normalized_channels
            ):
                raise ValueError("selector target 不得与任何气味阀 NI target 重复。")
        object.__setattr__(
            self,
            "max_total_sccm",
            _positive_finite(self.max_total_sccm, "A+B 总送风上限"),
        )
        object.__setattr__(
            self,
            "max_sample_a_sccm",
            _positive_finite(self.max_sample_a_sccm, "A 流量上限"),
        )
        object.__setattr__(
            self,
            "max_vacuum_c_sccm",
            _positive_finite(self.max_vacuum_c_sccm, "C 流量上限"),
        )

    @property
    def registry(self) -> ChannelRegistry:
        return ChannelRegistry(self.channels)

    @property
    def mapping_fingerprint(self) -> str:
        payload = [
            {
                "external_port": channel.external_port,
                "fingerprint": channel.mapping_fingerprint,
            }
            for channel in self.channels
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def resolved_target_for(self, internal_valve: int | None) -> str:
        """Resolve a normal-editor controller channel through the standard preset."""

        if internal_valve is None:
            return ""
        if self.target_preset is None:
            raise ValueError("缺少 20-channel target preset，无法修改控制通道。")
        return self.target_preset.target_for(internal_valve)

    def channel_uses_custom_target(self, external_port: int) -> bool:
        channel = self.registry.by_external_port(external_port)
        if channel.internal_valve is None or not channel.target:
            return False
        if self.target_preset is None:
            return True
        return normalize_digital_target(channel.target) != normalize_digital_target(
            self.target_preset.target_for(channel.internal_valve)
        )

    def flow_setpoints(
        self,
        *,
        sample_a_sccm: float,
        main_b_sccm: float,
        vacuum_c_sccm: float,
    ) -> FlowSetpoints:
        return FlowSetpoints(
            sample_a_sccm=sample_a_sccm,
            main_b_sccm=main_b_sccm,
            vacuum_c_sccm=vacuum_c_sccm,
            max_total_sccm=self.max_total_sccm,
            max_sample_a_sccm=self.max_sample_a_sccm,
            max_vacuum_c_sccm=self.max_vacuum_c_sccm,
        )

    def invalidate_changes_from(self, previous: HardwareProfile) -> HardwareProfile:
        previous_by_port = {
            channel.external_port: channel for channel in previous.channels
        }
        channels: list[ChannelDescriptor] = []
        for channel in self.channels:
            old = previous_by_port[channel.external_port]
            if channel.mapping_fingerprint == old.mapping_fingerprint:
                channels.append(channel)
                continue
            status = (
                VerificationStatus.MAPPING_CHANGED
                if old.mapping_fingerprint or channel.mapping_fingerprint
                else VerificationStatus.PENDING
            )
            channels.append(
                replace(
                    channel,
                    verification=ChannelVerification(
                        status=status,
                        fingerprint=old.verification.fingerprint,
                        verified_at=old.verification.verified_at,
                        note=old.verification.note,
                    ),
                )
            )
        return replace(self, channels=tuple(channels))

    def to_dict(self) -> dict[str, Any]:
        selector = None
        if self.selector is not None:
            selector = {
                "target": self.selector.target,
                "safe_route": self.selector.safe_route.value,
                "safe_level": self.selector.safe_level,
                "odor_level": self.selector.odor_level,
            }
        return {
            "schema_version": self.schema_version,
            "profile_name": self.profile_name,
            "flow_limits_sccm": {
                "total": self.max_total_sccm,
                "sample_a": self.max_sample_a_sccm,
                "vacuum_c": self.max_vacuum_c_sccm,
            },
            "selector": selector,
            "connections": self.connections.to_dict(),
            "channels": [channel.to_dict() for channel in self.channels],
        }

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        require_selector: bool = True,
    ) -> HardwareProfile:
        raw = config.get("hardware_profile", config)
        if not isinstance(raw, Mapping):
            raise ValueError("hardware_profile 必须是 JSON 对象。")
        _reject_unknown_keys(
            raw,
            {
                "schema_version",
                "profile_name",
                "flow_limits_sccm",
                "selector",
                "connections",
                "channels",
            },
            "hardware_profile",
        )
        channels_raw = raw.get("channels")
        if not isinstance(channels_raw, list):
            raise ValueError("hardware_profile.channels 必须是数组。")
        channels = tuple(_parse_channel(value) for value in channels_raw)
        selector = _parse_selector(raw.get("selector"), required=require_selector)
        limits = raw.get("flow_limits_sccm", {})
        if not isinstance(limits, Mapping):
            raise ValueError("flow_limits_sccm 必须是 JSON 对象。")
        _reject_unknown_keys(limits, {"total", "sample_a", "vacuum_c"}, "flow_limits_sccm")
        connections = _parse_connections(raw.get("connections"), legacy_config=config)
        try:
            target_preset = ValveTargetPreset.from_config(config)
        except ValueError:
            # HardwareProfile remains the runtime authority.  A broken legacy
            # preset blocks ordinary remapping, but must not rewrite or disable
            # an already valid saved profile during startup.
            target_preset = None
        return cls(
            schema_version=raw.get("schema_version"),
            profile_name=raw.get("profile_name", "默认硬件方案"),
            channels=channels,
            selector=selector,
            connections=connections,
            max_total_sccm=limits.get("total", DEFAULT_MAX_FLOW_SCCM),
            max_sample_a_sccm=limits.get("sample_a", DEFAULT_MAX_FLOW_SCCM),
            max_vacuum_c_sccm=limits.get("vacuum_c", DEFAULT_MAX_FLOW_SCCM),
            target_preset=target_preset,
        )


def _parse_connections(
    raw: Any,
    *,
    legacy_config: Mapping[str, Any],
) -> HardwareConnectionConfig:
    has_nested = raw is not None
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("hardware_profile.connections 必须是 JSON 对象。")
    _reject_unknown_keys(
        raw,
        {"serial_port", "ni_devices", "alicat_unit_ids"},
        "hardware_profile.connections",
    )
    serial_port = raw.get("serial_port") if has_nested else legacy_config.get("serial_port")
    ni_devices = (
        raw.get("ni_devices", DEFAULT_NI_DEVICE_IDS)
        if has_nested
        else legacy_config.get("ni_devices", DEFAULT_NI_DEVICE_IDS)
    )
    units = (
        raw.get("alicat_unit_ids", {"A": "a", "B": "b", "C": "c"})
        if has_nested
        else legacy_config.get("alicat_unit_ids", {"A": "a", "B": "b", "C": "c"})
    )
    if not isinstance(units, Mapping):
        raise ValueError("connections.alicat_unit_ids 必须是 JSON 对象。")
    _reject_unknown_keys(units, {"A", "B", "C"}, "connections.alicat_unit_ids")
    if set(units) != {"A", "B", "C"}:
        raise ValueError("connections.alicat_unit_ids 必须完整包含 A、B、C。")
    parsed = HardwareConnectionConfig(
        serial_port=serial_port,
        ni_device_ids=ni_devices,
        alicat_a_unit_id=units["A"],
        alicat_b_unit_id=units["B"],
        alicat_c_unit_id=units["C"],
    )
    return parsed


def _parse_channel(raw: Any) -> ChannelDescriptor:
    if not isinstance(raw, Mapping):
        raise ValueError("hardware_profile.channels 每一项必须是 JSON 对象。")
    _reject_unknown_keys(
        raw,
        {
            "external_port",
            "internal_valve",
            "target",
            "active_high",
            "enabled",
            "display_name",
            "verification",
        },
        "channel",
    )
    verification_raw = raw.get("verification", {})
    if not isinstance(verification_raw, Mapping):
        raise ValueError("channel.verification 必须是 JSON 对象。")
    _reject_unknown_keys(
        verification_raw,
        {"status", "fingerprint", "verified_at", "note"},
        "channel.verification",
    )
    try:
        verification = ChannelVerification(
            status=verification_raw.get("status", VerificationStatus.PENDING.value),
            fingerprint=verification_raw.get("fingerprint", ""),
            verified_at=verification_raw.get("verified_at", ""),
            note=verification_raw.get("note", ""),
        )
    except ValueError as exc:
        raise ValueError(f"机外气口 {raw.get('external_port')}: {exc}") from exc
    try:
        return ChannelDescriptor(
            external_port=raw.get("external_port"),
            internal_valve=raw.get("internal_valve"),
            target=raw.get("target", ""),
            active_high=raw.get("active_high", True),
            enabled=raw.get("enabled", False),
            display_name=raw.get("display_name", ""),
            verification=verification,
        )
    except ValueError as exc:
        raise ValueError(f"机外气口 {raw.get('external_port')}: {exc}") from exc


def _parse_selector(raw: Any, *, required: bool) -> SelectorConfig | None:
    if raw is None:
        if required:
            raise ValueError("HardwareProfile 缺少独立 selector 配置。")
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("hardware_profile.selector 必须是 JSON 对象。")
    _reject_unknown_keys(
        raw,
        {"target", "safe_route", "safe_level", "odor_level"},
        "hardware_profile.selector",
    )
    if type(raw.get("safe_level", False)) is not bool or type(
        raw.get("odor_level", True)
    ) is not bool:
        raise ValueError("selector safe_level/odor_level 必须是 JSON boolean。")
    try:
        return SelectorConfig(
            target=str(raw.get("target", "")),
            safe_route=SelectorRoute(raw.get("safe_route", "compensation")),
            safe_level=raw.get("safe_level", False),
            odor_level=raw.get("odor_level", True),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"selector 配置无效：{exc}") from exc


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"{label} 包含未知字段：{', '.join(unknown)}")


def _strict_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label}必须是整数。")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数值。")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数值。") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label}必须是有限数值。")
    return parsed


def _positive_finite(value: Any, label: str) -> float:
    parsed = _finite_number(value, label)
    if parsed <= 0:
        raise ValueError(f"{label}必须大于 0。")
    return parsed
