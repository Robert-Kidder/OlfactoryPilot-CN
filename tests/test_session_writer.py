from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import app.workers.session_writer as session_writer_module
from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationQualitySnapshot,
    ActuationReceipt,
    ActuationResult,
    ActuationStreamSnapshot,
    ProtocolGateEvent,
)
from app.models.session import SessionStatus
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.session_file_service import (
    MAX_STREAM_LINE_BYTES,
    SessionFileService,
)
from app.workers.session_writer import (
    RecorderReadinessLatch,
    SessionRecorderIngress,
    SessionWriterConfig,
    SessionWriterWorker,
)


def _descriptor(tmp_path: Path, *, generation: int = 1):
    return SessionFileService().reserve(
        output_dir=tmp_path,
        subject="受试者-01",
        condition="条件-A",
        generation=generation,
        protocol_source="demo.csv",
        protocol_metadata={"研究": "嗅觉"},
    )


def _started_payload() -> dict:
    return {
        "declared_trigger_mode": "manual",
        "current_trigger_mode": "manual",
        "inhale_threshold": 0.2,
        "exhale_threshold": -0.2,
        "low_flow_threshold": 0.3,
        "hardware_variant": "20-channel",
        "hardware_mode": "simulation",
        "ai_epoch_available": True,
        "actuation_quality_config": {
            "target_ms": 20.0,
            "single_limit_ms": 30.0,
            "window_size": 100,
            "min_samples": 20,
        },
    }


def _batch(sequence: int = 10) -> BreathSampleBatch:
    monotonic_ns = 123_456_789_000 + sequence * 10_000_000
    return BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(
                timestamp=1_785_146_400.123,
                ai0=-0.4412,
                monotonic_ns=monotonic_ns,
                ai_epoch=7,
                sample_sequence=sequence,
            ),
            AnalogInputFrame(
                timestamp=1_785_146_400.133,
                ai0=-0.4,
                monotonic_ns=monotonic_ns + 10_000_000,
                ai_epoch=7,
                sample_sequence=sequence + 1,
            ),
        )
    )


def _receipt(
    command_id: str = "command-1",
    *,
    valve: int = 9,
    action: ActuationAction = ActuationAction.OPEN,
    category: ActuationCategory = ActuationCategory.NORMAL,
    target_device: str = "Dev1",
    target_line: str = "Dev1/port0/line0",
) -> ActuationReceipt:
    return ActuationReceipt(
        command_id=command_id,
        execution_epoch=4,
        arm_epoch=3,
        sequence=11,
        trial_id="trial-1",
        trial_index=0,
        valve=valve,
        action=action,
        category=category,
        expected_ns=100,
        started_ns=105,
        actual_ns=110,
        wall_timestamp=1_785_146_400.2,
        offset_ms=0.00001,
        jitter_ms=0.00001,
        result=ActuationResult.SUCCESS,
        measurement_point="daqmx_write_ack",
        message="写入成功",
        stale=False,
        actual_duration_ms=None,
        target_device=target_device,
        target_line=target_line,
    )


def _writer(
    tmp_path: Path,
    *,
    capacity: int = 32,
    expected_producers: tuple[str, ...] = ("hardware", "actuation", "controller"),
    fault_injector=None,
    master_valve_line: str = "",
):
    descriptor = _descriptor(tmp_path)
    latch = RecorderReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(
            queue_capacity=capacity,
            flush_every_records=1000,
            close_timeout_ms=2000,
        ),
        expected_producers=expected_producers,
        session_started_payload=_started_payload(),
        readiness_latch=latch,
        master_valve_line=master_valve_line,
        fault_injector=fault_injector,
    )
    ingress = SessionRecorderIngress(writer, latch)
    return descriptor, writer, ingress, latch


