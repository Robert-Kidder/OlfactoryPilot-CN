from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.models.hardware_profile import HardwareProfile
from app.models.safe_stop import normalize_digital_target


class StaleHardwareProfileRevisionError(RuntimeError):
    """The caller is attempting to overwrite a newer on-disk profile."""


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    identity = os.path.normcase(str(path.resolve()))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(identity, threading.RLock())


class HardwareProfileStore:
    """Load, validate, atomically publish, and explicitly roll back profiles."""

    def __init__(
        self,
        *,
        local_config_path: Path,
        default_config: Mapping[str, Any] | None = None,
        default_config_path: Path | None = None,
        effective_config: Mapping[str, Any] | None = None,
    ) -> None:
        sources = sum(
            source is not None
            for source in (default_config, default_config_path, effective_config)
        )
        if sources != 1:
            raise ValueError(
                "必须且只能提供 default_config、default_config_path 或 effective_config 之一。"
            )
        self._local_config_path = Path(local_config_path)
        self._lock = _path_lock(self._local_config_path)
        if default_config_path is not None:
            base = self._read_json_object(Path(default_config_path), required=True)
        else:
            base = copy.deepcopy(dict(default_config or effective_config or {}))
        with self._lock:
            local = self._read_local()
            self._base_config = base
            self._effective_config = _prefer_local_profile_connections(
                _merge(base, local),
                local,
            )
            self._profile = HardwareProfile.from_config(self._effective_config)
            self._validate_devices(self._profile, self._effective_config)
            self._effective_config = _with_connection_aliases(
                self._effective_config,
                self._profile,
            )
            self._revision = _read_revision(local)
            last_good_raw = local.get("hardware_profile_last_known_good")
            self._last_known_good = (
                HardwareProfile.from_config(last_good_raw)
                if isinstance(last_good_raw, Mapping)
                else None
            )

    @property
    def profile(self) -> HardwareProfile:
        with self._lock:
            return self._profile

    @property
    def snapshot(self) -> HardwareProfile:
        return self.profile

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def rollback_available(self) -> bool:
        with self._lock:
            return self._last_known_good is not None

    @property
    def effective_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._effective_config)

    def validate_candidate(
        self,
        candidate: HardwareProfile | Mapping[str, Any],
    ) -> HardwareProfile:
        with self._lock:
            local = self._read_local()
            return self._validate_candidate(candidate, local=local)

    def _validate_candidate(
        self,
        candidate: HardwareProfile | Mapping[str, Any],
        *,
        local: Mapping[str, Any],
    ) -> HardwareProfile:
        if isinstance(candidate, HardwareProfile):
            parsed = HardwareProfile.from_config(candidate.to_dict())
        elif isinstance(candidate, Mapping):
            parsed = HardwareProfile.from_config(candidate)
        else:
            raise ValueError("HardwareProfile 候选必须是对象或 HardwareProfile。")
        validated = parsed.invalidate_changes_from(self._profile)
        candidate_effective = _merge(
            _merge(self._base_config, local),
            {"hardware_profile": validated.to_dict()},
        )
        self._validate_devices(validated, candidate_effective)
        return validated

    def save(
        self,
        candidate: HardwareProfile | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> HardwareProfile:
        with self._lock:
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            validated = self._validate_candidate(candidate, local=local)
            next_local = copy.deepcopy(local)
            next_local["hardware_profile_last_known_good"] = self._profile.to_dict()
            next_local = _with_connection_aliases(next_local, validated)
            next_local["hardware_profile"] = validated.to_dict()
            next_local["hardware_profile_revision"] = expected + 1
            self._atomic_write(next_local)
            self._last_known_good = self._profile
            self._profile = validated
            self._revision = expected + 1
            self._effective_config = _with_connection_aliases(
                _merge(self._base_config, next_local),
                validated,
            )
            return validated

    def rollback(self, *, expected_revision: int | None = None) -> HardwareProfile:
        with self._lock:
            if self._last_known_good is None:
                raise RuntimeError("没有可回滚的 HardwareProfile。")
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            rollback_profile = HardwareProfile.from_config(self._last_known_good.to_dict())
            candidate_effective = _merge(
                _merge(self._base_config, local),
                {"hardware_profile": rollback_profile.to_dict()},
            )
            self._validate_devices(rollback_profile, candidate_effective)
            next_local = copy.deepcopy(local)
            next_local = _with_connection_aliases(next_local, rollback_profile)
            next_local["hardware_profile"] = rollback_profile.to_dict()
            next_local["hardware_profile_last_known_good"] = self._profile.to_dict()
            next_local["hardware_profile_revision"] = expected + 1
            self._atomic_write(next_local)
            previous_active = self._profile
            self._profile = rollback_profile
            self._last_known_good = previous_active
            self._revision = expected + 1
            self._effective_config = _with_connection_aliases(
                _merge(self._base_config, next_local),
                rollback_profile,
            )
            return rollback_profile

    def _expected_revision(self, expected_revision: int | None) -> int:
        expected = self._revision if expected_revision is None else expected_revision
        if type(expected) is not int or expected < 0:
            raise ValueError("expected_revision 必须是非负整数。")
        if expected != self._revision:
            raise StaleHardwareProfileRevisionError(
                f"HardwareProfile revision 已过期：期望 {expected}，当前实例为 {self._revision}。"
            )
        return expected

    @staticmethod
    def _validate_devices(
        profile: HardwareProfile,
        effective_config: Mapping[str, Any],
    ) -> None:
        del effective_config
        devices = {device.casefold() for device in profile.connections.ni_device_ids}
        targets = [channel.target for channel in profile.channels if channel.enabled]
        if profile.selector is not None:
            targets.append(profile.selector.target)
        missing = sorted(
            {
                normalize_digital_target(target).split("/", 1)[0]
                for target in targets
                if normalize_digital_target(target).split("/", 1)[0] not in devices
            }
        )
        if missing:
            raise ValueError(
                "已启用 NI target 使用了未登记设备："
                + ", ".join(missing)
                + "；请先更新 ni_devices。"
            )

    @staticmethod
    def _require_disk_revision(local: Mapping[str, Any], expected: int) -> None:
        actual = _read_revision(local)
        if actual != expected:
            raise StaleHardwareProfileRevisionError(
                f"HardwareProfile revision 已过期：期望 {expected}，磁盘为 {actual}。"
            )

    def _read_local(self) -> dict[str, Any]:
        if not self._local_config_path.exists():
            return {}
        return self._read_json_object(self._local_config_path, required=True)

    @staticmethod
    def _read_json_object(path: Path, *, required: bool) -> dict[str, Any]:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"配置文件不存在：{path}")
            return {}
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} 顶层必须是 JSON 对象。")
        return value

    def _atomic_write(self, value: Mapping[str, Any]) -> None:
        target = self._local_config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            _fsync_directory(target.parent)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _with_connection_aliases(
    config: Mapping[str, Any],
    profile: HardwareProfile,
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(config))
    connections = profile.connections
    merged["serial_port"] = connections.serial_port
    merged["ni_devices"] = list(connections.ni_device_ids)
    merged["alicat_unit_ids"] = connections.alicat_unit_ids
    return merged


def _prefer_local_profile_connections(
    effective: Mapping[str, Any],
    local: Mapping[str, Any],
) -> dict[str, Any]:
    """Let a canonical local profile beat legacy aliases inherited from defaults."""

    merged = copy.deepcopy(dict(effective))
    local_profile = local.get("hardware_profile")
    if not isinstance(local_profile, Mapping):
        return merged
    connections = local_profile.get("connections")
    if not isinstance(connections, Mapping):
        return merged
    for key in ("serial_port", "ni_devices", "alicat_unit_ids"):
        if key not in local and key in connections:
            merged[key] = copy.deepcopy(connections[key])
    return merged


def _read_revision(local: Mapping[str, Any]) -> int:
    value = local.get("hardware_profile_revision", 0)
    if type(value) is not int or value < 0:
        raise ValueError("hardware_profile_revision 必须是非负整数。")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
