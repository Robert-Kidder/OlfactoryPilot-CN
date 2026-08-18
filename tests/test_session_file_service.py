from __future__ import annotations

import csv
import hashlib
import json
import threading
import unicodedata
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

import app.services.session_file_service as session_file_module
from app.models.session import (
    ProducerFence,
    SessionRecordEnvelope,
    SessionState,
    SessionStatus,
)
from app.services.session_file_service import (
    SessionFileError,
    SessionFileService,
    sanitize_windows_component,
    utf16_code_units,
)

FIXED_LOCAL_TIME = datetime(2026, 7, 27, 18, 0, 0, 123000).astimezone()
FAILED_HIL_BUNDLE = (
    Path(__file__).resolve().parents[1]
    / "logs"
    / "benchmarks"
    / "story-3-5-20260730-154234-live"
    / "session-output"
    / "20260730-154242-838_HIL-NO-SUBJECT_Story-3.5-Windows-NI"
)


class CountingClock:
    def __init__(self, value: datetime = FIXED_LOCAL_TIME) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


@pytest.mark.skipif(
    not FAILED_HIL_BUNDLE.is_dir(),
    reason="本机未保留 2026-07-30 Story 3.5 中止 HIL bundle",
)
def test_failed_hil_bundle_validates_read_only_with_configured_master_contract() -> None:
    evidence_files = tuple(
        sorted(path for path in FAILED_HIL_BUNDLE.iterdir() if path.is_file())
    )
    before = {
        path: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in evidence_files
    }

    validation = SessionFileService(
        master_valve_line="Dev2/P1.0"
    ).validate_complete_bundle(FAILED_HIL_BUNDLE)

    after = {
        path: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in evidence_files
    }
    assert validation.complete, validation.reason
    assert validation.last_sequence == 139
    assert after == before


def test_windows_component_cleaning_is_nfc_ordered_and_rejects_empty() -> None:
    decomposed = "  Cafe\u0301\t<test>...  "

    cleaned = sanitize_windows_component(decomposed)

    assert cleaned == unicodedata.normalize("NFC", "Café-test-")
    assert len(cleaned) <= 64
    with pytest.raises(SessionFileError, match="不能为空"):
        sanitize_windows_component(" \t<>.. ")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("PRN.anything", "_PRN.anything"),
        ("COM9", "_COM9"),
        ("LPT¹.csv", "_LPT¹.csv"),
        ("normal-CON", "normal-CON"),
    ],
)
def test_windows_reserved_device_names_are_prefixed(value: str, expected: str) -> None:
    assert sanitize_windows_component(value) == expected


def test_component_truncation_is_deterministic_and_collision_resistant() -> None:
    original_a = "受试者" * 40 + "甲"
    original_b = "受试者" * 40 + "乙"

    cleaned_a = sanitize_windows_component(original_a)
    cleaned_b = sanitize_windows_component(original_b)

    assert len(cleaned_a) <= 64
    assert len(cleaned_b) <= 64
    assert cleaned_a != cleaned_b
    assert cleaned_a.endswith("-" + hashlib.sha256(original_a.encode()).hexdigest()[:8])
    assert cleaned_b.endswith("-" + hashlib.sha256(original_b.encode()).hexdigest()[:8])


def test_preview_samples_wall_clock_once_and_preserves_original_values(tmp_path: Path) -> None:
    clock = CountingClock()
    service = SessionFileService(clock=clock)

    preview = service.preview(
        output_dir=tmp_path,
        subject="  CON  ",
        condition="条件 A",
    )

    assert clock.calls == 1
    assert preview.timestamp_text == FIXED_LOCAL_TIME.strftime("%Y%m%d-%H%M%S-%f")[:-3]
    assert preview.started_at_iso.endswith(FIXED_LOCAL_TIME.strftime("%z")[-4:-2] + ":00")
    assert preview.subject_original == "  CON  "
    assert preview.subject_clean == "_CON"
    assert preview.condition_clean == "条件-A"
    assert preview.stem == f"{preview.timestamp_text}__CON_条件-A"
    assert preview.final_dir == tmp_path.resolve() / preview.stem
    assert not preview.editable


def test_preview_enforces_240_utf16_unit_absolute_path_budget(tmp_path: Path) -> None:
    output = tmp_path / ("父目录" * 5)
    output.mkdir()
    service = SessionFileService(clock=CountingClock())

    preview = service.preview(
        output_dir=output,
        subject="受试者" * 40,
        condition="条件" * 40,
    )

    assert utf16_code_units(preview.final_raw_path) <= 240
    assert utf16_code_units(preview.final_log_path) <= 240
    assert utf16_code_units(preview.staging_manifest_path) <= 240
    assert "-" in preview.subject_clean
    assert "-" in preview.condition_clean


def test_path_budget_includes_collision_owner_marker(tmp_path: Path) -> None:
    timestamp = FIXED_LOCAL_TIME.strftime("%Y%m%d-%H%M%S-%f")[:-3]
    stem = f"{timestamp}_S_A__999"
    selected_root = None
    for length in range(1, 200):
        root = tmp_path / ("x" * length)
        staging = root / f".{stem}.session.part"
        existing_budget_paths = (
            root / stem / f"{stem}.raw",
            root / stem / f"{stem}.log",
            staging / f"{stem}.raw",
            staging / f"{stem}.log",
            staging / "manifest.json",
        )
        marker = staging / session_file_module.OWNER_MARKER_NAME
        if (
            max(utf16_code_units(path) for path in existing_budget_paths)
            <= 240
            and utf16_code_units(marker) > 240
        ):
            selected_root = root
            break
    assert selected_root is not None
    selected_root.mkdir()
    service = SessionFileService(clock=CountingClock())

    with pytest.raises(SessionFileError) as exc_info:
        service.preview(
            output_dir=selected_root,
            subject="S",
            condition="A",
        )

    assert exc_info.value.stage == "path_budget"