def _read_jsonl(path: Path) -> list[dict]:
    data = path.read_bytes()
    assert data.endswith(b"\n")
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def test_raw_and_log_schema_round_trip_and_structured_adapters(tmp_path: Path) -> None:
    descriptor, writer, ingress, latch = _writer(tmp_path)
    assert writer.start_and_wait()
    assert latch.read().recording_ready

    assert ingress.post_raw_batch(_batch(), producer_sequence=1)
    protocol_event = ProtocolGateEvent(
        event="ttl_accepted",
        timestamp=1_785_146_400.15,
        trial_id="trial-1",
        trial_index=0,
        valve=9,
        trigger_source="ttl",
        arm_epoch=3,
        pulse_sequence=8,
        command_id="command-1",
        expected_ns=100,
        started_ns=105,
        actual_ns=110,
        jitter_ms=0.00001,
        result="accepted",
        message="TTL 已接受",
    )
    assert ingress.post_protocol_event(protocol_event, producer_sequence=1)
    assert ingress.post_receipt(_receipt(), producer_sequence=2)
    quality = ActuationQualitySnapshot(
        open=ActuationStreamSnapshot(sample_count=20, p95_ms=10.0),
        close=ActuationStreamSnapshot(sample_count=19, p95_ms=None),
        combined=ActuationStreamSnapshot(
            sample_count=39,
            p95_ms=11.0,
            warning=True,
            target_met=True,
        ),
        last_jitter_ms=3.2,
        severe_latched=False,
    )
    assert ingress.post_quality_event(
        event="quality_warning",
        snapshot=quality,
        producer_sequence=3,
        command_id="command-1",
        message="质量窗口进入警告",
    )
    assert ingress.post_session_event(
        event="shutdown",
        producer_sequence=1,
        source="controller",
        result="success",
        message="安全关闭完成",
        payload={"valves_closed": True},
    )
    assert ingress.post_fence("hardware", producer_sequence=1)
    assert ingress.post_fence("actuation", producer_sequence=3)
    assert ingress.post_fence("controller", producer_sequence=1)

    result = writer.close(reason="completed", final_quality=quality)

    assert result.complete
    assert result.status == SessionStatus.CLOSED
    raw_path = descriptor.paths.final_raw_path
    log_path = descriptor.paths.final_log_path
    raw_lines = raw_path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(raw_lines[0][2:])
    assert metadata["schema"] == "olfactorypilot.raw"
    assert metadata["schema_version"] == 1
    assert metadata["session_id"] == descriptor.session_id
    assert metadata["nominal_rate_hz"] == 100
    assert raw_lines[1] == (
        "record_sequence,timestamp,monotonic_ns,ai_epoch,"
        "sample_sequence,ai0_raw"
    )
    assert raw_lines[2].endswith(",7,10,-0.4412")
    assert len(raw_lines) == 4

    records = _read_jsonl(log_path)
    assert records[0]["event"] == "session_started"
    assert records[0]["subject_original"] == "受试者-01"
    assert records[0]["protocol_metadata"] == {"研究": "嗅觉"}
    assert [record["session_sequence"] for record in records] == list(
        range(1, len(records) + 1)
    )
    assert all(record["schema"] == "olfactorypilot.event" for record in records)
    assert all(record["schema_version"] == 1 for record in records)
    assert all(record["timestamp"][-6:-5] in {"+", "-"} for record in records)

    protocol = next(record for record in records if record["record_type"] == "protocol_event")
    assert protocol["event"] == "ttl_accepted"
    assert protocol["command_id"] == "command-1"
    for duplicate_timing in (
        "expected_ns",
        "started_ns",
        "actual_ns",
        "offset_ms",
        "jitter_ms",
        "measurement_point",
    ):
        assert duplicate_timing not in protocol

    receipt = next(record for record in records if record["record_type"] == "receipt")
    assert {
        "command_id",
        "execution_epoch",
        "arm_epoch",
        "sequence",
        "trial_id",
        "trial_index",
        "valve",
        "action",
        "category",
        "expected_ns",
        "started_ns",
        "actual_ns",
        "offset_ms",
        "jitter_ms",
        "result",
        "measurement_point",
        "stale",
        "actual_duration_ms",
        "target_device",
        "target_line",
        "message",
    } <= receipt.keys()
    assert receipt["canonical_identity"] == [
        descriptor.session_id,
        4,
        "command-1",
    ]
    assert receipt["actual_ns"] == 110
    assert receipt["measurement_point"] == "daqmx_write_ack"
    assert receipt["actual_ns_semantics"] == "daqmx_write_ack"
    assert receipt["target_device"] == "Dev1"
    assert receipt["target_line"] == "Dev1/port0/line0"

    quality_record = next(
        record for record in records if record["record_type"] == "quality_event"
    )
    assert quality_record["p95_open_ms"] == 10.0
    assert quality_record["sample_count_combined"] == 39
    assert quality_record["command_id"] == "command-1"
    assert records[-1]["event"] == "session_closed"
    assert records[-1]["dropped_count"] == 0

    manifest = json.loads(descriptor.paths.final_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["raw_record_count"] == 2
    assert manifest["log_event_count"] == len(records)
    assert manifest["raw_bytes"] == raw_path.stat().st_size
    assert manifest["log_bytes"] == log_path.stat().st_size
    assert manifest["raw_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert manifest["log_sha256"] == hashlib.sha256(log_path.read_bytes()).hexdigest()


def test_configured_master_valve_zero_writer_round_trip_validates_complete_bundle(
    tmp_path: Path,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation", "controller"),
        master_valve_line="Dev2/P1.0",
    )
    assert writer.start_and_wait()
    master_receipt = _receipt(
        "safety-close-master",
        valve=0,
        action=ActuationAction.CLOSE,
        category=ActuationCategory.SAFETY,
        target_device="Dev2",
        target_line="P1.0",
    )
    assert ingress.post_receipt(master_receipt, producer_sequence=1)
    assert ingress.post_fence("actuation", producer_sequence=1)
    assert ingress.post_fence("controller", producer_sequence=0)

    result = writer.close(reason="safety_abort")

    assert result.complete
    validation = SessionFileService(
        master_valve_line="Dev2/P1.0"
    ).validate_complete_bundle(descriptor.paths.final_dir)
    assert validation.complete, validation.reason


@pytest.mark.parametrize(
    "category",
    [ActuationCategory.MANUAL, ActuationCategory.PRETEST],
)
def test_configured_master_prepare_writer_round_trip_accepts_manual_and_pretest(
    tmp_path: Path,
    category: ActuationCategory,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation", "controller"),
        master_valve_line="Dev2/P1.0",
    )
    assert writer.start_and_wait()
    assert ingress.post_receipt(
        _receipt(
            f"{category.value}-master-prepare",
            valve=0,
            action=ActuationAction.OPEN,
            category=category,
            target_device="Dev2",
            target_line="P1.0",
        ),
        producer_sequence=1,
    )
    assert ingress.post_fence("actuation", producer_sequence=1)
    assert ingress.post_fence("controller", producer_sequence=0)

    result = writer.close(reason="master_prepare_complete")

    assert result.complete
    validation = SessionFileService(
        master_valve_line="Dev2/P1.0"
    ).validate_complete_bundle(descriptor.paths.final_dir)
    assert validation.complete, validation.reason


@pytest.mark.parametrize(
    ("action", "category"),
    [
        (ActuationAction.CLOSE, ActuationCategory.WARMUP),
        (ActuationAction.OPEN, ActuationCategory.SAFETY),
        (ActuationAction.CLOSE, ActuationCategory.MANUAL),
        (ActuationAction.CLOSE, ActuationCategory.PRETEST),
        (ActuationAction.OPEN, ActuationCategory.NORMAL),
    ],
)
def test_configured_master_zero_rejects_illegal_action_category_pairs(
    tmp_path: Path,
    action: ActuationAction,
    category: ActuationCategory,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation",),
        master_valve_line="Dev2/P1.0",
    )
    assert writer.start_and_wait()
    assert ingress.post_receipt(
        _receipt(
            "illegal-master-action",
            valve=0,
            action=action,
            category=category,
            target_device="Dev2",
            target_line="P1.0",
        ),
        producer_sequence=1,
    )
    assert writer.wait(2000)

    assert writer.failure is not None
    assert "主阀" in writer.failure.message or "valve=0" in writer.failure.message
    assert not writer.close(reason="failed").complete
    assert not descriptor.paths.final_dir.exists()


@pytest.mark.parametrize(
    ("master_valve_line", "target_device", "target_line"),
    [
        ("", "Dev2", "P1.0"),
        ("Dev2/P1.0", "Dev1", "P1.0"),
        ("Dev2/P1.0", "Dev2", "P0.0"),
    ],
)
def test_valve_zero_is_rejected_unless_it_matches_configured_master_target(
    tmp_path: Path,
    master_valve_line: str,
    target_device: str,
    target_line: str,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation", "controller"),
        master_valve_line=master_valve_line,
    )
    assert writer.start_and_wait()
    assert ingress.post_receipt(
        _receipt(
            "invalid-master",
            valve=0,
            action=ActuationAction.CLOSE,
            category=ActuationCategory.SAFETY,
            target_device=target_device,
            target_line=target_line,
        ),
        producer_sequence=1,
    )
    assert writer.wait(2000)
    result = writer.close(reason="safety_abort")

    assert not result.complete
    assert writer.failure is not None
    assert "valve" in writer.failure.message or "主阀" in writer.failure.message
    assert not descriptor.paths.final_dir.exists()


def test_duplicate_receipt_is_persisted_once_by_canonical_identity(tmp_path: Path) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation",),
    )
    assert writer.start_and_wait()

    receipt = _receipt()
    assert ingress.post_receipt(receipt, producer_sequence=1)
    assert ingress.post_receipt(receipt, producer_sequence=2)
    assert ingress.post_fence("actuation", producer_sequence=2)
    assert writer.close(reason="stopped").complete

    records = _read_jsonl(descriptor.paths.final_log_path)
    receipts = [record for record in records if record["record_type"] == "receipt"]
    assert len(receipts) == 1


def test_duplicate_receipt_preserves_envelope_sequence_for_later_records(
    tmp_path: Path,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation",),
    )
    assert writer.start_and_wait()

    receipt = _receipt()
    assert ingress.post_receipt(receipt, producer_sequence=1)
    assert ingress.post_receipt(receipt, producer_sequence=2)
    assert ingress.post_receipt(_receipt("command-2"), producer_sequence=3)
    assert ingress.post_fence("actuation", producer_sequence=3)
    assert writer.close(reason="stopped").complete

    validation = SessionFileService().validate_complete_bundle(
        descriptor.paths.final_dir
    )
    assert validation.complete, validation.reason
    records = _read_jsonl(descriptor.paths.final_log_path)
    assert [
        record["producer_sequence"]
        for record in records
        if record["producer"] == "actuation"
    ] == [1, 2, 3]
    assert sum(record["record_type"] == "receipt" for record in records) == 2


@pytest.mark.parametrize(
    "conflict",
    [
        {"valve": 8},
        {"action": ActuationAction.CLOSE},
        {"category": ActuationCategory.MANUAL},
        {"target_device": "Dev2", "target_line": "P0.0"},
        {"result": ActuationResult.FAILED},
    ],
)
def test_conflicting_duplicate_receipt_fails_closed(
    tmp_path: Path,
    conflict: dict,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation",),
    )
    assert writer.start_and_wait()
    original = _receipt()
    conflicting = replace(original, **conflict)

    assert ingress.post_receipt(original, producer_sequence=1)
    assert ingress.post_receipt(conflicting, producer_sequence=2)
    assert writer.wait(2000)

    assert writer.failure is not None
    assert "canonical" in writer.failure.message or "冲突" in writer.failure.message
    assert not writer.close(reason="conflicting_receipt").complete
    assert not descriptor.paths.final_dir.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"generation": 99}, "generation"),
        ({"producer_sequence": 2}, "producer sequence"),
    ],
)
def test_old_generation_or_missing_sequence_fails_closed(
    tmp_path: Path,
    mutation: dict,
    message: str,
) -> None:
    descriptor, writer, ingress, latch = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()

    accepted = ingress.post_session_event(
        event="test",
        producer_sequence=mutation.get("producer_sequence", 1),
        source="controller",
        result="success",
        message="测试",
        generation=mutation.get("generation"),
    )

    assert not accepted
    assert not latch.read().recording_ready
    assert message in latch.read().message
    close = writer.close(reason="failed")
    assert not close.complete
    assert not descriptor.paths.final_dir.exists()
    assert descriptor.paths.staging_dir.exists()


