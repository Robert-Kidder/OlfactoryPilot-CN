from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime

import pytest

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    CleaningOperationIdentity,
    CleaningStatus,
)
from app.services.session_file_service import SessionFileError, SessionFileService
from app.workers.session_writer import (
    MaintenanceReadinessLatch,
    MaintenanceRecorderIngress,
    SessionWriterConfig,
    SessionWriterWorker,
)


def _service() -> SessionFileService:
    return SessionFileService(
        clock=lambda: datetime(2026, 7, 31, 12, 34, 56, 789000, tzinfo=UTC),
        master_valve_line="Dev2/P1.0",
    )


def _descriptor(tmp_path):
    return _service().reserve_maintenance(
        output_dir=tmp_path,
        identity=CleaningOperationIdentity("clean-op-1", 2),
        plan_snapshot={
            "gas_label": "Air",
            "selected_channels": [2],
            "open_duration_s": 10,
            "cycles": 1,
        },
        step_count=2,
    )


def test_maintenance_writer_publishes_log_only_v1_bundle(tmp_path) -> None:
    descriptor = _descriptor(tmp_path)
    latch = MaintenanceReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(),
        expected_producers=("actuation", "controller", "flow"),
        session_started_payload={"owner": "actuation"},
        readiness_latch=latch,
    )
    ingress = MaintenanceRecorderIngress(writer, latch)

    assert descriptor.paths.output_dir == tmp_path / "maintenance"
    assert not hasattr(descriptor.paths, "raw_path")
    assert writer.start_and_wait()
    assert latch.read().recording_ready is True

    receipt = _receipt()
    assert ingress.post_receipt(receipt, producer_sequence=1)
    assert ingress.post_event(
        producer="controller",
        producer_sequence=1,
        record_type="state_transition",
        event="running",
        result="success",
        message="清洗已进入运行态。",
        payload={"step_id": "step-1"},
    )
    assert ingress.post_event(
        producer="flow",
        producer_sequence=1,
        record_type="flow_receipt",
        event="flow_confirmed",
        result="success",
        message="A/B/C 已确认。",
        payload={"a": 1500, "b": 0, "c": 0},
    )
    for producer in ("actuation", "controller", "flow"):
        assert ingress.post_fence(producer, producer_sequence=1)

    result = writer.close(
        reason="normal",
        maintenance_status=CleaningStatus.COMPLETED,
        maintenance_outcome="completed",
    )

    assert result.complete is True
    assert result.operation_id == "clean-op-1"
    assert descriptor.paths.final_dir.is_dir()
    assert sorted(path.name for path in descriptor.paths.final_dir.iterdir()) == [
        ".olfactorypilot-session-owner.json",
        descriptor.paths.log_path.name,
        "manifest.json",
    ]
    manifest = json.loads(
        descriptor.paths.final_manifest_path.read_text(encoding="utf-8")
    )
    log_bytes = descriptor.paths.final_log_path.read_bytes()
    log_records = [
        json.loads(line)
        for line in log_bytes.decode("utf-8").splitlines()
    ]
    assert manifest["schema"] == "maintenance-v1"
    assert manifest["operation_id"] == "clean-op-1"
    assert manifest["operation_generation"] == 2
    assert manifest["status"] == "complete"
    assert manifest["operation_status"] == "completed"
    assert manifest["outcome"] == "completed"
    assert manifest["step_count"] == 2
    assert manifest["receipt_count"] == 1
    assert manifest["producer_fences"] == {
        "actuation": 1,
        "controller": 1,
        "flow": 1,
    }
    assert manifest["log_sha256"] == hashlib.sha256(log_bytes).hexdigest()
    assert manifest["log_bytes"] == len(log_bytes)
    assert manifest["log_event_count"] == len(log_records)
    assert all(record["operation_id"] == "clean-op-1" for record in log_records)
    assert all("session_id" not in record for record in log_records)
    assert _service().validate_maintenance_bundle(
        descriptor.paths.final_dir
    ).complete is True