def test_reserve_uses_pair_suffix_and_never_overwrites_existing_bundle(tmp_path: Path) -> None:
    service = SessionFileService(clock=CountingClock())
    base = service.preview(output_dir=tmp_path, subject="S01", condition="A")
    base.final_dir.mkdir()
    (tmp_path / f".{base.stem}__001.session.part").mkdir()

    descriptor = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="A",
        generation=7,
    )

    assert descriptor.stem.endswith("__002")
    assert descriptor.paths.raw_path.name == descriptor.stem + ".raw"
    assert descriptor.paths.log_path.name == descriptor.stem + ".log"
    assert descriptor.paths.staging_dir.name == f".{descriptor.stem}.session.part"
    assert descriptor.paths.raw_path.exists()
    assert descriptor.paths.log_path.exists()
    assert descriptor.paths.manifest_path.exists()
    assert not descriptor.paths.final_dir.exists()
    manifest = json.loads(descriptor.paths.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "recording"
    assert manifest["session_id"] == descriptor.session_id


def test_reserve_reuses_the_exact_preview_wall_clock_sample(tmp_path: Path) -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> datetime:
            value = FIXED_LOCAL_TIME + timedelta(seconds=self.calls)
            self.calls += 1
            return value

    clock = AdvancingClock()
    service = SessionFileService(clock=clock)
    preview = service.preview(output_dir=tmp_path, subject="S01", condition="A")

    descriptor = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="A",
        generation=1,
        preview=preview,
    )

    assert clock.calls == 1
    assert descriptor.timestamp_text == preview.timestamp_text
    assert descriptor.started_at == preview.started_at
    assert descriptor.stem == preview.stem
    assert descriptor.paths.staging_dir == preview.staging_dir
    assert descriptor.paths.final_dir == preview.final_dir


def test_concurrent_reservations_get_unique_atomic_stems(tmp_path: Path) -> None:
    barrier = threading.Barrier(8)
    descriptors = []
    errors = []
    lock = threading.Lock()

    def reserve() -> None:
        try:
            service = SessionFileService(clock=CountingClock())
            barrier.wait()
            descriptor = service.reserve(
                output_dir=tmp_path,
                subject="S01",
                condition="A",
                generation=1,
            )
            with lock:
                descriptors.append(descriptor)
        except Exception as exc:  # pragma: no cover - assertion reports payload
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(descriptors) == 8
    assert len({item.stem.casefold() for item in descriptors}) == 8
    assert all(item.paths.staging_dir.is_dir() for item in descriptors)


def test_reservation_failure_never_creates_success_final_directory(tmp_path: Path) -> None:
    def fail(stage: str, _path: Path) -> None:
        if stage == "create_log":
            raise PermissionError("synthetic denied")

    service = SessionFileService(clock=CountingClock(), fault_injector=fail)

    with pytest.raises(SessionFileError, match="日志文件"):
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
        )

    assert not list(tmp_path.glob("*S01_A"))
    parts = list(tmp_path.glob(".*.session.part"))
    assert len(parts) == 1
    assert not any(path.name.endswith(".log") for path in parts[0].iterdir())


def test_unwritable_directory_fault_is_reported_before_session_starts(
    tmp_path: Path,
) -> None:
    def deny_staging(stage: str, _path: Path) -> None:
        if stage == "create_staging":
            raise PermissionError("synthetic access denied")

    service = SessionFileService(
        clock=CountingClock(),
        fault_injector=deny_staging,
    )

    with pytest.raises(SessionFileError, match="无法创建会话工作目录") as exc_info:
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
        )

    assert exc_info.value.stage == "create_staging"
    assert not any(tmp_path.iterdir())


def test_atomic_collision_rejects_after_candidate_999(tmp_path: Path) -> None:
    attempted_names: list[str] = []

    def collide_every_staging(stage: str, path: Path) -> None:
        if stage == "create_staging":
            attempted_names.append(path.name)
            raise FileExistsError(path)

    service = SessionFileService(
        clock=CountingClock(),
        fault_injector=collide_every_staging,
    )
    preview = service.preview(output_dir=tmp_path, subject="S01", condition="A")

    with pytest.raises(SessionFileError, match="__999") as exc_info:
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
        )

    assert exc_info.value.stage == "collision"
    assert len(attempted_names) == 1000
    assert attempted_names[0] == f".{preview.stem}.session.part"
    assert attempted_names[-1] == f".{preview.stem}__999.session.part"


def test_invalid_output_directory_and_unc_path_fail_in_chinese(tmp_path: Path) -> None:
    service = SessionFileService(clock=CountingClock())
    missing = tmp_path / "missing"
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(SessionFileError, match="不存在"):
        service.reserve(output_dir=missing, subject="S", condition="C", generation=1)
    with pytest.raises(SessionFileError, match="不是目录"):
        service.reserve(output_dir=file_path, subject="S", condition="C", generation=1)
    with pytest.raises(SessionFileError, match="网络路径"):
        service.preview(
            output_dir=Path(r"\\server\share"),
            subject="S",
            condition="C",
        )