def test_queue_full_is_nonblocking_and_latches_one_failure(tmp_path: Path) -> None:
    entered_write = threading.Event()
    release_write = threading.Event()

    def slow(stage: str, _path: Path) -> None:
        if stage == "raw_write":
            entered_write.set()
            assert release_write.wait(2)

    descriptor, writer, ingress, latch = _writer(
        tmp_path,
        capacity=1,
        expected_producers=("hardware",),
        fault_injector=slow,
    )
    failures = []
    writer.failure_ready.connect(failures.append)
    assert writer.start_and_wait()
    assert ingress.post_raw_batch(_batch(1), producer_sequence=1)
    assert entered_write.wait(1)
    assert ingress.post_raw_batch(_batch(3), producer_sequence=2)

    started = time.perf_counter()
    accepted = ingress.post_raw_batch(_batch(5), producer_sequence=3)
    elapsed = time.perf_counter() - started

    assert not accepted
    assert elapsed < 0.05
    assert not latch.read().recording_ready
    assert "队列已满" in latch.read().message
    release_write.set()
    writer.wait(2000)
    assert len(failures) == 1
    assert not descriptor.paths.final_dir.exists()


def test_queue_full_failure_callback_runs_after_ingress_lock_is_released(
    tmp_path: Path,
) -> None:
    entered_write = threading.Event()
    release_write = threading.Event()
    callback_started = threading.Event()
    callback_completed = threading.Event()
    actuation_lock = threading.Lock()
    owner_acquired_ingress: list[bool] = []

    def slow(stage: str, _path: Path) -> None:
        if stage == "raw_write":
            entered_write.set()
            assert release_write.wait(2)

    descriptor = _descriptor(tmp_path)
    latch = RecorderReadinessLatch()
    ingress_holder: dict[str, SessionRecorderIngress] = {}

    def on_failure(_failure) -> None:
        callback_started.set()
        with actuation_lock:
            pass
        callback_completed.set()

    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(
            queue_capacity=1,
            flush_every_records=1000,
            close_timeout_ms=2000,
        ),
        expected_producers=("hardware",),
        session_started_payload=_started_payload(),
        readiness_latch=latch,
        fault_injector=slow,
        failure_callback=on_failure,
    )
    ingress = SessionRecorderIngress(writer, latch)
    ingress_holder["ingress"] = ingress
    assert writer.start_and_wait()
    assert ingress.post_raw_batch(_batch(1), producer_sequence=1)
    assert entered_write.wait(1)
    assert ingress.post_raw_batch(_batch(3), producer_sequence=2)

    def owner_path() -> None:
        with actuation_lock:
            assert callback_started.wait(0.5)
            ingress_acquired = ingress._lock.acquire(timeout=0.2)
            owner_acquired_ingress.append(ingress_acquired)
            if ingress_acquired:
                ingress._lock.release()

    owner = threading.Thread(target=owner_path)
    owner.start()
    producer = threading.Thread(
        target=lambda: ingress.post_raw_batch(_batch(5), producer_sequence=3)
    )
    producer.start()
    owner.join(timeout=0.5)
    assert owner_acquired_ingress == [True]
    assert callback_completed.wait(0.5)
    producer.join(timeout=0.5)
    release_write.set()
    writer.wait(2000)

    assert not owner.is_alive()
    assert not producer.is_alive()


