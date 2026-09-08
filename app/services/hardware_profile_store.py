from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.models.hardware_profile import (
    ChannelVerification,
    HardwareProfile,
    VerificationStatus,
)
from app.models.hardware_verification import PhysicalVerificationContract
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
                HardwareProfile.from_config(
                    _merge(self._effective_config, {"hardware_profile": last_good_raw})
                )
                if (
                    isinstance(last_good_raw, Mapping)
                    and (
                        "target_preset" in last_good_raw
                        or self._profile.target_preset is None
                    )
                )
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
            parsed = replace(
                HardwareProfile.from_config(candidate.to_dict()),
                target_preset=candidate.target_preset or self._profile.target_preset,
            )
        elif isinstance(candidate, Mapping):
            parsed = HardwareProfile.from_config(candidate)
            if parsed.target_preset is None:
                parsed = replace(parsed, target_preset=self._profile.target_preset)
        else:
            raise ValueError("HardwareProfile 候选必须是对象或 HardwareProfile。")
        current_by_port = {
            channel.external_port: channel for channel in self._profile.channels
        }
        parsed = replace(
            parsed,
            channels=tuple(
                replace(
                    channel,
                    verification=current_by_port[channel.external_port].verification,
                )
                for channel in parsed.channels
            ),
        )
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

    def update_verification(
        self,
        external_port: int,
        verification: ChannelVerification,
        *,
        expected_revision: int,
        expected_fingerprint: str,
    ) -> HardwareProfile:
        """Atomically persist evidence without accepting any mapping mutation."""

        if not isinstance(verification, ChannelVerification):
            raise ValueError("verification 必须是 ChannelVerification。")
        if verification.status is VerificationStatus.PHYSICAL_VERIFIED:
            raise ValueError("普通验证接口不能写入现场验证证据。")
        if verification.status not in {
            VerificationStatus.MOCK_VERIFIED,
            VerificationStatus.FAILED,
            VerificationStatus.INCOMPLETE,
        }:
            raise ValueError("verification-only 事务不接受该验证状态。")
        return self._commit_verification(
            external_port,
            verification,
            expected_revision=expected_revision,
            expected_fingerprint=expected_fingerprint,
        )

    def update_physical_verification(
        self,
        external_port: int,
        *,
        contract: PhysicalVerificationContract,
        user_confirmed: bool,
        run_identity: str,
        expected_revision: int,
        expected_fingerprint: str,
        verified_at: str,
        note: str = "现场确认出气正确",
    ) -> HardwareProfile:
        """Commit physical evidence only from a matching completed safe contract."""

        if not isinstance(contract, PhysicalVerificationContract):
            raise ValueError("缺少可信现场验证完成契约。")
        if type(user_confirmed) is not bool:
            raise ValueError("用户确认状态必须是布尔值。")
        if not user_confirmed:
            raise ValueError("用户尚未正向确认出气正确。")
        if not contract.permits(
            external_port=external_port,
            revision=expected_revision,
            fingerprint=expected_fingerprint,
            run_identity=run_identity,
        ):
            raise ValueError("现场验证完成契约与当前运行不匹配。")
        evidence = ChannelVerification(
            status=VerificationStatus.PHYSICAL_VERIFIED,
            fingerprint=expected_fingerprint,
            verified_at=verified_at,
            note=note,
        )
        return self._commit_verification(
            external_port,
            evidence,
            expected_revision=expected_revision,
            expected_fingerprint=expected_fingerprint,
        )

    def _commit_verification(
        self,
        external_port: int,
        verification: ChannelVerification,
        *,
        expected_revision: int,
        expected_fingerprint: str,
    ) -> HardwareProfile:
        with self._lock:
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            try:
                disk_effective = _prefer_local_profile_connections(
                    _merge(self._base_config, local),
                    local,
                )
                disk_profile = HardwareProfile.from_config(disk_effective)
            except (TypeError, ValueError) as exc:
                raise StaleHardwareProfileRevisionError(
                    "磁盘配置在 revision 之外发生无效或冲突变化，"
                    "拒绝写入验证证据。"
                ) from exc
            if _without_verification(disk_profile) != _without_verification(self._profile):
                raise StaleHardwareProfileRevisionError(
                    "磁盘配置在 revision 之外发生变化，拒绝写入验证证据。"
                )
            port = int(external_port)
            current = disk_profile.registry.by_external_port(port)
            fingerprint = str(expected_fingerprint).strip().lower()
            if not fingerprint or current.mapping_fingerprint != fingerprint:
                raise StaleHardwareProfileRevisionError(
                    "验证结果的 mapping fingerprint 已过期，未写入证据。"
                )
            if verification.fingerprint != fingerprint:
                raise ValueError("验证证据 fingerprint 与当前映射不一致。")
            if (
                current.verification.status is VerificationStatus.PHYSICAL_VERIFIED
                and current.verification_valid
            ):
                raise ValueError("有效现场验证证据不得被模拟或失败结果降级覆盖。")

            channels = list(disk_profile.channels)
            channels[port - 1] = replace(current, verification=verification)
            updated = replace(disk_profile, channels=tuple(channels))
            before_mapping = tuple(
                (c.external_port, c.internal_valve, c.target, c.active_high)
                for c in disk_profile.channels
            )
            after_mapping = tuple(
                (c.external_port, c.internal_valve, c.target, c.active_high)
                for c in updated.channels
            )
            if after_mapping != before_mapping:
                raise RuntimeError("verification-only 事务不得修改映射。")

            next_local = copy.deepcopy(local)
            next_local = _with_connection_aliases(next_local, updated)
            next_local["hardware_profile"] = updated.to_dict()
            next_local["hardware_profile_revision"] = expected + 1
            self._atomic_write(next_local)
            self._profile = updated
            self._revision = expected + 1
            self._effective_config = _with_connection_aliases(
                _merge(self._base_config, next_local),
                updated,
            )
            return updated

    def prepare_save(
        self,
        candidate: HardwareProfile | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> HardwareProfile:
        """Validate a save without changing disk, revision, active, or LKG."""

        with self._lock:
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            return self._validate_candidate(candidate, local=local)

    def commit_prepared_save(
        self,
        prepared: HardwareProfile,
        *,
        expected_revision: int,
    ) -> HardwareProfile:
        """Commit a previously prepared candidate under the same CAS guard."""

        return self.save(prepared, expected_revision=expected_revision)

    def preview_rollback(
        self,
        *,
        expected_revision: int | None = None,
    ) -> HardwareProfile:
        """Return the LKG rollback candidate without flipping any state."""

        with self._lock:
            if self._last_known_good is None:
                raise RuntimeError("没有可回滚的 HardwareProfile。")
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            candidate = HardwareProfile.from_config(self._last_known_good.to_dict())
            candidate_effective = _merge(
                _merge(self._base_config, local),
                {"hardware_profile": candidate.to_dict()},
            )
            self._validate_devices(candidate, candidate_effective)
            return candidate

    def commit_prepared_rollback(
        self,
        prepared: HardwareProfile,
        *,
        expected_revision: int,
    ) -> HardwareProfile:
        with self._lock:
            expected = self._expected_revision(expected_revision)
            local = self._read_local()
            self._require_disk_revision(local, expected)
            if self._last_known_good is None:
                raise RuntimeError("没有可回滚的 HardwareProfile。")
            rollback_profile = HardwareProfile.from_config(
                self._last_known_good.to_dict()
            )
            if prepared != rollback_profile:
                raise StaleHardwareProfileRevisionError(
                    "rollback preview 已过期，未写入磁盘。"
                )
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
                _merge(self._base_config, next_local), rollback_profile
            )
            return rollback_profile

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
        targets = [channel.target for channel in profile.channels if channel.target]
        if profile.target_preset is not None:
            targets.extend(profile.target_preset.targets)
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