def test_envelope_and_fence_validate_generation_identity() -> None:
    envelope = SessionRecordEnvelope(
        session_id="session-1",
        session_generation=3,
        producer="actuation",
        producer_sequence=4,
        event_id="actuation:3:4",
        record_type="protocol_event",
        payload={"event": "protocol_started"},
    )
    fence = ProducerFence(
        session_id="session-1",
        session_generation=3,
        producer="actuation",
        producer_sequence=5,
    )

    assert envelope.payload["event"] == "protocol_started"
    assert fence.event_id == "actuation:3:fence:5"
    with pytest.raises(FrozenInstanceError):
        envelope.producer_sequence = 9  # type: ignore[misc]
    with pytest.raises(ValueError, match="event_id"):
        SessionRecordEnvelope(
            session_id="session-1",
            session_generation=3,
            producer="actuation",
            producer_sequence=4,
            event_id="",
            record_type="protocol_event",
            payload={},
        )


def test_envelope_payload_is_deeply_frozen() -> None:
    nested = {
        "mapping": {"items": [1, {"name": "original"}]},
        "set": {"a", "b"},
    }
    envelope = SessionRecordEnvelope(
        session_id="session-1",
        session_generation=3,
        producer="controller",
        producer_sequence=1,
        event_id="controller:3:1",
        record_type="session_event",
        payload=nested,
    )

    nested["mapping"]["items"][1]["name"] = "mutated"
    nested["mapping"]["items"].append(2)
    nested["set"].add("c")

    assert isinstance(envelope.payload, MappingProxyType)
    assert isinstance(envelope.payload["mapping"], MappingProxyType)
    assert envelope.payload["mapping"]["items"] == (
        1,
        MappingProxyType({"name": "original"}),
    )
    assert envelope.payload["set"] == frozenset({"a", "b"})
    with pytest.raises(TypeError):
        envelope.payload["mapping"]["new"] = True  # type: ignore[index]


def test_session_state_transitions_are_idempotent_and_descriptor_is_immutable(
    tmp_path: Path,
) -> None:
    descriptor = SessionFileService(clock=CountingClock()).reserve(
        output_dir=tmp_path,
        subject="S",
        condition="C",
        generation=2,
    )
    state = SessionState()

    assert state.begin(descriptor)
    assert not state.begin(descriptor)
    assert state.status == SessionStatus.RECORDING
    assert state.begin_close("completed")
    assert not state.begin_close("completed")
    assert state.mark_closed(descriptor.paths.final_dir)
    assert not state.mark_closed(descriptor.paths.final_dir)
    assert state.status == SessionStatus.CLOSED
    with pytest.raises(FrozenInstanceError):
        descriptor.stem = "changed"  # type: ignore[misc]


def test_recovery_scanner_quarantines_incomplete_once_and_preserves_complete(
    tmp_path: Path,
) -> None:
    service = SessionFileService(clock=CountingClock())
    incomplete = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="incomplete",
        generation=1,
    )
    complete_dir, _raw, _log, _manifest = _write_strict_complete_bundle(
        tmp_path,
        "valid",
    )

    scanner = SessionFileService(clock=CountingClock())
    first = scanner.scan_recovery(tmp_path)
    second = scanner.scan_recovery(tmp_path)

    assert len(first) == 1
    assert first[0].original_path == incomplete.paths.staging_dir
    assert first[0].quarantined_path is not None
    assert first[0].quarantined_path.parent == tmp_path / "recovery"
    assert second == ()
    assert service.validate_complete_bundle(complete_dir).complete


def test_recovery_scanner_ignores_final_looking_bundle_without_identity_manifest(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "20260727-180000-123_S01_broken"
    broken.mkdir()
    (broken / f"{broken.name}.raw").write_text("raw\n", encoding="utf-8")
    (broken / f"{broken.name}.log").write_text("log\n", encoding="utf-8")

    findings = SessionFileService(clock=CountingClock()).scan_recovery(tmp_path)

    assert findings == ()
    assert broken.is_dir()


def test_recovery_scanner_ignores_unrelated_directory_with_manifest(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "third-party-project"
    unrelated.mkdir()
    manifest = unrelated / "manifest.json"
    manifest.write_text('{"name":"not-a-session"}\n', encoding="utf-8")

    findings = SessionFileService(clock=CountingClock()).scan_recovery(tmp_path)

    assert findings == ()
    assert unrelated.is_dir()
    assert manifest.is_file()
    assert not (tmp_path / "recovery").exists()


def test_manifest_validation_requires_schema_identity_and_contained_basenames(
    tmp_path: Path,
) -> None:
    bundle, raw, _log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260727-180000-123_S01_A",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    service = SessionFileService(clock=CountingClock())
    assert service.validate_complete_bundle(bundle).complete

    for field in ("schema", "schema_version", "session_id", "session_generation", "stem"):
        invalid = dict(manifest)
        invalid.pop(field)
        manifest_path.write_text(json.dumps(invalid), encoding="utf-8")
        validation = service.validate_complete_bundle(bundle)
        assert not validation.complete
        assert field in validation.reason

    escaped = dict(manifest)
    escaped["raw_file"] = "../outside.raw"
    manifest_path.write_text(json.dumps(escaped), encoding="utf-8")
    validation = service.validate_complete_bundle(bundle)
    assert not validation.complete
    assert "basename" in validation.reason


def test_manifest_validation_isolates_invalid_utf8_and_malformed_jsonl(
    tmp_path: Path,
) -> None:
    service = SessionFileService(clock=CountingClock())
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260727-180000-123_S01_A",
    )
    raw.write_bytes(b"\xff\xfe\n")
    log.write_bytes(b"{not-json}\n")
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = service.validate_complete_bundle(bundle)

    assert not validation.complete
    assert "UTF-8" in validation.reason or "JSON" in validation.reason
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1
    assert findings[0].original_path == bundle


def test_recovery_scanner_skips_in_process_active_reservation(
    tmp_path: Path,
) -> None:
    service = SessionFileService(clock=CountingClock())
    descriptor = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="active",
        generation=1,
    )
    service.mark_active(descriptor.paths.staging_dir)

    findings = service.scan_recovery(tmp_path)

    assert findings == ()
    assert descriptor.paths.staging_dir.is_dir()
    service.mark_inactive(descriptor.paths.staging_dir)
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1