@pytest.mark.parametrize(
    "stage",
    [
        "raw_header_write",
        "log_session_started_write",
        "raw_write",
        "log_write",
        "raw_flush",
        "log_flush",
        "raw_fsync",
        "log_fsync",
        "raw_close",
        "log_close",
        "manifest_write",
        "manifest_flush",
        "manifest_fsync",
        "manifest_close",
        "manifest_replace",
        "publish_rename",
    ],
)
def test_every_writer_failure_stage_keeps_incomplete_bundle(
    tmp_path: Path,
    stage: str,
) -> None:
    tripped = False

    def fail(candidate: str, _path: Path) -> None:
        nonlocal tripped
        if candidate == stage and not tripped:
            tripped = True
            raise OSError(f"synthetic {stage}")

    descriptor, writer, ingress, latch = _writer(
        tmp_path,
        expected_producers=("hardware", "controller"),
        fault_injector=fail,
    )
    initialized = writer.start_and_wait()
    if initialized:
        assert ingress.post_raw_batch(_batch(), producer_sequence=1)
        assert ingress.post_session_event(
            event="test",
            producer_sequence=1,
            source="controller",
            result="success",
            message="测试事件",
        )
        assert ingress.post_fence("hardware", producer_sequence=1)
        assert ingress.post_fence("controller", producer_sequence=1)
        result = writer.close(reason="completed")
        assert not result.complete
    else:
        assert stage in {"raw_header_write", "log_session_started_write"}
    assert tripped
    assert not latch.read().recording_ready
    assert not descriptor.paths.final_dir.exists()
    assert descriptor.paths.staging_dir.exists()


