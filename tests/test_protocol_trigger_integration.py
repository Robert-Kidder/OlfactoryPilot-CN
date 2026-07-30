from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.controllers.main_controller import MainController
from app.models import (
    ActuationAction,
    ActuationReceipt,
    ActuationResult,
    AppState,
    ProtocolDocument,
    ProtocolTrial,
    TriggerMode,
)
from app.models.protocol_execution import ProtocolExecutionStatus
from app.models.session import SessionStatus
from app.services.hal import AnalogInputFrame
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlPulse
from app.workers import HardwareWorker
from app.workers.session_writer import SessionWriterWorker


def _document(mode: TriggerMode = TriggerMode.MANUAL) -> ProtocolDocument:
    return ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=mode,
            )
        ],
    )


def _controller(mode: TriggerMode = TriggerMode.MANUAL) -> MainController:
    state = AppState(simulation_mode=True)
    state.valve_variants = {"20-channel": {1: "Dev1/P0.0", 2: "Dev1/P0.1"}}
    state.loaded_protocol = _document(mode)
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    worker = HardwareWorker(hal=MockHAL(), simulation=True)
    controller = MainController(state, worker, allow_test_actuation_bridge=True)
    controller.protocol_executor.reset(state.loaded_protocol)
    controller.actuation_interlock.update(recording_ready=True)
    return controller


def test_protocol_start_is_service_gated_without_recording_session() -> None:
    controller = _controller()
    controller._allow_test_actuation_bridge = False
    previous_epoch = controller.protocol_executor.state.execution_epoch

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.execution_epoch == previous_epoch
    assert "会话" in controller.state.status_message


def test_session_start_binds_protocol_and_rejects_repeated_start(tmp_path: Path) -> None:
    controller = _controller()

    assert controller.handle_session_start_requested(
        "S01",
        "条件 A",
        tmp_path,
    )
    descriptor = controller.session_state.descriptor
    assert descriptor is not None
    assert controller.session_state.status == SessionStatus.RECORDING
    assert descriptor.protocol_source == "demo.csv"
    assert not controller.handle_session_start_requested("S02", "条件 B", tmp_path)
    assert controller.session_state.descriptor is descriptor

    assert controller.handle_session_end_requested("test_cleanup")
    result = controller.wait_for_session_finalization(2.0)
    assert result is not None and result.complete


def test_active_session_rejects_protocol_replacement_and_keeps_binding(
    tmp_path: Path,
) -> None:
    controller = _controller()
    original = controller.state.loaded_protocol
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    candidate = tmp_path / "candidate.csv"
    candidate.write_text(
        "trial,timing_ms,duration_ms,valve,trigger\n"
        "new,0,100,2,manual\n",
        encoding="utf-8",
    )

    assert not controller.handle_protocol_file_selected(candidate)
    assert controller.state.loaded_protocol is original
    assert controller.session_state.descriptor is not None
    assert controller.session_state.descriptor.protocol_source == "demo.csv"
    assert "结束当前会话" in controller.state.status_message

    assert controller.handle_session_end_requested("test_cleanup")
    assert controller.wait_for_session_finalization(2.0).complete