def test_maintenance_ingress_rejects_fenced_or_wrong_generation_without_raw_producer(
    tmp_path,
) -> None:
    descriptor = _descriptor(tmp_path)
    latch = MaintenanceReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(),
        expected_producers=("actuation", "controller", "flow"),
        session_started_payload={},
        readiness_latch=latch,
    )
    ingress = MaintenanceRecorderIngress(writer, latch)
    assert writer.start_and_wait()

    assert ingress.post_event(
        producer="actuation",
        producer_sequence=1,
        record_type="step",
        event="started",
        result="success",
        message="ok",
    )
    assert ingress.post_fence("actuation", producer_sequence=1)
    assert ingress.post_event(
        producer="actuation",
        producer_sequence=2,
        record_type="step",
        event="late",
        result="ignored",
        message="late",
    ) is False
    assert ingress.post_event(
        producer="hardware",
        producer_sequence=1,
        record_type="raw_batch",
        event="raw",
        result="rejected",
        message="raw",
    ) is False
    assert ingress.post_event(
        producer="flow",
        producer_sequence=1,
        generation=99,
        record_type="flow",
        event="stale",
        result="rejected",
        message="stale",
    ) is False
    assert writer.wait(1000)


def test_maintenance_finalize_failure_never_publishes_complete(tmp_path) -> None:
    descriptor = _descriptor(tmp_path)
    latch = MaintenanceReadinessLatch()

    def fault(stage, _path):
        if stage == "manifest_replace":
            raise OSError("manifest replace fault")

    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(close_timeout_ms=1000),
        expected_producers=("actuation", "controller", "flow"),
        session_started_payload={},
        readiness_latch=latch,
        fault_injector=fault,
    )
    ingress = MaintenanceRecorderIngress(writer, latch)
    assert writer.start_and_wait()
    for producer in ("actuation", "controller", "flow"):
        assert ingress.post_fence(producer, producer_sequence=0)

    result = writer.close(
        reason="normal",
        maintenance_status=CleaningStatus.COMPLETED,
        maintenance_outcome="completed",
    )

    assert result.complete is False
    assert descriptor.paths.final_dir.exists() is False
    assert descriptor.paths.staging_dir.exists() is True
    manifest = json.loads(descriptor.paths.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "recording"
    assert latch.read().recording_ready is False
    assert latch.read().failed is True
    terminal = writer.maintenance_terminal_snapshot
    assert terminal is not None
    assert terminal.operation_id == "clean-op-1"
    assert terminal.status == CleaningStatus.RECOVERY_REQUIRED
    assert "manifest replace fault" in terminal.reason
    findings = _service().scan_recovery(tmp_path)
    maintenance = [
        finding for finding in findings if "maintenance" in finding.reason
    ]
    assert len(maintenance) == 1
    assert maintenance[0].quarantined_path is not None
    assert maintenance[0].quarantined_path.parent == tmp_path / "maintenance" / "recovery"


def test_cc04_shutdown_cancels_finalize_without_losing_prefence_event(
    tmp_path,
) -> None:
    descriptor = _descriptor(tmp_path)
    latch = MaintenanceReadinessLatch()
    finalize_reached = threading.Event()
    allow_finalize = threading.Event()

    def pause_finalize(stage, _path):
        if stage == "manifest_replace":
            finalize_reached.set()
            assert allow_finalize.wait(2)

    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(close_timeout_ms=2000),
        expected_producers=("actuation", "controller", "flow"),
        session_started_payload={},
        readiness_latch=latch,
        fault_injector=pause_finalize,
    )
    ingress = MaintenanceRecorderIngress(writer, latch)
    assert writer.start_and_wait()
    assert ingress.post_event(
        producer="controller",
        producer_sequence=1,
        record_type="shutdown_race",
        event="before_shutdown",
        result="success",
        message="fence 前事件",
    )
    assert ingress.post_fence("controller", producer_sequence=1)
    assert ingress.post_fence("actuation", producer_sequence=0)
    assert ingress.post_fence("flow", producer_sequence=0)

    results = []
    closer = threading.Thread(
        target=lambda: results.append(
            writer.close(
                reason="normal",
                maintenance_status=CleaningStatus.COMPLETED,
                maintenance_outcome="completed",
            )
        )
    )
    closer.start()
    assert finalize_reached.wait(2)
    writer.fail_from_producer(
        stage="shutdown",
        message="shutdown 在 publish commit 前取消 finalize。",
    )
    assert ingress.post_event(
        producer="controller",
        producer_sequence=2,
        record_type="shutdown_race",
        event="after_fence",
        result="rejected",
        message="不得接受",
    ) is False
    allow_finalize.set()
    closer.join(2)

    assert results and results[0].complete is False
    assert descriptor.paths.final_dir.exists() is False
    assert descriptor.paths.staging_dir.exists() is True
    records = [
        json.loads(line)
        for line in descriptor.paths.log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record.get("event") == "before_shutdown" for record in records)
    assert not any(record.get("event") == "after_fence" for record in records)