def test_reserve_registration_is_atomic_against_recovery_scan(tmp_path: Path) -> None:
    create_raw_entered = threading.Event()
    release_create_raw = threading.Event()

    def fault(stage: str, _path: Path) -> None:
        if stage == "create_raw":
            create_raw_entered.set()
            assert release_create_raw.wait(2)

    service = SessionFileService(clock=CountingClock(), fault_injector=fault)
    reserved = []
    reserve_thread = threading.Thread(
        target=lambda: reserved.append(
            service.reserve(
                output_dir=tmp_path,
                subject="S01",
                condition="atomic",
                generation=1,
            )
        )
    )
    reserve_thread.start()
    assert create_raw_entered.wait(2)

    findings = service.scan_recovery(tmp_path)
    release_create_raw.set()
    reserve_thread.join(2)

    assert findings == ()
    assert len(reserved) == 1
    assert reserved[0].paths.staging_dir.is_dir()


def test_recovery_ignores_unidentified_part_directory_with_matching_pair(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / ".20260729-120000-000_S01_A.session.part"
    unrelated.mkdir()
    stem = "20260729-120000-000_S01_A"
    (unrelated / f"{stem}.raw").write_text("user data", encoding="utf-8")
    (unrelated / f"{stem}.log").write_text("user data", encoding="utf-8")

    findings = SessionFileService(clock=CountingClock()).scan_recovery(tmp_path)

    assert findings == ()
    assert unrelated.is_dir()


def test_output_validation_rejects_mapped_or_resolved_network_location(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        session_file_module,
        "_is_network_location",
        lambda _path: True,
        raising=False,
    )

    with pytest.raises(SessionFileError, match="网络"):
        SessionFileService(clock=CountingClock()).preview(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
        )


def test_complete_bundle_validation_streams_files_without_read_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle, raw, log, _manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260729-120000-000_S01_A",
    )
    original_read_bytes = Path.read_bytes

    def reject_payload_read_bytes(path: Path) -> bytes:
        if path in {raw, log}:
            raise AssertionError("raw/log 必须流式读取")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_payload_read_bytes)

    assert SessionFileService().validate_complete_bundle(bundle).complete


def test_recovery_validation_does_not_hold_active_registration_lock(
    tmp_path: Path,
) -> None:
    service = SessionFileService(clock=CountingClock())
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "olfactorypilot.session",
                "schema_version": 1,
                "status": "complete",
                "session_id": "stale-session",
                "session_generation": 1,
                "stem": stale.name,
                "raw_file": f"{stale.name}.raw",
                "log_file": f"{stale.name}.log",
            }
        ),
        encoding="utf-8",
    )
    validation_entered = threading.Event()
    release_validation = threading.Event()
    scan_done = threading.Event()
    reserve_done = threading.Event()
    reserved = []

    def blocked_validation(path, *, cancel_event=None):
        validation_entered.set()
        assert release_validation.wait(2)
        return session_file_module.BundleValidation(Path(path), True)

    service.validate_complete_bundle = blocked_validation  # type: ignore[method-assign]
    scan_thread = threading.Thread(
        target=lambda: (
            service.scan_recovery(tmp_path),
            scan_done.set(),
        )
    )
    reserve_thread = threading.Thread(
        target=lambda: (
            reserved.append(
                service.reserve(
                    output_dir=tmp_path,
                    subject="S02",
                    condition="concurrent",
                    generation=2,
                )
            ),
            reserve_done.set(),
        )
    )
    scan_thread.start()
    assert validation_entered.wait(1)
    reserve_thread.start()
    try:
        assert reserve_done.wait(0.5)
    finally:
        release_validation.set()
        scan_thread.join(2)
        reserve_thread.join(2)

    assert scan_done.is_set()
    assert len(reserved) == 1


def test_complete_bundle_validation_cancels_inside_raw_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle, raw, _log, _manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260729-130000-000_S01_cancel",
    )
    cancel_event = threading.Event()
    original_open = Path.open

    class CancellingReader:
        def __init__(self, handle):
            self._handle = handle
            self._seen = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def read(self, size=-1):
            line = self._handle.read(size)
            if not self._seen:
                self._seen = True
                cancel_event.set()
            return line

    def cancelling_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == raw and "b" in str(args[0] if args else kwargs.get("mode", "r")):
            return CancellingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", cancelling_open)

    validation = SessionFileService().validate_complete_bundle(
        bundle,
        cancel_event=cancel_event,
    )

    assert not validation.complete
    assert "取消" in validation.reason