def test_close_waits_for_all_fences_and_is_idempotent(tmp_path: Path) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("hardware", "actuation"),
    )
    assert writer.start_and_wait()
    assert ingress.post_raw_batch(_batch(), producer_sequence=1)
    assert ingress.post_fence("hardware", producer_sequence=1)

    result_holder = []

    def close() -> None:
        result_holder.append(writer.close(reason="stopped", timeout_ms=1500))

    closer_a = threading.Thread(target=close)
    closer_b = threading.Thread(target=close)
    closer_a.start()
    closer_b.start()
    assert ingress.post_receipt(_receipt(), producer_sequence=1)
    assert ingress.post_fence("actuation", producer_sequence=1)
    closer_a.join(timeout=2)
    closer_b.join(timeout=2)

    assert len(result_holder) == 2
    assert result_holder[0] is result_holder[1]
    assert result_holder[0].complete
    records = _read_jsonl(descriptor.paths.final_log_path)
    assert sum(record["event"] == "session_closed" for record in records) == 1
    assert writer.close(reason="again") is result_holder[0]


def test_close_timeout_does_not_publish_or_wait_forever(tmp_path: Path) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("hardware", "actuation"),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("hardware", producer_sequence=0)

    started = time.perf_counter()
    result = writer.close(reason="timeout", timeout_ms=30)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert not result.complete
    assert result.status == SessionStatus.RECOVERY_REQUIRED
    assert not descriptor.paths.final_dir.exists()
    assert descriptor.paths.staging_dir.exists()
    assert writer.wait(2000)


def test_slow_finalize_timeout_claims_terminal_result_and_never_publishes(
    tmp_path: Path,
) -> None:
    entered_finalize = threading.Event()
    release_finalize = threading.Event()

    def slow(stage: str, _path: Path) -> None:
        if stage == "manifest_write":
            entered_finalize.set()
            assert release_finalize.wait(2)

    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
        fault_injector=slow,
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)

    result = writer.close(reason="timeout", timeout_ms=30)
    assert entered_finalize.is_set()
    assert not result.complete
    assert result.status == SessionStatus.RECOVERY_REQUIRED
    release_finalize.set()
    assert writer.wait(2000)

    assert writer.close(reason="again") is result
    assert not descriptor.paths.final_dir.exists()
    assert descriptor.paths.staging_dir.exists()


def test_close_timeout_arbitrates_before_late_complete_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    publish_entered = threading.Event()
    release_publish = threading.Event()
    fake_monotonic = [100.0]

    def block_before_publish(stage: str, _path: Path) -> None:
        if stage == "publish_rename":
            publish_entered.set()
            assert release_publish.wait(2)

    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
        fault_injector=block_before_publish,
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    monkeypatch.setattr(
        session_writer_module.time,
        "monotonic",
        lambda: fake_monotonic[0],
    )

    finalized = writer._finalized

    class TimeoutThenLetWriterFinish:
        def wait(self, _timeout: float | None = None) -> bool:
            assert publish_entered.wait(2)
            fake_monotonic[0] = 101.0
            release_publish.set()
            assert finalized.wait(2)
            return False

        def set(self) -> None:
            finalized.set()

        def is_set(self) -> bool:
            return finalized.is_set()

    writer._finalized = TimeoutThenLetWriterFinish()  # type: ignore[assignment]

    result = writer.close(reason="deadline", timeout_ms=100)

    assert not result.complete
    assert result.status == SessionStatus.RECOVERY_REQUIRED
    assert writer.wait(2000)
    assert not descriptor.paths.final_dir.exists()