@pytest.mark.parametrize(
    "stage",
    [
        "log_session_started_write",
        "log_write",
        "log_flush",
        "log_fsync",
        "log_close",
        "manifest_write",
        "manifest_flush",
        "manifest_fsync",
        "manifest_close",
        "manifest_replace",
        "publish_rename",
    ],
)
def test_maintenance_writer_fault_matrix_never_publishes_complete(
    tmp_path,
    stage: str,
) -> None:
    tripped = False

    def fail(candidate, _path):
        nonlocal tripped
        if candidate == stage and not tripped:
            tripped = True
            raise OSError(f"synthetic {stage}")

    descriptor = _descriptor(tmp_path)
    latch = MaintenanceReadinessLatch()
    writer = SessionWriterWorker(
        descriptor=descriptor,
        config=SessionWriterConfig(close_timeout_ms=1000),
        expected_producers=("actuation", "controller", "flow"),
        session_started_payload={},
        readiness_latch=latch,
        fault_injector=fail,
    )
    ingress = MaintenanceRecorderIngress(writer, latch)
    initialized = writer.start_and_wait()
    if initialized:
        for producer in ("actuation", "controller", "flow"):
            assert ingress.post_fence(producer, producer_sequence=0)
        result = writer.close(
            reason="normal",
            maintenance_status=CleaningStatus.COMPLETED,
            maintenance_outcome="completed",
        )
        assert result.complete is False
    else:
        assert stage == "log_session_started_write"
    assert tripped
    assert descriptor.paths.final_dir.exists() is False
    assert descriptor.paths.staging_dir.exists() is True
    assert latch.read().recording_ready is False


@pytest.mark.parametrize(
    "stage",
    ["create_staging", "create_owner_marker", "create_log", "create_manifest"],
)
def test_maintenance_reserve_fault_matrix_has_no_complete_bundle(
    tmp_path,
    stage: str,
) -> None:
    def fail(candidate, _path):
        if candidate == stage:
            raise OSError(f"synthetic {stage}")

    service = SessionFileService(
        clock=lambda: datetime(
            2026,
            7,
            31,
            12,
            34,
            56,
            789000,
            tzinfo=UTC,
        ),
        fault_injector=fail,
        master_valve_line="Dev2/P1.0",
    )
    with pytest.raises(SessionFileError):
        service.reserve_maintenance(
            output_dir=tmp_path,
            identity=CleaningOperationIdentity("reserve-fault", 1),
            plan_snapshot={},
            step_count=0,
        )
    assert not any(
        path.is_dir() and not path.name.startswith(".")
        for path in (tmp_path / "maintenance").glob("*")
    )


def _receipt() -> ActuationReceipt:
    command = ActuationCommand(
        command_id="clean-op-1:2:0001",
        execution_epoch=0,
        arm_epoch=0,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=2,
        action=ActuationAction.OPEN,
        category=ActuationCategory.CLEANING,
        expected_ns=100,
        duration_ns=None,
        wall_timestamp=1_700_000_000,
        safety_generation=0,
        target_device="Dev1",
        target_line="P0.1",
        operation_id="clean-op-1",
        generation=2,
        step_id="step-1",
        action_kind=ActuationAction.OPEN,
    )
    return ActuationReceipt.from_write(
        command=command,
        started_ns=100,
        actual_ns=110,
        wall_timestamp=1_700_000_000,
        result=ActuationResult.SUCCESS,
    )