@pytest.mark.parametrize(
    "failure_mode",
    ["create_log", "create_manifest", "partial_manifest"],
)
def test_recovery_quarantines_owned_staging_without_valid_manifest(
    tmp_path: Path,
    monkeypatch,
    failure_mode: str,
) -> None:
    def fault(stage: str, _path: Path) -> None:
        if stage == failure_mode:
            raise OSError(f"synthetic {failure_mode}")

    service = SessionFileService(
        clock=CountingClock(),
        fault_injector=fault,
    )
    if failure_mode == "partial_manifest":
        def partial_dump(_payload, handle, **_kwargs) -> None:
            handle.write('{"schema":"olfactorypilot.session"')
            raise OSError("synthetic partial manifest")

        monkeypatch.setattr(session_file_module.json, "dump", partial_dump)

    with pytest.raises(SessionFileError):
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition=failure_mode,
            generation=1,
        )
    staging = next(tmp_path.glob(".*.session.part"))

    findings = service.scan_recovery(tmp_path)

    assert len(findings) == 1
    assert findings[0].original_path == staging
    assert findings[0].quarantined_path is not None
    assert not staging.exists()


def test_complete_bundle_rejects_blank_jsonl_line(tmp_path: Path) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260729-140000-000_S01_blank",
    )
    records = log.read_text(encoding="utf-8").splitlines()
    log.write_text(
        records[0] + "\n\n" + records[1] + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete
    assert "空白" in validation.reason


@pytest.mark.parametrize(
    "case",
    ["missing_csv_header", "duplicate_sample_identity", "monotonic_backwards"],
)
def test_complete_raw_validator_requires_header_and_monotonic_identity(
    tmp_path: Path,
    case: str,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        f"raw-order-{case}",
    )
    lines = raw.read_text(encoding="utf-8").splitlines()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case == "missing_csv_header":
        raw.write_text(lines[0] + "\n", encoding="utf-8", newline="\n")
        manifest["raw_record_count"] = 0
    else:
        second = (
            "2,1785146400.133,123456789001,7,4102,-0.4"
            if case == "duplicate_sample_identity"
            else "2,1785146400.133,123456788999,7,4103,-0.4"
        )
        raw.write_text(
            "\n".join([*lines, second]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest["raw_record_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete
    assert "header" in validation.reason or "identity" in validation.reason


@pytest.mark.parametrize(
    "case",
    [
        "unknown_record_type",
        "receipt_missing_field",
        "quality_missing_field",
        "duplicate_event_id",
        "producer_sequence_duplicate",
        "summary_count_mismatch",
        "owner_identity_mismatch",
        "nonfinite_json",
    ],
)
def test_complete_log_validator_enforces_record_envelopes_and_summaries(
    tmp_path: Path,
    case: str,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        f"log-contract-{case}",
    )
    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
    owner_path = bundle / session_file_module.OWNER_MARKER_NAME
    if case == "unknown_record_type":
        records[-1]["record_type"] = "mystery"
    elif case == "receipt_missing_field":
        records[-1]["record_type"] = "receipt"
        records[-1]["event"] = "actuation_receipt"
    elif case == "quality_missing_field":
        records[-1]["record_type"] = "quality_event"
        records[-1]["event"] = "quality_transition"
    elif case == "duplicate_event_id":
        records[-1]["event_id"] = records[0]["event_id"]
    elif case == "producer_sequence_duplicate":
        records[-1]["producer_sequence"] = records[0]["producer_sequence"]
    elif case == "summary_count_mismatch":
        records[-1]["sample_count"] = 999
    elif case == "nonfinite_json":
        records[0]["nonfinite"] = float("nan")
    else:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner["session_id"] = "different-session"
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
    log.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete


def test_windows_component_replaces_del_and_c1_control_characters() -> None:
    assert sanitize_windows_component("受试者\u007f\u0085条件") == "受试者-条件"


def test_collision_cleanup_failure_is_owned_and_reported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = SessionFileService(clock=CountingClock())
    preview = service.preview(output_dir=tmp_path, subject="S01", condition="A")
    preview.final_dir.mkdir()
    original_rmdir = Path.rmdir

    def fail_collision_cleanup(path: Path) -> None:
        if path == preview.staging_dir:
            raise OSError("synthetic collision cleanup failure")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_collision_cleanup)

    with pytest.raises(SessionFileError) as exc_info:
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
            preview=preview,
        )

    assert exc_info.value.stage == "collision_cleanup"
    assert (preview.staging_dir / session_file_module.OWNER_MARKER_NAME).is_file()


def test_collision_cleanup_marker_failure_writes_recovery_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def inject(stage: str, _path: Path) -> None:
        if stage == "create_owner_marker":
            raise OSError("synthetic owner marker failure")

    service = SessionFileService(
        clock=CountingClock(),
        fault_injector=inject,
    )
    preview = service.preview(output_dir=tmp_path, subject="S01", condition="A")
    preview.final_dir.mkdir()
    original_rmdir = Path.rmdir

    def fail_collision_cleanup(path: Path) -> None:
        if path == preview.staging_dir:
            raise OSError("synthetic collision cleanup failure")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_collision_cleanup)

    with pytest.raises(SessionFileError) as exc_info:
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
            preview=preview,
        )

    assert exc_info.value.stage == "collision_cleanup"
    manifest_path = preview.staging_dir / "manifest.json"
    assert manifest_path.is_file()
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1
    assert findings[0].original_path == preview.staging_dir
    assert findings[0].quarantined_path is not None