def test_shutdown_payload_cannot_override_canonical_log_identity(
    tmp_path: Path,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_session_event(
        event="shutdown",
        producer_sequence=1,
        source="controller",
        result="unsafe",
        message="关闭失败",
        payload={
            "schema": "evil",
            "schema_version": 999,
            "session_id": "other",
            "session_generation": 999,
            "session_sequence": 999,
            "timestamp": "1900-01-01T00:00:00+00:00",
            "producer": "attacker",
            "producer_sequence": 999,
            "event_id": "evil",
            "record_type": "receipt",
        },
    )
    assert ingress.post_fence("controller", producer_sequence=1)
    assert writer.close(reason="closed").complete

    shutdown = next(
        item
        for item in _read_jsonl(descriptor.paths.final_log_path)
        if item["event"] == "shutdown"
    )
    assert shutdown["schema"] == "olfactorypilot.event"
    assert shutdown["schema_version"] == 1
    assert shutdown["session_id"] == descriptor.session_id
    assert shutdown["session_generation"] == descriptor.generation
    assert shutdown["session_sequence"] != 999
    assert shutdown["producer"] == "controller"
    assert shutdown["producer_sequence"] == 1
    assert shutdown["event_id"] == f"controller:{descriptor.generation}:1"
    assert shutdown["record_type"] == "session_event"
    assert shutdown["timestamp"] != "1900-01-01T00:00:00+00:00"


def test_session_closed_does_not_claim_publish_before_bundle_rename(
    tmp_path: Path,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    assert writer.close(reason="closed").complete

    closed = _read_jsonl(descriptor.paths.final_log_path)[-1]
    assert closed["event"] == "session_closed"
    assert "已发布" not in closed["message"]
    assert "等待" in closed["message"] or "完成收尾" in closed["message"]


def test_failed_recording_rejects_normal_records_but_failure_is_reported_once(
    tmp_path: Path,
) -> None:
    def fail(stage: str, _path: Path) -> None:
        if stage == "log_write":
            raise OSError("disk lost")

    _, writer, ingress, latch = _writer(
        tmp_path,
        expected_producers=("controller",),
        fault_injector=fail,
    )
    assert writer.start_and_wait()

    assert ingress.post_session_event(
        event="first",
        producer_sequence=1,
        source="controller",
        result="success",
        message="第一次",
    )
    writer.wait(2000)
    assert not ingress.post_session_event(
        event="second",
        producer_sequence=2,
        source="controller",
        result="success",
        message="第二次",
    )

    failure = writer.failure
    assert failure is not None
    assert writer.failure is failure
    assert "磁盘空间或目录权限" in latch.read().message


def test_invalid_writer_config_uses_safe_defaults_and_logs_chinese_warning(
    caplog,
) -> None:
    config = SessionWriterConfig.from_mapping(
        {
            "session_writer_queue_capacity": 0,
            "session_writer_flush_every_records": "bad",
            "session_writer_close_timeout_ms": -1,
        }
    )

    assert config == SessionWriterConfig()
    assert "会话 writer 配置无效" in caplog.text


def test_active_writer_and_recovery_scanner_do_not_race_quarantine(
    tmp_path: Path,
) -> None:
    service = SessionFileService()
    descriptor = service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="active",
        generation=1,
    )
    latch = RecorderReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(close_timeout_ms=2000),
        expected_producers=("controller",),
        session_started_payload=_started_payload(),
        readiness_latch=latch,
    )
    ingress = SessionRecorderIngress(writer, latch)
    assert writer.start_and_wait()

    findings = service.scan_recovery(tmp_path)

    assert findings == ()
    assert descriptor.paths.staging_dir.is_dir()
    assert ingress.post_fence("controller", producer_sequence=0)
    assert writer.close(reason="closed").complete
    service.mark_inactive(descriptor.paths.staging_dir)


def test_publish_path_probe_never_holds_state_lock_against_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    probe_entered = threading.Event()
    release_probe = threading.Event()
    failure_returned = threading.Event()
    original_exists = Path.exists

    def blocking_exists(path: Path) -> bool:
        if path == descriptor.paths.final_dir:
            probe_entered.set()
            assert release_probe.wait(2)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", blocking_exists)
    closer = threading.Thread(
        target=lambda: writer.close(reason="test", timeout_ms=1000),
    )
    closer.start()
    assert probe_entered.wait(2)

    def fail_writer() -> None:
        writer.fail_from_producer(stage="late_unsafe", message="unsafe")
        failure_returned.set()

    failure = threading.Thread(target=fail_writer)
    failure.start()
    assert failure_returned.wait(1)
    release_probe.set()
    closer.join(2)
    failure.join(2)

    result = writer.close(reason="test", timeout_ms=100)
    assert not result.complete
    assert not descriptor.paths.final_dir.exists()


def test_publish_rollback_failure_marks_final_bundle_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    original_rename = session_writer_module.os.rename
    publish_completed = threading.Event()

    def fail_rollback(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path == descriptor.paths.final_dir
            and target_path == descriptor.paths.staging_dir
        ):
            assert publish_completed.is_set()
            raise OSError("synthetic rollback rename failure")
        original_rename(source, target)
        if (
            source_path == descriptor.paths.staging_dir
            and target_path == descriptor.paths.final_dir
        ):
            publish_completed.set()
            writer.fail_from_producer(
                stage="late_publish_failure",
                message="publish rename 后发生失败",
            )

    monkeypatch.setattr(session_writer_module.os, "rename", fail_rollback)

    result = writer.close(reason="test", timeout_ms=1000)

    assert publish_completed.is_set()
    assert not result.complete
    assert result.status == SessionStatus.RECOVERY_REQUIRED
    assert descriptor.paths.final_dir.is_dir()
    service = SessionFileService()
    validation = service.validate_complete_bundle(
        descriptor.paths.final_dir
    )
    assert not validation.complete
    assert "发布" in validation.reason or "恢复" in validation.reason
    findings = service.scan_recovery(tmp_path)
    assert len(findings) == 1
    assert findings[0].original_path == descriptor.paths.final_dir
    assert findings[0].quarantined_path is not None
    assert findings[0].quarantined_path.parent == tmp_path / "recovery"


