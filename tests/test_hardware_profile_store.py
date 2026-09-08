from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import (
    ChannelVerification,
    HardwareProfile,
    PhysicalVerificationContract,
    VerificationStatus,
)
from app.services import HardwareProfileStore, StaleHardwareProfileRevisionError


def _default_config() -> dict:
    return json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))


def test_store_merges_default_and_local_and_preserves_unrelated_fields(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    profile = HardwareProfile.from_config(_default_config()).to_dict()
    profile["profile_name"] = "本机方案"
    local_path.write_text(
        json.dumps({"serial_port": "COM9", "hardware_profile": profile}),
        encoding="utf-8",
    )

    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    candidate = store.profile.to_dict()
    candidate["profile_name"] = "本机方案 V2"
    saved = store.save(candidate)

    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["serial_port"] == "COM9"
    assert persisted["hardware_profile"]["profile_name"] == "本机方案 V2"
    assert persisted["hardware_profile_last_known_good"]["profile_name"] == "本机方案"
    assert persisted["hardware_profile_revision"] == 1
    assert store.revision == 1
    assert saved.profile_name == "本机方案 V2"
    assert not list(tmp_path.glob(".local_config.json.*.tmp"))


def test_save_invalid_candidate_has_no_disk_or_active_side_effect(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    original = {"serial_port": "COM9"}
    local_path.write_text(json.dumps(original), encoding="utf-8")
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    before = store.profile
    invalid = before.to_dict()
    invalid["channels"][3]["target"] = "Dev1/P0.1"

    with pytest.raises(ValueError, match="NI target 不得重复"):
        store.save(invalid)

    assert json.loads(local_path.read_text(encoding="utf-8")) == original
    assert store.profile is before


def test_atomic_replace_failure_keeps_disk_and_published_profile(
    tmp_path,
    monkeypatch,
) -> None:
    local_path = tmp_path / "local_config.json"
    original = {"serial_port": "COM9"}
    local_path.write_text(json.dumps(original), encoding="utf-8")
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    before = store.profile
    candidate = before.to_dict()
    candidate["profile_name"] = "不会发布"

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(OSError, match="fault"):
        store.save(candidate)

    assert json.loads(local_path.read_text(encoding="utf-8")) == original
    assert store.profile is before
    assert not list(tmp_path.glob(".local_config.json.*.tmp"))


def test_explicit_rollback_is_persistent_and_swaps_last_known_good(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    candidate = store.profile.to_dict()
    candidate["profile_name"] = "候选方案"
    store.save(candidate)

    rolled_back = store.rollback(expected_revision=1)

    assert rolled_back.profile_name == "默认 8 气口方案"
    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["hardware_profile"]["profile_name"] == "默认 8 气口方案"
    assert persisted["hardware_profile_last_known_good"]["profile_name"] == "候选方案"
    assert persisted["hardware_profile_revision"] == 2
    restarted = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    assert restarted.profile.profile_name == "默认 8 气口方案"
    assert restarted.rollback_available is True
    assert restarted.revision == 2


def test_legacy_last_known_good_without_wiring_snapshot_is_not_rollbackable(
    tmp_path,
) -> None:
    config = _default_config()
    current = HardwareProfile.from_config(config).to_dict()
    legacy_last_good = json.loads(json.dumps(current))
    legacy_last_good.pop("target_preset")
    local_path = tmp_path / "local_config.json"
    local_path.write_text(
        json.dumps(
            {
                "hardware_profile": current,
                "hardware_profile_last_known_good": legacy_last_good,
            }
        ),
        encoding="utf-8",
    )

    store = HardwareProfileStore(
        default_config=config,
        local_config_path=local_path,
    )

    assert store.rollback_available is False


def test_rename_keeps_verification_while_mapping_change_forces_reverification(tmp_path) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    renamed = store.profile.to_dict()
    renamed["channels"][1]["display_name"] = "薄荷"
    saved = store.save(renamed)
    assert saved.registry.by_external_port(2).verification.status is VerificationStatus.MOCK_VERIFIED

    changed = saved.to_dict()
    changed["channels"][1]["target"] = "Dev1/P1.1"
    saved = store.save(changed)
    channel = saved.registry.by_external_port(2)
    assert channel.verification.status is VerificationStatus.MAPPING_CHANGED
    assert channel.verification_valid is False


def test_stale_expected_revision_never_writes_or_publishes(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    candidate = store.profile.to_dict()
    candidate["profile_name"] = "版本一"
    store.save(candidate, expected_revision=0)
    before = local_path.read_bytes()
    active = store.profile

    candidate["profile_name"] = "过期覆盖"
    with pytest.raises(StaleHardwareProfileRevisionError, match="已过期"):
        store.save(candidate, expected_revision=0)

    assert local_path.read_bytes() == before
    assert store.profile is active
    assert store.revision == 1


def test_two_store_concurrent_save_serializes_and_rejects_silent_overwrite(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    first = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    second = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def save(store: HardwareProfileStore, name: str) -> None:
        candidate = store.profile.to_dict()
        candidate["profile_name"] = name
        barrier.wait()
        try:
            store.save(candidate, expected_revision=0)
        except StaleHardwareProfileRevisionError:
            outcome = "stale"
        else:
            outcome = "saved"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=save, args=(first, "并发一")),
        threading.Thread(target=save, args=(second, "并发二")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["saved", "stale"]
    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["hardware_profile_revision"] == 1
    assert persisted["hardware_profile"]["profile_name"] in {"并发一", "并发二"}


def test_candidate_rejects_enabled_target_on_unregistered_ni_device(tmp_path) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    candidate = store.profile.to_dict()
    candidate["channels"][1]["target"] = "Dev9/P0.1"

    with pytest.raises(ValueError, match="未登记设备.*dev9"):
        store.save(candidate, expected_revision=0)


def test_connections_share_profile_revision_atomic_save_restart_and_rollback(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    candidate = store.profile.to_dict()
    candidate["connections"] = {
        "serial_port": "COM18",
        "ni_devices": ["RackA", "RackB"],
        "alicat_unit_ids": {"A": "1", "B": "2", "C": "3"},
    }
    candidate["selector"]["target"] = "RackB/P1.0"
    for channel in candidate["channels"]:
        if channel["target"]:
            channel["target"] = channel["target"].replace("Dev1", "RackA")
    candidate["target_preset"]["targets"] = {
        key: value.replace("Dev1", "RackA").replace("Dev2", "RackB")
        for key, value in candidate["target_preset"]["targets"].items()
    }

    saved = store.save(candidate, expected_revision=0)

    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["hardware_profile_revision"] == 1
    assert persisted["hardware_profile"]["connections"]["serial_port"] == "COM18"
    assert persisted["serial_port"] == "COM18"
    assert persisted["ni_devices"] == ["RackA", "RackB"]
    assert persisted["alicat_unit_ids"] == {"A": "1", "B": "2", "C": "3"}
    assert saved.connections.serial_port == "COM18"

    restarted = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )
    assert restarted.revision == 1
    assert restarted.profile.connections == saved.connections
    assert restarted.effective_config["serial_port"] == "COM18"

    rolled_back = restarted.rollback(expected_revision=1)
    assert rolled_back.connections.serial_port is None
    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["hardware_profile_revision"] == 2
    assert persisted["serial_port"] is None
    assert persisted["ni_devices"] == ["Dev1", "Dev2"]


def test_restart_prefers_canonical_local_connections_over_inherited_legacy_defaults(
    tmp_path,
) -> None:
    local_path = tmp_path / "local_config.json"
    profile = HardwareProfile.from_config(_default_config()).to_dict()
    profile["connections"] = {
        "serial_port": "COM22",
        "ni_devices": ["Dev1", "Dev2"],
        "alicat_unit_ids": {"A": "1", "B": "2", "C": "3"},
    }
    local_path.write_text(
        json.dumps({"hardware_profile": profile, "hardware_profile_revision": 4}),
        encoding="utf-8",
    )

    restarted = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=local_path,
    )

    assert restarted.revision == 4
    assert restarted.profile.connections.serial_port == "COM22"
    assert restarted.profile.connections.alicat_unit_ids == {"A": "1", "B": "2", "C": "3"}
    assert restarted.effective_config["serial_port"] == "COM22"


def test_verification_only_transaction_is_cas_persistent_and_mapping_immutable(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    before = store.profile.registry.by_external_port(4)
    evidence = ChannelVerification(
        status=VerificationStatus.MOCK_VERIFIED,
        fingerprint=before.mapping_fingerprint,
        verified_at="2026-09-03T12:00:00+08:00",
    )

    updated = store.update_verification(
        4,
        evidence,
        expected_revision=0,
        expected_fingerprint=before.mapping_fingerprint,
    )

    after = updated.registry.by_external_port(4)
    assert (after.external_port, after.internal_valve, after.target, after.active_high) == (
        before.external_port,
        before.internal_valve,
        before.target,
        before.active_high,
    )
    assert store.revision == 1
    restarted = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    assert restarted.profile.registry.by_external_port(4).verification == evidence
    with pytest.raises(StaleHardwareProfileRevisionError):
        store.update_verification(
            4,
            evidence,
            expected_revision=0,
            expected_fingerprint=before.mapping_fingerprint,
        )

    disk_before = local_path.read_bytes()
    stale_evidence = ChannelVerification(
        status=VerificationStatus.FAILED,
        fingerprint="0" * 64,
    )
    with pytest.raises(StaleHardwareProfileRevisionError, match="fingerprint"):
        restarted.update_verification(
            4,
            stale_evidence,
            expected_revision=1,
            expected_fingerprint="0" * 64,
        )
    assert local_path.read_bytes() == disk_before


def test_default_mapping_and_remap_survive_restart_with_verification_invalidated(
    tmp_path,
) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    expected = {
        2: 2,
        4: 3,
        6: 4,
        8: 5,
        12: 6,
        14: 7,
        16: 8,
        18: 9,
    }
    assert {
        channel.external_port: channel.internal_valve
        for channel in store.profile.channels
        if channel.enabled
    } == expected

    candidate = store.profile.to_dict()
    candidate["channels"][3]["display_name"] = "薄荷"
    candidate["channels"][3]["internal_valve"] = 10
    candidate["channels"][3]["target"] = store.profile.resolved_target_for(10)
    saved = store.save(candidate, expected_revision=0)
    assert saved.registry.by_external_port(4).verification.status is (
        VerificationStatus.MAPPING_CHANGED
    )

    restarted = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    port04 = restarted.profile.registry.by_external_port(4)
    assert restarted.revision == 1
    assert port04.display_name == "薄荷"
    assert port04.internal_valve == 10
    assert port04.target == "Dev1/P1.1"
    assert port04.verification.status is VerificationStatus.MAPPING_CHANGED
    assert {
        channel.external_port
        for channel in restarted.profile.channels
        if channel.enabled
    } == set(expected)


def test_unbound_line_edit_persists_and_is_used_by_later_mapping(tmp_path) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    candidate = store.profile.to_dict()
    candidate["target_preset"]["targets"]["10"] = "Dev2/P1.1"
    first = store.save(candidate, expected_revision=0)
    assert first.target_preset.target_for(10) == "Dev2/P1.1"

    restarted = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    mapped = restarted.profile.to_dict()
    mapped["channels"][3]["internal_valve"] = 10
    mapped["channels"][3]["target"] = restarted.profile.resolved_target_for(10)
    saved = restarted.save(mapped, expected_revision=1)

    assert saved.registry.by_external_port(4).target == "Dev2/P1.1"
    persisted = json.loads(local_path.read_text(encoding="utf-8"))
    assert persisted["valve_mapping"]["variants"]["20-channel"]["10"] == "Dev2/P1.1"


def test_verification_transaction_rejects_unconfirmed_physical_status(tmp_path) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    channel = store.profile.registry.by_external_port(2)
    evidence = ChannelVerification(
        status=VerificationStatus.PHYSICAL_VERIFIED,
        fingerprint=channel.mapping_fingerprint,
    )

    with pytest.raises(ValueError, match="普通验证接口"):
        store.update_verification(
            2,
            evidence,
            expected_revision=0,
            expected_fingerprint=channel.mapping_fingerprint,
        )


def test_physical_evidence_requires_matching_completed_safe_contract_and_confirmation(
    tmp_path,
) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    channel = store.profile.registry.by_external_port(2)
    contract = PhysicalVerificationContract(
        run_identity="physical-run-1",
        external_port=2,
        revision=0,
        fingerprint=channel.mapping_fingerprint,
        action_completed=True,
        safe_closed=True,
        authorized=True,
    )

    with pytest.raises(ValueError, match="尚未正向确认"):
        store.update_physical_verification(
            2,
            contract=contract,
            user_confirmed=False,
            run_identity="physical-run-1",
            expected_revision=0,
            expected_fingerprint=channel.mapping_fingerprint,
            verified_at="2026-09-08T12:00:00+08:00",
        )

    with pytest.raises(ValueError, match="布尔值"):
        store.update_physical_verification(
            2,
            contract=contract,
            user_confirmed="false",
            run_identity="physical-run-1",
            expected_revision=0,
            expected_fingerprint=channel.mapping_fingerprint,
            verified_at="2026-09-08T12:00:00+08:00",
        )

    updated = store.update_physical_verification(
        2,
        contract=contract,
        user_confirmed=True,
        run_identity="physical-run-1",
        expected_revision=0,
        expected_fingerprint=channel.mapping_fingerprint,
        verified_at="2026-09-08T12:00:00+08:00",
    )
    verified = updated.registry.by_external_port(2)
    assert verified.verification.status is VerificationStatus.PHYSICAL_VERIFIED
    assert verified.available is True


@pytest.mark.parametrize(
    "contract_change,call_change",
    (
        ({"authorized": False}, {}),
        ({"action_completed": False}, {}),
        ({"safe_closed": False}, {}),
        ({"external_port": 4}, {}),
        ({"revision": 1}, {}),
        ({"fingerprint": "f" * 64}, {}),
        ({"run_identity": "other-run"}, {}),
        ({}, {"run_identity": "other-run"}),
        ({}, {"expected_revision": 1}),
        ({}, {"expected_fingerprint": "f" * 64}),
    ),
)
def test_physical_evidence_rejects_each_incomplete_or_mismatched_contract_field(
    tmp_path,
    contract_change,
    call_change,
) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    channel = store.profile.registry.by_external_port(2)
    original_verification = channel.verification
    contract = PhysicalVerificationContract(
        run_identity="physical-run-1",
        external_port=2,
        revision=0,
        fingerprint=channel.mapping_fingerprint,
        action_completed=True,
        safe_closed=True,
        authorized=True,
    )
    contract = replace(contract, **contract_change)
    call = {
        "run_identity": "physical-run-1",
        "expected_revision": 0,
        "expected_fingerprint": channel.mapping_fingerprint,
        **call_change,
    }

    with pytest.raises(ValueError, match="不匹配"):
        store.update_physical_verification(
            2,
            contract=contract,
            user_confirmed=True,
            verified_at="2026-09-08T12:00:00+08:00",
            **call,
        )
    assert store.revision == 0
    assert store.profile.registry.by_external_port(2).verification == original_verification


@pytest.mark.parametrize("field", ("authorized", "action_completed", "safe_closed"))
def test_physical_contract_requires_strict_boolean_state(field) -> None:
    values = {
        "run_identity": "physical-run-1",
        "external_port": 2,
        "revision": 0,
        "fingerprint": "a" * 64,
        "action_completed": True,
        "safe_closed": True,
        "authorized": True,
    }
    values[field] = "false"

    with pytest.raises(ValueError, match="布尔值"):
        PhysicalVerificationContract(**values)


def test_normal_save_cannot_inject_or_replace_verification_evidence(tmp_path) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    channel = store.profile.registry.by_external_port(2)
    accepted = ChannelVerification(
        status=VerificationStatus.MOCK_VERIFIED,
        fingerprint=channel.mapping_fingerprint,
    )
    store.update_verification(
        2,
        accepted,
        expected_revision=0,
        expected_fingerprint=channel.mapping_fingerprint,
    )

    forged = store.profile.to_dict()
    forged["channels"][1]["verification"] = {
        "status": VerificationStatus.FAILED.value,
        "fingerprint": channel.mapping_fingerprint,
        "note": "caller controlled",
    }
    saved = store.save(forged, expected_revision=1)

    assert saved.registry.by_external_port(2).verification == accepted


def test_mapping_save_invalidates_current_evidence_even_if_candidate_forges_physical(
    tmp_path,
) -> None:
    store = HardwareProfileStore(
        default_config=_default_config(),
        local_config_path=tmp_path / "local_config.json",
    )
    channel = store.profile.registry.by_external_port(2)
    store.update_verification(
        2,
        ChannelVerification(
            status=VerificationStatus.MOCK_VERIFIED,
            fingerprint=channel.mapping_fingerprint,
        ),
        expected_revision=0,
        expected_fingerprint=channel.mapping_fingerprint,
    )
    candidate = store.profile.to_dict()
    candidate["channels"][1]["internal_valve"] = 10
    candidate["channels"][1]["target"] = "Dev1/P1.1"
    candidate["channels"][1]["verification"] = {
        "status": VerificationStatus.PHYSICAL_VERIFIED.value,
        "fingerprint": "f" * 64,
    }

    saved = store.save(candidate, expected_revision=1)

    assert saved.registry.by_external_port(2).verification.status is (
        VerificationStatus.MAPPING_CHANGED
    )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("profile", "profile_name", "externally-edited"),
        ("channel", "display_name", "external-alias"),
        ("channel", "enabled", False),
        ("selector", "safe_level", True),
        ("connections", "serial_port", "COM18"),
        ("limits", "total", 4321.0),
    ),
)
def test_verification_cas_rejects_same_revision_nonverification_disk_drift(
    tmp_path, section: str, field: str, value
) -> None:
    local_path = tmp_path / "local_config.json"
    store = HardwareProfileStore(
        default_config=_default_config(), local_config_path=local_path
    )
    store.save(store.profile, expected_revision=0)
    channel = store.profile.registry.by_external_port(2)
    before_verification = channel.verification
    disk = json.loads(local_path.read_text(encoding="utf-8"))
    profile = disk["hardware_profile"]
    if section == "channel":
        profile["channels"][1][field] = value
    elif section == "selector":
        profile["selector"][field] = value
    elif section == "connections":
        profile["connections"][field] = value
    elif section == "limits":
        profile["flow_limits_sccm"][field] = value
    else:
        profile[field] = value
    local_path.write_text(json.dumps(disk), encoding="utf-8")

    with pytest.raises(StaleHardwareProfileRevisionError, match="revision"):
        store.update_verification(
            2,
            ChannelVerification(
                status=VerificationStatus.MOCK_VERIFIED,
                fingerprint=channel.mapping_fingerprint,
            ),
            expected_revision=1,
            expected_fingerprint=channel.mapping_fingerprint,
        )

    assert store.revision == 1
    assert store.profile.registry.by_external_port(2).verification == before_verification


def test_valid_physical_evidence_cannot_be_downgraded(tmp_path) -> None:
    config = _default_config()
    profile = HardwareProfile.from_config(config)
    channels = list(profile.channels)
    channel = channels[1]
    channels[1] = replace(
        channel,
        verification=ChannelVerification(
            status=VerificationStatus.PHYSICAL_VERIFIED,
            fingerprint=channel.mapping_fingerprint,
        ),
    )
    config["hardware_profile"] = replace(profile, channels=tuple(channels)).to_dict()
    store = HardwareProfileStore(
        default_config=config,
        local_config_path=tmp_path / "local_config.json",
    )

    with pytest.raises(ValueError, match="降级"):
        store.update_verification(
            2,
            ChannelVerification(
                status=VerificationStatus.MOCK_VERIFIED,
                fingerprint=channel.mapping_fingerprint,
            ),
            expected_revision=0,
            expected_fingerprint=channel.mapping_fingerprint,
        )