@pytest.mark.parametrize("collision", [False, True])
def test_recovery_tracks_orphan_when_all_identity_writes_and_cleanup_fail(
    tmp_path: Path,
    monkeypatch,
    collision: bool,
) -> None:
    def inject(stage: str, _path: Path) -> None:
        if stage in {"create_owner_marker", "create_manifest"}:
            raise OSError(f"synthetic {stage} failure")

    service = SessionFileService(
        clock=CountingClock(),
        fault_injector=inject,
    )
    preview = service.preview(output_dir=tmp_path, subject="S01", condition="A")
    if collision:
        preview.final_dir.mkdir()
    original_rmdir = Path.rmdir

    def fail_staging_cleanup(path: Path) -> None:
        if path == preview.staging_dir:
            raise OSError("synthetic staging cleanup failure")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_staging_cleanup)

    with pytest.raises(SessionFileError) as exc_info:
        service.reserve(
            output_dir=tmp_path,
            subject="S01",
            condition="A",
            generation=1,
            preview=preview,
        )

    assert exc_info.value.path == preview.staging_dir
    assert preview.staging_dir.is_dir()
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1
    assert findings[0].original_path == preview.staging_dir
    assert findings[0].quarantined_path is not None


def test_recovery_json_identity_reads_are_streamed_without_read_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = SessionFileService(clock=CountingClock())
    descriptor = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="stream-json",
        generation=1,
    )
    service.mark_inactive(descriptor.paths.staging_dir)
    calls: list[Path] = []
    original_read_bytes = Path.read_bytes

    def track_read_bytes(path: Path) -> bytes:
        calls.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read_bytes)

    findings = service.scan_recovery(tmp_path)

    assert len(findings) == 1
    assert calls == []


def test_recovery_raw_reader_checks_cancel_between_bounded_chunks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "20260730-090000-000_S01_cancel",
    )
    raw.write_bytes(b"x" * (2 * 1024 * 1024))
    _refresh_bundle_manifest(raw, log, manifest_path)
    cancel_event = threading.Event()
    original_open = Path.open
    read_sizes: list[int] = []

    class ChunkControlledReader:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._handle.close()

        def __iter__(self):
            raise AssertionError("恢复验证不得以不可取消的整行迭代读取")

        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024 * 1024
            read_sizes.append(size)
            data = self._handle.read(size)
            cancel_event.set()
            return data

    def controlled_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if path == raw and "b" in mode:
            return ChunkControlledReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", controlled_open)

    validation = SessionFileService().validate_complete_bundle(
        bundle,
        cancel_event=cancel_event,
    )

    assert read_sizes
    assert not validation.complete
    assert "取消" in validation.reason


def test_recovery_rejects_resolved_network_child_before_identity_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child = tmp_path / ".network.session.part"
    child.mkdir()
    identity_reads: list[Path] = []
    original_read = session_file_module._read_json_limited
    child_resolved = child.resolve(strict=False)

    def fake_network(path: Path) -> bool:
        return path.resolve(strict=False) == child_resolved

    def track_read(path: Path, **kwargs):
        if child in path.parents:
            identity_reads.append(path)
        return original_read(path, **kwargs)

    monkeypatch.setattr(
        session_file_module,
        "_is_network_location",
        fake_network,
    )
    monkeypatch.setattr(session_file_module, "_read_json_limited", track_read)

    findings = SessionFileService().scan_recovery(tmp_path)

    assert len(findings) == 1
    assert findings[0].original_path == child
    assert findings[0].quarantined_path is None
    assert "网络" in findings[0].reason
    assert identity_reads == []
    assert child.is_dir()


