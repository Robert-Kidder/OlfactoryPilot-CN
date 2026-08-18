from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import (
    AppState,
    ChannelRegistry,
    FlowSetpoints,
    HardwareProfile,
    VerificationStatus,
)


def _default_profile() -> HardwareProfile:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    return HardwareProfile.from_config(config)


def test_default_profile_has_fixed_twenty_ports_and_expected_available_mapping() -> None:
    profile = _default_profile()

    assert tuple(channel.external_port for channel in profile.channels) == tuple(range(1, 21))
    assert profile.registry.available_external_ports() == ()
    assert profile.registry.available_external_ports(allow_mock=True) == (
        2,
        4,
        6,
        8,
        12,
        14,
        16,
        18,
    )
    assert profile.registry.internal_valve_targets(allow_mock=True) == {
        2: "Dev1/P0.1",
        3: "Dev1/P0.2",
        4: "Dev1/P0.3",
        5: "Dev1/P0.4",
        6: "Dev1/P0.5",
        7: "Dev1/P0.6",
        8: "Dev1/P0.7",
        9: "Dev1/P1.0",
    }
    assert profile.selector is not None
    assert profile.selector.target == "Dev2/P1.0"


def test_registry_refuses_unverified_or_disabled_external_ports() -> None:
    registry = _default_profile().registry

    assert registry.target_for_external_port(2) is None
    assert registry.target_for_external_port(2, allow_mock=True) == "Dev1/P0.1"
    assert registry.target_for_external_port(1) is None
    with pytest.raises(KeyError, match="未知机外气口"):
        registry.by_external_port(21)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"channels": []}, "1–20"),
        ({"schema_version": 2}, "仅支持 1"),
        ({"unknown": True}, "未知字段"),
    ],
)
def test_profile_strict_schema_rejects_invalid_candidates(changes, message) -> None:
    raw = _default_profile().to_dict()
    raw.update(changes)

    with pytest.raises(ValueError, match=message):
        HardwareProfile.from_config(raw)


@pytest.mark.parametrize("invalid", ["1", 1.0, True])
def test_profile_integer_fields_require_json_integer_type(invalid) -> None:
    raw = _default_profile().to_dict()
    raw["schema_version"] = invalid
    with pytest.raises(ValueError, match="必须是整数"):
        HardwareProfile.from_config(raw)

    raw = _default_profile().to_dict()
    raw["channels"][0]["external_port"] = invalid
    with pytest.raises(ValueError, match="必须是整数"):
        HardwareProfile.from_config(raw)


def test_registry_rejects_duplicate_internal_valve_target_and_selector_collision() -> None:
    raw = _default_profile().to_dict()
    raw["channels"][3]["internal_valve"] = 2
    with pytest.raises(ValueError, match="内部阀位不得重复"):
        HardwareProfile.from_config(raw)

    raw = _default_profile().to_dict()
    raw["channels"][3]["target"] = "dev1/port0/line1"
    with pytest.raises(ValueError, match="NI target 不得重复"):
        HardwareProfile.from_config(raw)

    raw = _default_profile().to_dict()
    raw["channels"][3]["target"] = "Dev2/P1.0"
    with pytest.raises(ValueError, match="selector target"):
        HardwareProfile.from_config(raw)


def test_mapping_fingerprint_ignores_display_name_but_includes_mapping_and_polarity() -> None:
    channel = _default_profile().registry.by_external_port(2)

    assert replace(channel, display_name="新名称").mapping_fingerprint == channel.mapping_fingerprint
    assert replace(channel, active_high=False).mapping_fingerprint != channel.mapping_fingerprint
    assert replace(channel, target="Dev1/P1.1").mapping_fingerprint != channel.mapping_fingerprint


def test_flow_setpoints_derives_b_and_enforces_domain_and_profile_limits() -> None:
    setpoints = FlowSetpoints(
        total_sccm=1000,
        sample_a_sccm=250,
        vacuum_c_sccm=100,
    )
    assert setpoints.main_b_sccm == 750
    assert setpoints.as_mfc_setpoints() == (("A", 250.0), ("B", 750.0), ("C", 100.0))

    with pytest.raises(ValueError, match="0 ≤ A ≤ T"):
        FlowSetpoints(total_sccm=100, sample_a_sccm=101, vacuum_c_sccm=0)
    with pytest.raises(ValueError, match="派生值"):
        FlowSetpoints(
            total_sccm=100,
            sample_a_sccm=25,
            vacuum_c_sccm=0,
            main_b_sccm=74,
        )
    with pytest.raises(ValueError, match="批准范围"):
        _default_profile().flow_setpoints(
            total_sccm=5001,
            sample_a_sccm=0,
            vacuum_c_sccm=0,
        )


def test_app_state_uses_hardware_profile_as_runtime_mapping_source() -> None:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    config["valve_mapping"]["variants"]["20-channel"]["2"] = "Dev9/P9.9"

    state = AppState.from_config(config)

    assert isinstance(state.channel_registry, ChannelRegistry)
    assert state.resolve_external_port(2) is None
    state.simulation_mode = True
    assert state.resolve_external_port(2) == "Dev1/P0.1"
    assert state.resolve_external_port(1) is None
    assert state.master_valve_line == "Dev2/P1.0"


def test_stale_verified_fingerprint_is_exposed_as_mapping_changed() -> None:
    raw = _default_profile().to_dict()
    raw["channels"][1]["target"] = "Dev1/P1.1"

    changed = HardwareProfile.from_config(raw).registry.by_external_port(2)

    assert changed.verification.status is VerificationStatus.MAPPING_CHANGED
    assert changed.verification_valid is False
