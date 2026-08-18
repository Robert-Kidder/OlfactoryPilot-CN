from __future__ import annotations

import json
import os

import pytest

from app.services.cleaning_config_store import CleaningConfigStore


def _effective_config() -> dict:
    return {
        "serial_port": "COM6",
        "cleaning": {
            "enabled": True,
            "gas_label": "Air",
            "flow_channel": "A",
            "default_flow_sccm": 1500,
            "max_approved_flow_sccm": 1500,
            "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
            "default_open_duration_s": 10,
            "max_open_duration_s": 120,
            "default_cycles": 3,
            "max_cycles": 20,
            "parallel_open_limit": 1,
            "default_channels": [2, 3],
            "external_labels": {"2": "2", "3": "4"},
        },
    }


def test_cleaning_override_atomically_preserves_unrelated_local_fields(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    local_path.write_text(
        json.dumps({"serial_port": "COM9", "calibration": {"gain": 2.5}}),
        encoding="utf-8",
    )
    store = CleaningConfigStore(
        effective_config=_effective_config(),
        local_config_path=local_path,
        available_channels={2: "Dev1/P0.1", 3: "Dev1/P0.2"},
    )

    published = store.save(
        selected_channels=(3,),
        flow_sccm=1200,
        open_duration_s=8,
        cycles=2,
    )

    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["serial_port"] == "COM9"
    assert persisted["calibration"] == {"gain": 2.5}
    assert persisted["cleaning"] == {
        "selected_channels": [3],
        "flow_sccm": 1200.0,
        "open_duration_s": 8.0,
        "cycles": 2,
    }
    assert published.selected_channels == (3,)
    assert store.snapshot is published
    assert not list(tmp_path.glob(".local_config.json.*.tmp"))


def test_atomic_replace_failure_keeps_disk_and_active_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    local_path = tmp_path / "local_config.json"
    original = {"serial_port": "COM9", "cleaning": {"selected_channels": [2]}}
    local_path.write_text(json.dumps(original), encoding="utf-8")
    store = CleaningConfigStore(
        effective_config=_effective_config(),
        local_config_path=local_path,
        available_channels={2: "Dev1/P0.1", 3: "Dev1/P0.2"},
    )
    before = store.snapshot

    def fail_replace(_source, _destination):
        raise OSError("replace fault")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace fault"):
        store.save(
            selected_channels=(3,),
            flow_sccm=1000,
            open_duration_s=5,
            cycles=1,
        )

    assert json.loads(local_path.read_text(encoding="utf-8")) == original
    assert store.snapshot is before
    assert not list(tmp_path.glob(".local_config.json.*.tmp"))


def test_invalid_candidate_has_no_disk_side_effect(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    local_path.write_text(json.dumps({"serial_port": "COM9"}), encoding="utf-8")
    store = CleaningConfigStore(
        effective_config=_effective_config(),
        local_config_path=local_path,
        available_channels={2: "Dev1/P0.1", 3: "Dev1/P0.2"},
    )
    before = local_path.read_bytes()

    with pytest.raises(ValueError, match="批准范围"):
        store.save(
            selected_channels=(2,),
            flow_sccm=1501,
            open_duration_s=10,
            cycles=3,
        )

    assert local_path.read_bytes() == before
