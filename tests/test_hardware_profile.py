from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import (
    AppState,
    ChannelRegistry,
    FlowSetpoints,
    HardwareConnectionConfig,
    HardwareProfile,
    VerificationStatus,
    normalize_digital_target,
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
    assert profile.connections == HardwareConnectionConfig(
        serial_port=None,
        ni_device_ids=("Dev1", "Dev2"),
        alicat_a_unit_id="a",
        alicat_b_unit_id="b",
        alicat_c_unit_id="c",
    )


def test_legacy_top_level_connections_are_loaded_and_round_trip_in_profile() -> None:
    config = _default_profile().to_dict()
    config.pop("connections")
    wrapper = {
        "hardware_profile": config,
        "serial_port": "com9",
        "ni_devices": ["LabDev1", "LabDev2"],
        "alicat_unit_ids": {"A": "1", "B": "2", "C": "3"},
    }

    profile = HardwareProfile.from_config(wrapper)

    assert profile.connections.serial_port == "COM9"
    assert profile.connections.ni_device_ids == ("LabDev1", "LabDev2")
    assert profile.connections.alicat_unit_ids == {"A": "1", "B": "2", "C": "3"}
    assert HardwareProfile.from_config(profile.to_dict()).connections == profile.connections


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"serial_port": "ttyUSB0"}, "COM1–COM256"),
        ({"serial_port": "COM0"}, "COM1–COM256"),
        ({"ni_devices": []}, "非空数组"),
        ({"ni_devices": ["Dev1", "dev1"]}, "不得重复"),
        ({"ni_devices": ["Dev/1"]}, "仅允许"),
        ({"alicat_unit_ids": {"A": "a", "B": "A", "C": "c"}}, "不得重复"),
        ({"alicat_unit_ids": {"A": "aa", "B": "b", "C": "c"}}, "单个 ASCII"),
    ),
)
def test_connection_config_strictly_rejects_invalid_identifiers(changes, message) -> None:
    raw = _default_profile().to_dict()
    raw["connections"].update(changes)

    with pytest.raises(ValueError, match=message):
        HardwareProfile.from_config(raw)


@pytest.mark.parametrize(
    "target",
    (
        "Dev1/ai0",
        "Dev1/foo",
        "Dev1/P0.8",
        "Dev1/P1.4",
        "Dev1/P2.0",
        "Dev1/port0/line0:7",
    ),
)
def test_ni_do_target_parser_rejects_non_do_and_out_of_range_targets(target) -> None:
    with pytest.raises(ValueError):
        normalize_digital_target(target)


def test_ni_do_target_parser_accepts_alias_and_canonical_form() -> None:
    assert normalize_digital_target("Dev1/P0.7") == "dev1/port0/line7"
    assert normalize_digital_target("dev1/port1/line3") == "dev1/port1/line3"


def test_registry_refuses_unverified_or_disabled_external_ports() -> None:
    registry = _default_profile().registry

    assert registry.target_for_external_port(2) is None
    assert registry.target_for_external_port(2, allow_mock=True) == "Dev1/P0.1"
    assert registry.target_for_external_port(1) is None
    with pytest.raises(KeyError, match="未知机外气口"):
        registry.by_external_port(21)


def test_production_availability_requires_matching_physical_verification() -> None:
    profile = _default_profile()
    original = profile.registry.by_external_port(2)
    pending = replace(original, verification=replace(original.verification, status=VerificationStatus.PENDING))
    incomplete_physical = replace(
        original,
        verification=replace(
            original.verification,
            status=VerificationStatus.PHYSICAL_VERIFIED,
            fingerprint=original.mapping_fingerprint,
        ),
    )
    physical = replace(
        original,
        verification=replace(
            original.verification,
            status=VerificationStatus.PHYSICAL_VERIFIED,
            fingerprint=original.mapping_fingerprint,
            run_identity="physical-run-1",
            profile_revision=0,
            ni_target=original.target,
            flow_setpoint_sccm=1500,
            flow_readback_sccm=1499,
            open_command_id="physical-run-1:open",
            close_command_id="physical-run-1:close",
            opened_at_ns=100,
            closed_at_ns=200,
            action_completed=True,
            safe_closed=True,
            authorized=True,
            user_confirmed=True,
        ),
    )

    assert pending.enabled and not pending.available
    assert not pending.verification_valid_for(allow_mock=True)
    assert original.verification.status is VerificationStatus.MOCK_VERIFIED
    assert not original.available
    assert original.verification_valid_for(allow_mock=True)
    assert not incomplete_physical.available
    assert physical.available
    assert physical.verification_valid_for(allow_mock=True)
    assert not replace(physical, enabled=False).available


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


def test_flow_setpoints_keeps_independent_b_and_enforces_domain_and_profile_limits() -> None:
    setpoints = FlowSetpoints(
        sample_a_sccm=250,
        main_b_sccm=750,
        vacuum_c_sccm=100,
    )
    assert setpoints.main_b_sccm == 750
    assert setpoints.derived_total_sccm == 1000
    assert setpoints.as_mfc_setpoints() == (("A", 250.0), ("B", 750.0), ("C", 100.0))

    with pytest.raises(ValueError, match="不得为负数"):
        FlowSetpoints(sample_a_sccm=101, main_b_sccm=-1, vacuum_c_sccm=0)
    with pytest.raises(ValueError, match=r"A\+B 总送风"):
        _default_profile().flow_setpoints(
            sample_a_sccm=2501,
            main_b_sccm=2500,
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


def test_standard_preset_resolves_controller_channel_and_flags_historical_custom_target() -> None:
    profile = _default_profile()

    assert profile.resolved_target_for(10) == "Dev1/P1.1"
    assert profile.channel_uses_custom_target(4) is False
    raw = profile.to_dict()
    raw["channels"][3]["target"] = "Dev2/P0.2"
    custom = HardwareProfile.from_config(
        {**_default_profile_config(), "hardware_profile": raw}
    )
    assert custom.channel_uses_custom_target(4) is True


def test_canonical_target_preset_round_trips_without_legacy_mapping() -> None:
    profile = _default_profile()
    raw = profile.to_dict()

    assert raw["target_preset"]["variant"] == "20-channel"
    assert raw["target_preset"]["targets"]["10"] == "Dev1/P1.1"
    parsed = HardwareProfile.from_config(raw)
    assert parsed.target_preset == profile.target_preset




def _default_profile_config() -> dict:
    return json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