def _without_verification(profile: HardwareProfile) -> dict[str, Any]:
    value = profile.to_dict()
    for channel in value["channels"]:
        channel.pop("verification", None)
    return value


def _with_connection_aliases(
    config: Mapping[str, Any],
    profile: HardwareProfile,
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(config))
    connections = profile.connections
    merged["serial_port"] = connections.serial_port
    merged["ni_devices"] = list(connections.ni_device_ids)
    merged["alicat_unit_ids"] = connections.alicat_unit_ids
    valve_mapping = copy.deepcopy(dict(merged.get("valve_mapping") or {}))
    if profile.target_preset is not None:
        variants = copy.deepcopy(dict(valve_mapping.get("variants") or {}))
        variants[profile.target_preset.variant] = {
            str(port): profile.target_preset.target_for(port)
            for port in range(1, 21)
        }
        valve_mapping["variants"] = variants
    if profile.selector is not None:
        valve_mapping["selector"] = {
            "target": profile.selector.target,
            "safe_route": profile.selector.safe_route.value,
            "safe_level": profile.selector.safe_level,
            "odor_level": profile.selector.odor_level,
        }
        valve_mapping["master_valve"] = profile.selector.target
    merged["valve_mapping"] = valve_mapping
    return merged


def _prefer_local_profile_connections(
    effective: Mapping[str, Any],
    local: Mapping[str, Any],
) -> dict[str, Any]:
    """Let a canonical local profile beat legacy aliases inherited from defaults."""

    merged = copy.deepcopy(dict(effective))
    local_profile = local.get("hardware_profile")
    connections = (
        local_profile.get("connections")
        if isinstance(local_profile, Mapping)
        else None
    )
    if not isinstance(connections, Mapping):
        # A legacy local file predating nested connections must still beat the
        # nested defaults it was merged with.  Remove only the inherited block
        # so HardwareProfile reads the explicit legacy aliases.
        if any(key in local for key in ("serial_port", "ni_devices", "alicat_unit_ids")):
            profile = merged.get("hardware_profile")
            if isinstance(profile, Mapping):
                profile = copy.deepcopy(dict(profile))
                profile.pop("connections", None)
                merged["hardware_profile"] = profile
        return merged
    explicit_aliases = {
        key: local[key]
        for key in ("serial_port", "ni_devices", "alicat_unit_ids")
        if key in local
    }
    if explicit_aliases:
        if "hardware_profile_revision" not in local:
            # Migration path for pre-revision local files: their top-level
            # values were the only writable connection settings, even when a
            # copied default profile happened to contain nested defaults.
            profile = copy.deepcopy(dict(merged["hardware_profile"]))
            canonical = copy.deepcopy(dict(profile.get("connections") or {}))
            canonical.update(copy.deepcopy(explicit_aliases))
            profile["connections"] = canonical
            merged["hardware_profile"] = profile
            connections = canonical
        else:
            conflicts = [
                key
                for key, value in explicit_aliases.items()
                if key in connections and connections[key] != value
            ]
            if conflicts:
                raise ValueError(
                    "connections 与顶层兼容别名冲突："
                    + "、".join(conflicts)
                )
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