def test_session_close_writes_protocol_bound_and_is_idempotent(tmp_path: Path) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    descriptor = controller.session_state.descriptor
    assert descriptor is not None

    assert controller.handle_session_end_requested("stopped")
    assert not controller.handle_session_end_requested("duplicate")
    first = controller.wait_for_session_finalization(2.0)
    second = controller.wait_for_session_finalization(2.0)

    assert first is second
    assert first.complete
    records = [
        __import__("json").loads(line)
        for line in descriptor.paths.final_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(record["event"] == "protocol_bound" for record in records) == 1
    assert not any(record["event"] == "protocol_loaded" for record in records)
    assert sum(record["event"] == "session_closed" for record in records) == 1


def test_hardware_owner_orders_actuation_then_recorder_then_ui() -> None:
    order = []
    hal = MockHAL()
    hal.read_ai_frames = MagicMock(
        return_value=[
            AnalogInputFrame(
                timestamp=10.0,
                ai0=0.25,
                ai6=0.0,
                monotonic_ns=1_000,
                ai_epoch=1,
                sample_sequence=0,
            )
        ]
    )
    worker = HardwareWorker(hal=hal, simulation=True)

    class Sink:
        def post_ai_batch(self, _batch) -> None:
            order.append("actuation")

    class Recorder:
        def post_raw_batch(self, _batch, *, producer_sequence: int) -> bool:
            assert producer_sequence == 1
            order.append("recorder")
            return True

        def post_fence(self, producer: str, *, producer_sequence: int) -> bool:
            assert (producer, producer_sequence) == ("hardware", 1)
            return True

    worker.set_actuation_sink(Sink())
    worker.set_session_recorder(Recorder())
    worker.breath_samples.connect(lambda _batch: order.append("ui"))

    worker._emit_ai_frame(10.0)
    worker.post_session_fence()

    assert order == ["actuation", "recorder", "ui"]


def test_hardware_recorder_bind_waits_for_inflight_ai_cutover() -> None:
    read_started = threading.Event()
    release_read = threading.Event()
    bind_finished = threading.Event()
    recorded: list[int] = []
    hal = MockHAL()

    reads = 0

    def controlled_read(_timestamp):
        nonlocal reads
        reads += 1
        if reads > 1:
            return []
        read_started.set()
        assert release_read.wait(1)
        return [
            AnalogInputFrame(
                timestamp=10.0,
                ai0=0.25,
                ai6=0.0,
                monotonic_ns=1_000,
                ai_epoch=1,
                sample_sequence=0,
            )
        ]

    hal.read_ai_frames = controlled_read
    worker = HardwareWorker(hal=hal, simulation=True)

    class Recorder:
        def post_raw_batch(self, _batch, *, producer_sequence: int) -> bool:
            recorded.append(producer_sequence)
            return True

        def post_fence(self, producer: str, *, producer_sequence: int) -> bool:
            assert producer == "hardware"
            return True

    recorder = Recorder()

    worker.start()
    assert read_started.wait(1)

    bind_result: list[bool] = []

    def bind() -> None:
        bind_result.append(
            worker.bind_session_recorder(recorder, generation=4, timeout_ms=1000)
        )
        bind_finished.set()

    binder = threading.Thread(target=bind)
    binder.start()
    assert not bind_finished.is_set()
    release_read.set()
    binder.join(1)
    assert worker.stop()

    assert not binder.is_alive()
    assert bind_result == [True]
    assert recorded == []


def test_timed_out_hardware_recorder_bind_cannot_arrive_late(
    monkeypatch,
) -> None:
    worker = HardwareWorker(hal=MockHAL(), simulation=True)
    append_finished = threading.Event()
    owner_second_acquire = threading.Event()
    release_owner = threading.Event()
    owner_ident: list[int] = []
    base_lock = threading.RLock()

    class OwnerBindGate:
        def __init__(self) -> None:
            self._owner_acquires = 0
            self._caller_acquires = 0

        def __enter__(self):
            if owner_ident and threading.get_ident() == owner_ident[0]:
                self._owner_acquires += 1
                if self._owner_acquires == 2:
                    owner_second_acquire.set()
                    assert release_owner.wait(2)
            base_lock.acquire()
            return self

        def __exit__(self, *_exc_info) -> None:
            is_owner = (
                bool(owner_ident)
                and threading.get_ident() == owner_ident[0]
            )
            base_lock.release()
            if not is_owner:
                self._caller_acquires += 1
                if self._caller_acquires == 1:
                    append_finished.set()

    worker._ttl_control_lock = OwnerBindGate()  # type: ignore[assignment]
    monkeypatch.setattr(worker, "isRunning", lambda: True)
    stale = object()
    bind_result: list[bool] = []
    bind_finished = threading.Event()

    def bind() -> None:
        bind_result.append(
            worker.bind_session_recorder(
                stale,
                generation=7,
                timeout_ms=20,
            )
        )
        bind_finished.set()

    binder = threading.Thread(target=bind)
    binder.start()
    assert append_finished.wait(1)

    def process_owner_queue() -> None:
        owner_ident.append(threading.get_ident())
        worker._process_ttl_control()

    owner = threading.Thread(target=process_owner_queue)
    owner.start()
    assert owner_second_acquire.wait(1)
    assert bind_finished.wait(1)
    release_owner.set()
    binder.join(1)
    owner.join(1)

    assert bind_result == [False]
    assert worker._session_recorder is None


def test_session_start_requires_hardware_owner_recorder_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller = _controller()
    bind_calls: list[tuple[object, int]] = []

    def reject_bind(recorder, *, generation: int, timeout_ms: int) -> bool:
        bind_calls.append((recorder, generation))
        return False

    monkeypatch.setattr(
        controller.worker,
        "bind_session_recorder",
        reject_bind,
        raising=False,
    )

    assert not controller.handle_session_start_requested("S01", "A", tmp_path)
    assert len(bind_calls) == 1
    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert controller.session_ingress is None


def test_recorder_failure_latches_before_actuation_wakeup_and_requires_new_session(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    failed_descriptor = controller.session_state.descriptor
    assert failed_descriptor is not None
    writer = controller.session_writer
    assert writer is not None

    writer.fail_from_producer(stage="synthetic_write", message="disk lost")
    controller._drain_actuation_if_not_running()

    snapshot = controller.actuation_interlock.read()[1]
    assert snapshot.recorder_failed
    assert not snapshot.recording_ready
    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert not failed_descriptor.paths.final_dir.exists()

    assert writer.wait(2000)
    assert controller.handle_session_start_requested("S02", "B", tmp_path)
    controller._drain_actuation_if_not_running()
    assert not controller.actuation_interlock.read()[1].recorder_failed
    assert controller.handle_session_end_requested("test_cleanup")
    assert controller.wait_for_session_finalization(2.0).complete


def test_new_generation_is_rejected_until_failed_writer_has_terminated(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    writer = controller.session_writer
    assert writer is not None
    writer.fail_from_producer(stage="synthetic_write", message="disk lost")
    original_is_running = writer.isRunning
    writer.isRunning = lambda: True  # type: ignore[method-assign]
    try:
        assert not controller.handle_session_start_requested("S02", "B", tmp_path)
        assert "尚未终止" in controller.state.status_message
    finally:
        writer.isRunning = original_is_running  # type: ignore[method-assign]
    assert writer.wait(2000)


def test_async_master_prepare_revalidates_session_document_and_generation(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    descriptor = controller.session_state.descriptor
    document = controller.state.loaded_protocol
    assert descriptor is not None and document is not None
    begun = []
    controller._begin_protocol_start = lambda *, document: begun.append(document)  # type: ignore[method-assign]
    request_id = "prepare-1"
    controller._pending_plan_ui[request_id] = {
        "kind": "protocol_master_prepare",
        "document": document,
        "session_id": descriptor.session_id,
        "session_generation": descriptor.generation,
    }
    assert controller.handle_session_end_requested("closing")

    controller._handle_actuation_plan_result(
        {"request_id": request_id, "success": True, "message": "prepared"}
    )

    assert begun == []
    assert "会话" in controller.state.status_message


def test_session_boundaries_wait_for_pending_plans_and_compensate_late_prepare(
    tmp_path: Path,
) -> None:
    controller = _controller()
    controller._protocol_start_pending = True

    assert not controller.handle_session_start_requested("S01", "A", tmp_path)
    assert "等待" in controller.state.status_message

    controller._protocol_start_pending = False
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    descriptor = controller.session_state.descriptor
    document = controller.state.loaded_protocol
    assert descriptor is not None and document is not None
    controller._pending_plan_ui["manual-pending"] = {
        "kind": "manual",
        "channels": [1],
    }
    finalized = []
    controller._begin_session_finalization = lambda: finalized.append(True)  # type: ignore[method-assign]

    assert controller.handle_session_end_requested("closing")
    assert finalized == []

    controller._pending_plan_ui.pop("manual-pending")
    request_id = "late-master-prepare"
    controller._pending_plan_ui[request_id] = {
        "kind": "protocol_master_prepare",
        "document": document,
        "session_id": descriptor.session_id,
        "session_generation": descriptor.generation,
    }
    controller._protocol_master_prepare_pending = True
    controller.actuation_worker.post_stop = MagicMock()

    controller._handle_actuation_plan_result(
        {"request_id": request_id, "success": True, "message": "prepared"}
    )

    controller.actuation_worker.post_stop.assert_called_once()
    assert "补偿" in controller.state.status_message or "关闭" in controller.state.status_message


def test_controller_fence_blocks_later_shutdown_event_append(tmp_path: Path) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    assert controller.handle_session_end_requested("closing")
    before = controller._session_controller_sequence
    assert controller._session_controller_fenced

    controller._finish_session_after_global_stop(
        {"source": "stop", "result": "success", "timestamp": "late"}
    )

    assert controller._session_controller_sequence == before
    assert controller._session_controller_fenced


def test_normal_finalizer_thread_is_tracked_and_identity_bound(tmp_path: Path) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    descriptor = controller.session_state.descriptor
    assert descriptor is not None

    assert controller.handle_session_end_requested("closed")
    thread = controller._session_finalize_thread

    assert thread is not None
    assert controller.wait_for_session_finalization(2.0).complete
    assert controller.session_state.descriptor is descriptor


def test_controller_finalization_never_reads_metrics_from_background_thread(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    owner_thread = threading.get_ident()
    calls: list[int] = []
    original_snapshot = controller.actuation_worker.metrics.snapshot

    def checked_snapshot():
        calls.append(threading.get_ident())
        return original_snapshot()

    controller.actuation_worker.metrics.snapshot = checked_snapshot  # type: ignore[method-assign]
    assert controller.handle_session_end_requested("closed")
    assert controller.wait_for_session_finalization(2.0).complete

    assert calls
    assert set(calls) == {owner_thread}


def test_new_writer_initialization_failure_replaces_closed_state_identity(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    first = controller.session_state.descriptor
    assert first is not None
    assert controller.handle_session_end_requested("closed")
    assert controller.wait_for_session_finalization(2.0).complete

    with patch.object(
        SessionWriterWorker,
        "_initialize_files",
        side_effect=OSError("synthetic open failure"),
    ):
        assert not controller.handle_session_start_requested("S02", "B", tmp_path)

    failed = controller.session_state.descriptor
    assert failed is not None
    assert failed.session_id != first.session_id
    assert failed.generation == first.generation + 1
    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert "initialize" in controller.session_state.failure_message


def test_writer_initialize_timeout_keeps_reservation_active_until_thread_stops(
    tmp_path: Path,
) -> None:
    controller = _controller()
    controller.config["session_writer_close_timeout_ms"] = 50
    initialize_entered = threading.Event()
    release_initialize = threading.Event()
    original_initialize = SessionWriterWorker._initialize_files

    def blocked_initialize(writer) -> None:
        initialize_entered.set()
        assert release_initialize.wait(2)
        original_initialize(writer)

    with patch.object(
        SessionWriterWorker,
        "_initialize_files",
        blocked_initialize,
    ):
        assert not controller.handle_session_start_requested("S01", "A", tmp_path)
        assert initialize_entered.is_set()
        writer = controller.session_writer
        descriptor = controller.session_state.descriptor
        assert writer is not None and descriptor is not None
        with controller.session_file_service._active_lock:
            assert descriptor.paths.staging_dir.resolve(strict=False) in (
                controller.session_file_service._active_staging
            )
        release_initialize.set()
        assert writer.wait(2000)


def test_recorder_bind_rechecks_boundary_after_writer_initializes(
    tmp_path: Path,
) -> None:
    controller = _controller()
    original_start = SessionWriterWorker.start_and_wait

    def initialize_then_make_boundary_unsafe(writer, *args, **kwargs):
        initialized = original_start(writer, *args, **kwargs)
        controller._protocol_start_pending = True
        return initialized

    with patch.object(
        SessionWriterWorker,
        "start_and_wait",
        initialize_then_make_boundary_unsafe,
    ):
        assert not controller.handle_session_start_requested("S01", "A", tmp_path)

    writer = controller.session_writer
    descriptor = controller.session_state.descriptor
    assert writer is not None and descriptor is not None
    assert writer.wait(2000)
    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert not descriptor.paths.final_dir.exists()
    controller._protocol_start_pending = False


def test_session_start_waits_for_pending_protocol_document_load(
    tmp_path: Path,
) -> None:
    controller = _controller()
    pending = replace(
        _document(),
        source_path=Path("pending.csv"),
        source_name="pending.csv",
    )
    controller._pending_protocol_load = pending

    assert not controller.handle_session_start_requested("S01", "A", tmp_path)
    assert controller.session_state.status == SessionStatus.IDLE
    assert controller.session_writer is None
    assert "协议" in controller.state.status_message


def test_late_protocol_load_success_cannot_replace_recording_session_document(
    tmp_path: Path,
) -> None:
    controller = _controller()
    bound = controller.state.loaded_protocol
    assert bound is not None
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    pending = replace(
        _document(),
        source_path=Path("late.csv"),
        source_name="late.csv",
    )
    controller._pending_protocol_load = pending

    try:
        controller._handle_document_result(
            {"document": pending, "success": True, "message": "late success"}
        )

        assert controller.state.loaded_protocol is bound
        assert controller._session_protocol_document is bound
        assert controller.session_state.status == SessionStatus.RECORDING
        assert "迟到" in controller.state.status_message or "活动会话" in (
            controller.state.status_message
        )
    finally:
        if controller.session_state.status == SessionStatus.RECORDING:
            controller.handle_session_end_requested("test_cleanup")
            controller.wait_for_session_finalization(2.0)


def test_late_protocol_load_success_cannot_replace_prepared_session_document(
    tmp_path: Path,
) -> None:
    controller = _controller()
    bound = controller.state.loaded_protocol
    assert bound is not None
    preview = controller.session_file_service.preview(
        output_dir=tmp_path,
        subject="S01",
        condition="prepared",
    )
    descriptor = controller.session_file_service.reserve(
        output_dir=tmp_path,
        subject="S01",
        condition="prepared",
        generation=1,
        protocol_source=bound.source_name,
        preview=preview,
    )
    assert controller.session_state.prepare(descriptor)
    controller._session_protocol_document = bound
    pending = replace(
        _document(),
        source_path=Path("late-prepared.csv"),
        source_name="late-prepared.csv",
    )
    controller._pending_protocol_load = pending

    controller._handle_document_result(
        {"document": pending, "success": True, "message": "late success"}
    )

    assert controller.state.loaded_protocol is bound
    assert controller._session_protocol_document is bound
    assert controller.session_state.status == SessionStatus.PREPARED


def test_stale_protocol_load_result_does_not_clear_newer_pending_request() -> None:
    controller = _controller()
    original = controller.state.loaded_protocol
    current = replace(
        _document(),
        source_path=Path("current.csv"),
        source_name="current.csv",
    )
    stale = replace(
        _document(),
        source_path=Path("stale.csv"),
        source_name="stale.csv",
    )
    controller._pending_protocol_load = current

    controller._handle_document_result(
        {"document": stale, "success": True, "message": "stale success"}
    )

    assert controller._pending_protocol_load is current
    assert controller.state.loaded_protocol is original


def test_pretest_thread_never_overwrites_session_finalizer_reference() -> None:
    controller = _controller()
    entered = threading.Event()
    release = threading.Event()

    def blocked_pretest(*_args) -> None:
        entered.set()
        assert release.wait(2)

    controller._run_pretest_sequence = blocked_pretest  # type: ignore[method-assign]
    controller.handle_pretest_sequence_request("open", [1], 1.0, 1.0, 1.0)
    assert entered.wait(1)

    assert controller._session_finalize_thread is None
    assert controller._pretest_thread is not None
    release.set()
    controller._pretest_thread.join(2)


def test_stale_finalizer_cannot_overwrite_new_generation_state(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    first_writer = controller.session_writer
    first_descriptor = controller.session_state.descriptor
    assert first_writer is not None and first_descriptor is not None
    assert controller.handle_session_end_requested("first")
    assert controller.wait_for_session_finalization(2.0).complete

    assert controller.handle_session_start_requested("S02", "B", tmp_path)
    second_descriptor = controller.session_state.descriptor
    assert second_descriptor is not None
    assert second_descriptor.generation > first_descriptor.generation
    controller._session_finalize_event.clear()
    controller._session_finalize_result = None

    controller._finalize_session_writer(
        writer=first_writer,
        descriptor=first_descriptor,
        reason="late-old-finalizer",
    )

    assert controller.session_state.status == SessionStatus.RECORDING
    assert controller.session_state.descriptor is second_descriptor
    assert controller._session_finalize_result is None
    assert not controller._session_finalize_event.is_set()
    assert controller.actuation_interlock.read()[1].recording_ready
    assert controller.handle_session_end_requested("cleanup")
    assert controller.wait_for_session_finalization(2.0).complete


def test_real_writer_failure_reaches_controller_and_terminates_before_retry(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    writer = controller.session_writer
    assert writer is not None

    def fail_log(stage: str, _path: Path) -> None:
        if stage == "log_write":
            raise OSError("synthetic disk failure")

    writer._fault_injector = fail_log
    assert controller._post_controller_session_event(
        event="test_failure",
        source="controller",
        result="success",
        message="触发真实 writer failure",
    )
    assert writer.wait(2000)
    controller._drain_actuation_if_not_running()

    assert writer.failure is not None
    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert not controller.recorder_readiness.read().recording_ready


def test_pause_resume_rearm_keep_same_session_and_completed_closes_it(
    tmp_path: Path,
) -> None:
    controller = _controller()
    assert controller.handle_session_start_requested("S01", "A", tmp_path)
    descriptor = controller.session_state.descriptor
    assert descriptor is not None
    controller.handle_protocol_start_requested()
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER

    controller.handle_protocol_pause_requested()
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.PAUSED
    controller.handle_protocol_resume_requested()
    controller.handle_protocol_rearm_requested()

    assert controller.session_state.descriptor is descriptor
    assert len(list(tmp_path.glob(".*.session.part"))) == 1

    controller.protocol_executor.state.status = ProtocolExecutionStatus.COMPLETED
    controller.actuation_worker._emit_snapshot()
    result = controller.wait_for_session_finalization(2.0)
    assert result is not None and result.complete


def test_controller_connects_ttl_worker_signals_once() -> None:
    state = AppState()
    worker = MagicMock(spec=HardwareWorker)

    MainController(state, worker)

    worker.ttl_pulse.connect.assert_called_once()
    worker.ttl_input_error.connect.assert_called_once()


def test_manual_handler_uses_common_readiness_even_when_ai6_not_ready() -> None:
    controller = _controller()
    controller.worker.hal = MagicMock(ttl_input_ready=False)

    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert controller.protocol_executor.state.current_mode == TriggerMode.MANUAL


def test_protocol_start_is_blocked_while_manual_valve_is_open() -> None:
    controller = _controller()
    controller.valve_service._states[1] = True
    previous_epoch = controller.protocol_executor.state.execution_epoch

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.execution_epoch == previous_epoch
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert "阀" in controller.state.status_message


def test_ttl_handler_forwards_immutable_payload_without_relabeling_epoch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    old_epoch = controller.protocol_executor.state.arm_epoch
    pulse = TtlPulse(
        timestamp=12.25,
        arm_epoch=old_epoch,
        sequence=9,
        monotonic_ns=12_250_000_000,
    )
    controller.protocol_executor.accept_trigger = MagicMock(
        return_value=controller.protocol_executor.empty_result()
    )

    controller.handle_ttl_pulse(pulse)

    kwargs = controller.protocol_executor.accept_trigger.call_args.kwargs
    assert kwargs["timestamp"] == 12.25
    assert kwargs["captured_epoch"] == old_epoch
    assert kwargs["sequence"] == 9


def test_queued_ttl_pulse_is_rejected_after_mode_switch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
        monotonic_ns=10_000_000_000,
    )

    controller.handle_protocol_trigger_mode_requested("manual")
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.current_mode == TriggerMode.MANUAL
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert controller.protocol_executor.state.recent_event.result == "ignored"


def test_queued_ttl_pulse_is_rejected_after_stop() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
        monotonic_ns=10_000_000_000,
    )
    old_epoch = queued.arm_epoch

    controller.handle_protocol_stop_requested()
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.STOPPED
    assert controller.protocol_executor.state.trial_index == 0
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert controller.protocol_executor.state.recent_event.result == "ignored"


def test_queued_ttl_pulse_cannot_advance_after_disconnect() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
        monotonic_ns=10_000_000_000,
    )
    old_epoch = queued.arm_epoch

    controller.state.telemetry.connected = False
    controller.actuation_interlock.update(connected=False, safety_state="DATA_STALE")
    controller.actuation_worker.post_readiness_update(
        readiness=controller._execution_readiness(), timestamp=10.0
    )
    controller._drain_actuation_if_not_running()
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.trial_index == 0
    assert controller.protocol_executor.state.waiting_started_at is None
    assert controller.protocol_executor.state.active_valve is None
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert controller.protocol_executor.state.recent_event.result == "rejected"


def test_ttl_read_error_blocks_running_executor_and_invalidates_epoch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    old_epoch = controller.protocol_executor.state.arm_epoch

    controller.handle_ttl_input_error("TTL/共享 AI 读取失败：USB disconnected")

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert "读取失败" in controller.protocol_executor.state.recent_event.message


def test_runtime_read_error_keeps_ttl_rearm_rejected_until_a_frame_recovers() -> None:
    class FailingHAL(MockHAL):
        def read_ai_frame(self, timestamp: float | None = None):
            raise RuntimeError("USB disconnected")

    controller = _controller(TriggerMode.TTL)
    controller.worker.hal = FailingHAL()
    controller.handle_protocol_start_requested()

    controller.worker._emit_ai_frame(10.0)
    controller.handle_protocol_rearm_requested()

    assert controller.worker.ttl_input_ready is False
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.recent_event.event == "rearm_rejected"
    assert "AI0" in controller.protocol_executor.state.recent_event.message


def test_breath_sample_blocks_on_fresh_readiness_loss_before_opening_valve() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.state.telemetry.connected = False

    controller.handle_breath_samples([-0.6], 10.0)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.active_valve is None
    assert controller.worker.hal.get_line_state("Dev1/P0.0") is None
    assert "连接" in controller.protocol_executor.state.recent_event.message


def test_protocol_replacement_close_failure_keeps_old_document_and_active_valve() -> None:
    controller = _controller()
    old_document = controller.state.loaded_protocol

    def writer(command):
        result = (
            ActuationResult.FAILED
            if command.action == ActuationAction.CLOSE
            else ActuationResult.SUCCESS
        )
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns if result == ActuationResult.SUCCESS else None,
            wall_timestamp=10.0,
            result=result,
            message="关闭失败" if result == ActuationResult.FAILED else "ok",
        )

    controller.actuation_worker.writer = writer
    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.handle_breath_samples([-0.6], 10.0)
    candidate = ProtocolDocument(
        source_path=Path("candidate.csv"),
        source_name="candidate.csv",
        trials=[
            ProtocolTrial(
                trial_id="new",
                timing_ms=0,
                duration_ms=100,
                valve=2,
                trigger=TriggerMode.TTL,
            )
        ],
    )

    with patch(
        "app.controllers.main_controller.parse_protocol_file",
        return_value=candidate,
    ):
        loaded = controller.handle_protocol_file_selected("candidate.csv")

    assert loaded is False
    assert controller.state.loaded_protocol is old_document
    assert controller.protocol_executor.state.document is old_document
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.active_valve == 1


def test_telemetry_cannot_release_pending_protocol_start_lease() -> None:
    controller = _controller()
    controller.actuation_worker.post_start = MagicMock()
    controller.handle_protocol_start_requested()

    assert controller._protocol_start_pending is True
    assert controller.flow_worker.execution_context[1] == "protocol"

    controller.handle_telemetry(
        {
            "timestamp": 10.0,
            "connected": True,
            "airflow": 1.0,
            "safety_state": "SAFE",
        }
    )

    assert controller._protocol_start_pending is True
    assert controller.actuation_interlock.read()[1].device_lease == "protocol"
    assert controller.flow_worker.execution_context[1] == "protocol"


def test_rejected_start_releases_pending_flow_lease() -> None:
    controller = _controller()
    controller.state.telemetry.connected = False
    controller.actuation_interlock.update(connected=False, safety_state="DATA_STALE")

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert controller._protocol_start_pending is False
    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[1] == "idle"


def test_rejected_start_from_blocked_state_does_not_retain_flow_lease() -> None:
    controller = _controller()
    controller.protocol_executor.state.status = ProtocolExecutionStatus.BLOCKED

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller._protocol_start_pending is False
    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[1] == "idle"


def test_start_epoch_resync_failure_releases_flow_lease_and_stops() -> None:
    controller = _controller()
    original_acquire = controller.flow_worker.acquire_protocol_lease
    calls = 0

    def fail_second_acquire(epoch: int) -> bool:
        nonlocal calls
        calls += 1
        return original_acquire(epoch) if calls == 1 else False

    controller.flow_worker.acquire_protocol_lease = fail_second_acquire

    controller.handle_protocol_start_requested()
    controller._drain_actuation_if_not_running()

    assert calls == 2
    assert controller._protocol_lease_epoch is None
    assert controller._protocol_start_pending is False
    assert controller.flow_worker.execution_context[1] == "idle"
    assert controller.actuation_interlock.read()[1].connected is False
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.STOPPED


def test_production_controller_rejects_start_when_owner_workers_are_stopped() -> None:
    state = AppState(simulation_mode=True)
    state.loaded_protocol = _document()
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    controller = MainController(state, HardwareWorker(hal=MockHAL(), simulation=True))
    controller.protocol_executor.reset(state.loaded_protocol)

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert controller.flow_worker.execution_context[1] == "idle"
    assert "worker" in controller.state.status_message


def test_active_epoch_drift_keeps_exact_flow_token_until_terminal_release() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    held_epoch = controller._protocol_lease_epoch
    assert held_epoch is not None

    active = replace(
        controller._protocol_snapshot,
        status=ProtocolExecutionStatus.BLOCKED,
        execution_epoch=held_epoch + 1,
    )
    controller._handle_protocol_snapshot(active)

    assert controller._protocol_lease_epoch == held_epoch
    assert controller.flow_worker.execution_context[:2] == (held_epoch, "protocol")

    terminal = replace(
        active,
        status=ProtocolExecutionStatus.STOPPED,
        execution_epoch=held_epoch + 2,
    )
    controller._handle_protocol_snapshot(terminal)

    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[:2] == (
        held_epoch + 2,
        "idle",
    )


def test_failed_terminal_flow_release_keeps_fail_closed_lease_token() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    held_epoch = controller._protocol_lease_epoch
    assert held_epoch is not None
    controller.flow_worker.release_protocol_lease = MagicMock(return_value=False)
    terminal = replace(
        controller._protocol_snapshot,
        status=ProtocolExecutionStatus.STOPPED,
        execution_epoch=held_epoch + 1,
    )

    controller._handle_protocol_snapshot(terminal)

    assert controller._protocol_lease_epoch == held_epoch
    interlock = controller.actuation_interlock.read()[1]
    assert interlock.device_lease == "protocol"
    assert interlock.connected is False
    assert "租约释放失败" in controller.state.status_message
