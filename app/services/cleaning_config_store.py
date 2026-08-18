from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.models import CleaningConfigSnapshot


class CleaningConfigStore:
    """Validate, atomically persist, and publish the active cleaning override."""

    def __init__(
        self,
        *,
        effective_config: Mapping[str, Any],
        local_config_path: Path,
        available_channels: Mapping[int, str],
    ) -> None:
        self._local_config_path = Path(local_config_path)
        self._available_channels = {
            int(channel): str(target) for channel, target in available_channels.items()
        }
        local = self._read_local()
        self._effective_config = _merge(copy.deepcopy(dict(effective_config)), local)
        self._snapshot = CleaningConfigSnapshot.from_effective_config(
            self._effective_config,
            available_channels=self._available_channels,
        )

    @property
    def snapshot(self) -> CleaningConfigSnapshot:
        return self._snapshot

    def save(
        self,
        *,
        selected_channels: tuple[int, ...] | list[int],
        flow_sccm: float,
        open_duration_s: float,
        cycles: int,
    ) -> CleaningConfigSnapshot:
        override, candidate_effective, candidate_snapshot = self._candidate(
            selected_channels=selected_channels,
            flow_sccm=flow_sccm,
            open_duration_s=open_duration_s,
            cycles=cycles,
        )
        local = self._read_local()
        next_local = _merge(local, {"cleaning": override})
        self._atomic_write(next_local)
        self._effective_config = candidate_effective
        self._snapshot = candidate_snapshot
        return candidate_snapshot

    def validate_candidate(
        self,
        *,
        selected_channels: tuple[int, ...] | list[int],
        flow_sccm: float,
        open_duration_s: float,
        cycles: int,
    ) -> CleaningConfigSnapshot:
        return self._candidate(
            selected_channels=selected_channels,
            flow_sccm=flow_sccm,
            open_duration_s=open_duration_s,
            cycles=cycles,
        )[2]

    def _candidate(
        self,
        *,
        selected_channels: tuple[int, ...] | list[int],
        flow_sccm: float,
        open_duration_s: float,
        cycles: int,
    ) -> tuple[dict[str, Any], dict[str, Any], CleaningConfigSnapshot]:
        override = {
            "selected_channels": [int(channel) for channel in selected_channels],
            "flow_sccm": float(flow_sccm),
            "open_duration_s": float(open_duration_s),
            "cycles": cycles,
        }
        candidate_effective = _merge(
            self._effective_config,
            {"cleaning": override},
        )
        candidate_snapshot = CleaningConfigSnapshot.from_effective_config(
            candidate_effective,
            available_channels=self._available_channels,
        )
        return override, candidate_effective, candidate_snapshot

    def _read_local(self) -> dict[str, Any]:
        if not self._local_config_path.exists():
            return {}
        with self._local_config_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("local_config.json 顶层必须是 JSON 对象。")
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


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