def test_recovery_quarantines_owned_final_bundle_with_missing_manifest(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "20260730-091000-000_S01_owned"
    bundle.mkdir()
    (bundle / session_file_module.OWNER_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema": "olfactorypilot.session-owner",
                "schema_version": 1,
                "session_id": "owned-final",
                "session_generation": 3,
                "stem": bundle.name,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    findings = SessionFileService(clock=CountingClock()).scan_recovery(tmp_path)

    assert len(findings) == 1
    assert findings[0].original_path == bundle
    assert findings[0].quarantined_path is not None


def _write_strict_complete_bundle(root: Path, name: str) -> tuple[Path, Path, Path, Path]:
    bundle = root / name
    bundle.mkdir()
    session_id = f"session-{name}"
    raw = bundle / f"{name}.raw"
    log = bundle / f"{name}.log"
    manifest_path = bundle / "manifest.json"
    owner_path = bundle / session_file_module.OWNER_MARKER_NAME
    owner_path.write_text(
        json.dumps(
            {
                "schema": "olfactorypilot.session-owner",
                "schema_version": 1,
                "session_id": session_id,
                "session_generation": 1,
                "stem": name,
            }
        ),
        encoding="utf-8",
    )
    raw.write_text(
        "# "
        + json.dumps(
            {
                "schema": "olfactorypilot.raw",
                "schema_version": 1,
                "session_id": session_id,
                "columns": [
                    "record_sequence",
                    "timestamp",
                    "monotonic_ns",
                    "ai_epoch",
                    "sample_sequence",
                    "ai0_raw",
                ],
                "nominal_rate_hz": 100,
            },
            separators=(",", ":"),
        )
        + "\n"
        + "record_sequence,timestamp,monotonic_ns,ai_epoch,sample_sequence,ai0_raw\n"
        + "1,1785146400.123,123456789000,7,4102,-0.4412\n",
        encoding="utf-8",
        newline="\n",
    )
    records = [
        {
            "schema": "olfactorypilot.event",
            "schema_version": 1,
            "session_id": session_id,
            "session_generation": 1,
            "session_sequence": 1,
            "record_type": "session_event",
            "event": "session_started",
            "timestamp": "2026-07-30T09:00:00.000+08:00",
            "monotonic_ns": None,
            "source": "session",
            "result": "success",
            "message": "会话已开始。",
            "producer": "session",
            "producer_sequence": 1,
            "event_id": "session:1:1",
        },
        {
            "schema": "olfactorypilot.event",
            "schema_version": 1,
            "session_id": session_id,
            "session_generation": 1,
            "session_sequence": 2,
            "record_type": "session_event",
            "event": "session_closed",
            "timestamp": "2026-07-30T09:01:00.000+08:00",
            "monotonic_ns": 123456789999,
            "source": "session",
            "result": "success",
            "message": "会话已关闭。",
            "producer": "session",
            "producer_sequence": 2,
            "event_id": "session:1:2",
            "started_at": "2026-07-30T09:00:00.000+08:00",
            "ended_at": "2026-07-30T09:01:00.000+08:00",
            "sample_count": 1,
            "event_count": 2,
            "receipt_count": 0,
            "queue_high_water": 0,
            "dropped_count": 0,
        },
    ]
    log.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schema": "olfactorypilot.session",
        "schema_version": 1,
        "status": "complete",
        "session_id": session_id,
        "session_generation": 1,
        "stem": name,
        "raw_file": raw.name,
        "log_file": log.name,
        "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "raw_bytes": raw.stat().st_size,
        "log_bytes": log.stat().st_size,
        "raw_record_count": 1,
        "log_event_count": 2,
        "receipt_count": 0,
        "queue_high_water": 0,
        "dropped_count": 0,
        "last_session_sequence": 2,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, raw, log, manifest_path


def _refresh_bundle_manifest(
    raw: Path,
    log: Path,
    manifest_path: Path,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "raw_bytes": raw.stat().st_size,
            "log_bytes": log.stat().st_size,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _insert_contract_record(
    log: Path,
    manifest_path: Path,
    record: dict,
) -> None:
    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
    session_id = records[0]["session_id"]
    values = {
        "schema": "olfactorypilot.event",
        "schema_version": 1,
        "session_id": session_id,
        "session_generation": 1,
        "session_sequence": 2,
        "timestamp": "2026-07-30T09:00:30.000+08:00",
        "monotonic_ns": 123456789500,
        "source": "actuation",
        "result": "success",
        "message": "测试记录",
        "producer": "actuation",
        "producer_sequence": 1,
        "event_id": "actuation:1:1",
        **record,
    }
    records[-1]["session_sequence"] = 3
    records[-1]["event_count"] = 3
    records[-1]["receipt_count"] = int(
        values["record_type"] == "receipt"
    )
    log.write_text(
        "".join(
            json.dumps(item, separators=(",", ":")) + "\n"
            for item in [records[0], values, records[-1]]
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["log_event_count"] = 3
    manifest["last_session_sequence"] = 3
    manifest["receipt_count"] = records[-1]["receipt_count"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("record_type", "field", "invalid"),
    [
        ("receipt", "execution_epoch", True),
        ("receipt", "arm_epoch", -1),
        ("receipt", "sequence", 1.5),
        ("receipt", "valve", 0),
        ("receipt", "stale", 0),
        ("receipt", "actual_ns", 90),
        ("receipt", "offset_ms", "late"),
        ("receipt", "measurement_point", "mechanical_complete"),
        ("receipt", "actual_ns_semantics", "physical_valve_complete"),
        ("quality_event", "p95_open_ms", -1.0),
        (
            "quality_event",
            "transitions",
            [{"stream": "mystery", "direction": "entered", "p95_ms": "slow"}],
        ),
    ],
)
def test_complete_validator_rejects_invalid_receipt_and_quality_semantics(
    tmp_path: Path,
    record_type: str,
    field: str,
    invalid,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        f"typed-{record_type}-{field}",
    )
    if record_type == "receipt":
        record = {
            "record_type": "receipt",
            "event": "actuation_receipt",
            "canonical_identity": [
                f"session-typed-{record_type}-{field}",
                4,
                "command-1",
            ],
            "command_id": "command-1",
            "execution_epoch": 4,
            "arm_epoch": 3,
            "sequence": 11,
            "trial_id": "trial-1",
            "trial_index": 0,
            "valve": 9,
            "action": "open",
            "category": "normal",
            "expected_ns": 100,
            "started_ns": 105,
            "actual_ns": 110,
            "offset_ms": 0.00001,
            "jitter_ms": 0.00001,
            "measurement_point": "daqmx_write_ack",
            "actual_ns_semantics": "daqmx_write_ack",
            "stale": False,
            "actual_duration_ms": None,
            "target_device": "Dev1",
            "target_line": "Dev1/port0/line0",
        }
    else:
        record = {
            "record_type": "quality_event",
            "event": "quality_transition",
            "command_id": "command-1",
            "transitions": [
                {"stream": "open", "direction": "entered", "p95_ms": 21.0}
            ],
            "last_jitter_ms": 2.0,
            "p95_open_ms": 21.0,
            "p95_close_ms": None,
            "p95_combined_ms": 20.0,
            "sample_count_open": 20,
            "sample_count_close": 19,
            "sample_count_combined": 39,
            "warning_open": True,
            "warning_close": False,
            "warning_combined": False,
            "severe_latched": False,
        }
    record[field] = invalid
    _insert_contract_record(log, manifest_path, record)
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete


def test_complete_validator_accepts_reverse_polarity_selector_safe_receipt(
    tmp_path: Path,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "selector-safe-open",
    )
    _insert_contract_record(
        log,
        manifest_path,
        {
            "record_type": "receipt",
            "event": "actuation_receipt",
            "canonical_identity": ["session-selector-safe-open", 4, "selector-1"],
            "command_id": "selector-1",
            "execution_epoch": 4,
            "arm_epoch": 3,
            "sequence": 11,
            "trial_id": None,
            "trial_index": None,
            "valve": 0,
            "action": "open",
            "category": "safety",
            "expected_ns": 100,
            "started_ns": 105,
            "actual_ns": 110,
            "offset_ms": 0.00001,
            "jitter_ms": 0.00001,
            "measurement_point": "daqmx_write_ack",
            "actual_ns_semantics": "daqmx_write_ack",
            "stale": False,
            "actual_duration_ms": None,
            "target_device": "Dev2",
            "target_line": "P1.0",
            "operation_id": "safe-stop-1",
            "generation": 1,
            "step_id": "selector_safe",
            "action_kind": "open",
        },
    )
    _refresh_bundle_manifest(raw, log, manifest_path)

    validation = SessionFileService(
        master_valve_line="Dev2/P1.0"
    ).validate_complete_bundle(bundle)

    assert validation.complete, validation.reason


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("raw_sha256", lambda value: "0" * len(value)),
        ("raw_bytes", lambda value: value + 1),
        ("raw_record_count", lambda value: value + 1),
        ("log_event_count", lambda value: value + 1),
        ("last_session_sequence", lambda value: value + 10),
    ],
)
def test_complete_validation_failure_preserves_manifest_last_sequence(
    tmp_path: Path,
    field: str,
    mutate,
) -> None:
    bundle, _raw, _log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        f"last-sequence-{field}",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = mutate(manifest[field])
    expected_last = manifest["last_session_sequence"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete
    assert validation.last_sequence == expected_last


def test_recovery_reports_csv_field_limit_error_without_aborting_scan(
    tmp_path: Path,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        "csv-field-limit",
    )
    lines = raw.read_text(encoding="utf-8").splitlines()
    oversized_field = "1" * (csv.field_size_limit() + 1)
    lines[2] = (
        "1,1785146400.123,123456789000,7,4102,"
        + oversized_field
    )
    raw.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _refresh_bundle_manifest(raw, log, manifest_path)
    service = SessionFileService(clock=CountingClock())

    validation = service.validate_complete_bundle(bundle)

    assert not validation.complete
    assert "CSV" in validation.reason
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1
    assert findings[0].original_path == bundle
    assert findings[0].quarantined_path is not None


@pytest.mark.parametrize(
    "case",
    [
        "raw_header",
        "raw_row",
        "log_required_field",
        "first_lifecycle",
        "last_lifecycle",
        "dropped_count",
        "bool_schema",
        "float_count",
        "float_sequence",
    ],
)
def test_complete_bundle_validator_enforces_strict_v1_contract(
    tmp_path: Path,
    case: str,
) -> None:
    bundle, raw, log, manifest_path = _write_strict_complete_bundle(
        tmp_path,
        f"strict-{case}",
    )
    if case == "raw_header":
        lines = raw.read_text(encoding="utf-8").splitlines()
        lines[1] = "record_sequence,timestamp"
        raw.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        _refresh_bundle_manifest(raw, log, manifest_path)
    elif case == "raw_row":
        lines = raw.read_text(encoding="utf-8").splitlines()
        lines[2] = "1,1785146400.123"
        raw.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        _refresh_bundle_manifest(raw, log, manifest_path)
    elif case in {
        "log_required_field",
        "first_lifecycle",
        "last_lifecycle",
        "dropped_count",
        "float_sequence",
    }:
        records = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
        ]
        if case == "log_required_field":
            records[0].pop("source")
        elif case == "first_lifecycle":
            records[0]["event"] = "protocol_bound"
        elif case == "last_lifecycle":
            records[-1]["event"] = "shutdown"
        elif case == "dropped_count":
            records[-1]["dropped_count"] = 1
        else:
            records[0]["session_sequence"] = 1.0
        log.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
            newline="\n",
        )
        _refresh_bundle_manifest(raw, log, manifest_path)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if case == "bool_schema":
            manifest["schema_version"] = True
        else:
            manifest["raw_record_count"] = 1.0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validation = SessionFileService().validate_complete_bundle(bundle)

    assert not validation.complete