def test_publish_rollback_marks_final_incomplete_when_staging_reappears(
    tmp_path: Path,
    monkeypatch,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    original_rename = session_writer_module.os.rename
    publish_completed = threading.Event()

    def recreate_staging_after_publish(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        original_rename(source, target)
        if (
            source_path == descriptor.paths.staging_dir
            and target_path == descriptor.paths.final_dir
        ):
            descriptor.paths.staging_dir.mkdir()
            publish_completed.set()
            writer.fail_from_producer(
                stage="late_publish_failure",
                message="publish 后 staging 路径被重建",
            )

    monkeypatch.setattr(
        session_writer_module.os,
        "rename",
        recreate_staging_after_publish,
    )

    result = writer.close(reason="test", timeout_ms=1000)

    assert publish_completed.is_set()
    assert not result.complete
    assert writer.wait(2000)
    validation = SessionFileService().validate_complete_bundle(
        descriptor.paths.final_dir
    )
    assert not validation.complete
    assert "发布" in validation.reason or "恢复" in validation.reason


def test_publish_rollback_invalidates_manifest_when_marker_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
    )
    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    original_rename = session_writer_module.os.rename
    original_open = Path.open
    rollback_attempted = threading.Event()

    def fail_rollback(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path == descriptor.paths.final_dir
            and target_path == descriptor.paths.staging_dir
        ):
            rollback_attempted.set()
            raise OSError("synthetic rollback rename failure")
        original_rename(source, target)
        if (
            source_path == descriptor.paths.staging_dir
            and target_path == descriptor.paths.final_dir
        ):
            writer.fail_from_producer(
                stage="late_publish_failure",
                message="publish rename 后发生失败",
            )

    def fail_marker_open(path: Path, *args, **kwargs):
        if (
            rollback_attempted.is_set()
            and path.name == ".olfactorypilot-publish-incomplete.json.tmp"
        ):
            raise OSError("synthetic marker open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(session_writer_module.os, "rename", fail_rollback)
    monkeypatch.setattr(Path, "open", fail_marker_open)

    result = writer.close(reason="test", timeout_ms=1000)

    assert rollback_attempted.is_set()
    assert not result.complete
    assert writer.wait(2000)
    validation = SessionFileService().validate_complete_bundle(
        descriptor.paths.final_dir
    )
    assert not validation.complete


@pytest.mark.parametrize("stream_name", ["raw", "log"])
@pytest.mark.parametrize("tamper_during_initialize", [False, True])
def test_writer_rejects_reserved_stream_modified_before_header_commit(
    tmp_path: Path,
    stream_name: str,
    tamper_during_initialize: bool,
) -> None:
    context: dict[str, object] = {}
    tampered = threading.Event()

    def inject(stage: str, _path: Path) -> None:
        expected_stage = (
            "raw_header_write"
            if stream_name == "raw"
            else "log_session_started_write"
        )
        if tamper_during_initialize and stage == expected_stage:
            descriptor = context["descriptor"]
            path = getattr(descriptor.paths, f"{stream_name}_path")
            with path.open("ab") as external:
                external.write(b"external-tamper")
            tampered.set()

    descriptor, writer, _ingress, _ = _writer(
        tmp_path,
        expected_producers=("controller",),
        fault_injector=inject,
    )
    context["descriptor"] = descriptor
    target = getattr(descriptor.paths, f"{stream_name}_path")
    if not tamper_during_initialize:
        target.write_bytes(b"external-tamper")
        tampered.set()

    assert not writer.start_and_wait()
    assert tampered.is_set()
    assert writer.failure is not None
    assert writer.failure.stage == "initialize"
    assert not descriptor.paths.final_dir.exists()


def test_writer_rejects_jsonl_record_exceeding_shared_validator_limit(
    tmp_path: Path,
) -> None:
    descriptor = SessionFileService().reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="oversized",
        generation=1,
        protocol_source="large.csv",
        protocol_metadata={"blob": "x" * MAX_STREAM_LINE_BYTES},
    )
    latch = RecorderReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(),
        expected_producers=("controller",),
        session_started_payload=_started_payload(),
        readiness_latch=latch,
    )

    assert not writer.start_and_wait()
    assert writer.failure is not None
    assert writer.failure.stage == "initialize"
    assert "单行" in writer.failure.message or "上限" in writer.failure.message
    assert not descriptor.paths.final_dir.exists()


def test_ingress_rejects_duplicate_raw_sample_identity(tmp_path: Path) -> None:
    _descriptor_value, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("hardware",),
    )
    assert writer.start_and_wait()
    batch = _batch(sequence=10)

    assert ingress.post_raw_batch(batch, producer_sequence=1)
    assert not ingress.post_raw_batch(batch, producer_sequence=2)
    assert writer.failure is not None
    assert writer.failure.stage == "raw_identity"
    assert writer.wait(2000)


