from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.models import HardwareProfile, VerificationStatus
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