def test_session_started_payload_cannot_override_canonical_fields(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor(tmp_path)
    latch = RecorderReadinessLatch()
    payload = {
        **_started_payload(),
        "schema": "forged",
        "session_id": "forged-session",
        "record_type": "receipt",
        "event": "forged_event",
        "timestamp": "1900-01-01T00:00:00+00:00",
        "producer": "forged-producer",
        "producer_sequence": 999,
        "event_id": "forged-event",
    }
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(),
        expected_producers=("controller",),
        session_started_payload=payload,
        readiness_latch=latch,
    )
    ingress = SessionRecorderIngress(writer, latch)

    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    assert writer.close(reason="completed").complete
    started = _read_jsonl(descriptor.paths.final_log_path)[0]
    assert started["schema"] == "olfactorypilot.event"
    assert started["session_id"] == descriptor.session_id
    assert started["record_type"] == "session_event"
    assert started["event"] == "session_started"
    assert started["producer"] == "session"
    assert started["producer_sequence"] == 1
    assert started["event_id"] == f"session:{descriptor.generation}:1"


def test_writer_rejects_nonfinite_json_value(tmp_path: Path) -> None:
    descriptor = _descriptor(tmp_path)
    latch = RecorderReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(),
        expected_producers=("controller",),
        session_started_payload={
            **_started_payload(),
            "protocol_metadata": {"invalid": float("nan")},
        },
        readiness_latch=latch,
    )

    assert not writer.start_and_wait()
    assert writer.failure is not None
    assert writer.failure.stage == "initialize"
    assert not descriptor.paths.final_dir.exists()


def test_readiness_latch_rejects_stale_generation_fail_and_close(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _descriptor(first_dir, generation=1)
    second = _descriptor(second_dir, generation=2)
    latch = RecorderReadinessLatch()
    latch.bind(first)
    assert latch.mark_ready(first)
    latch.bind(second)
    assert latch.mark_ready(second)

    assert not latch.fail(
        "late old failure",
        session_id=first.session_id,
        generation=first.generation,
    )
    assert not latch.close(
        session_id=first.session_id,
        generation=first.generation,
    )

    snapshot = latch.read()
    assert snapshot.session_id == second.session_id
    assert snapshot.generation == second.generation
    assert snapshot.recording_ready
    assert not snapshot.failed


def test_recording_started_timestamp_drives_session_lifecycle_fields(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor(tmp_path)
    recording_started_at = "2026-07-30T09:10:11.456+08:00"
    latch = RecorderReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(close_timeout_ms=2000),
        expected_producers=("controller",),
        session_started_payload={
            **_started_payload(),
            "recording_started_at": recording_started_at,
        },
        readiness_latch=latch,
    )
    ingress = SessionRecorderIngress(writer, latch)

    assert writer.start_and_wait()
    assert ingress.post_fence("controller", producer_sequence=0)
    assert writer.close(reason="completed").complete
    records = _read_jsonl(descriptor.paths.final_log_path)
    manifest = json.loads(
        descriptor.paths.final_manifest_path.read_text(encoding="utf-8")
    )

    assert records[0]["timestamp"] == recording_started_at
    assert records[-1]["started_at"] == recording_started_at
    assert manifest["started_at"] == recording_started_at


def test_quality_event_persists_transitions_and_aggregate_result(
    tmp_path: Path,
) -> None:
    descriptor, writer, ingress, _ = _writer(
        tmp_path,
        expected_producers=("actuation",),
    )
    snapshot = ActuationQualitySnapshot(
        open=ActuationStreamSnapshot(
            sample_count=20,
            p95_ms=25.0,
            warning=True,
            target_met=False,
        ),
        close=ActuationStreamSnapshot(
            sample_count=20,
            p95_ms=10.0,
            warning=False,
            target_met=True,
        ),
        combined=ActuationStreamSnapshot(
            sample_count=40,
            p95_ms=15.0,
            warning=False,
            target_met=True,
        ),
        last_jitter_ms=35.0,
        severe_latched=True,
    )

    assert writer.start_and_wait()
    assert ingress.post_quality_event(
        event="quality_transition",
        snapshot=snapshot,
        producer_sequence=1,
        command_id="quality-command",
        message="open p95 进入超限警告",
        timestamp=1_785_146_400.5,
        monotonic_ns=777,
        transitions=(
            {"stream": "open", "direction": "entered", "p95_ms": 25.0},
            {"stream": "close", "direction": "recovered", "p95_ms": 10.0},
        ),
    )
    assert ingress.post_fence("actuation", producer_sequence=1)
    assert writer.close(reason="completed", final_quality=snapshot).complete

    quality = next(
        record
        for record in _read_jsonl(descriptor.paths.final_log_path)
        if record["record_type"] == "quality_event"
    )
    assert quality["transitions"] == [
        {"stream": "open", "direction": "entered", "p95_ms": 25.0},
        {"stream": "close", "direction": "recovered", "p95_ms": 10.0},
    ]
    assert quality["result"] == "severe"
    assert (
        datetime.fromisoformat(quality["timestamp"]).timestamp()
        == 1_785_146_400.5
    )
    assert quality["monotonic_ns"] == 777
