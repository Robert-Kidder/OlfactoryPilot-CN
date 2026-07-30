from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import app.controllers.main_controller as controller_module
from app.controllers.main_controller import MainController, RecoveryScanWorker
from app.models import AppState, ProtocolDocument, ProtocolTrial, TriggerMode
from app.models.session import SessionStatus, SessionViewSnapshot
from app.services.mock_hal import MockHAL
from app.views.main_window import MainWindow
from app.views.session_view import SessionView
from app.workers.hardware_worker import HardwareWorker


def _controller_and_window(tmp_path: Path):
    state = AppState(simulation_mode=True)
    state.valve_variants = {"20-channel": {1: "Dev1/P0.0"}}
    state.loaded_protocol = ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=TriggerMode.MANUAL,
            )
        ],
    )
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    controller = MainController(
        state,
        HardwareWorker(hal=MockHAL(), simulation=True),
        allow_test_actuation_bridge=True,
    )
    controller.protocol_executor.reset(state.loaded_protocol)
    window = MainWindow(controller, state)
    controller.bind_view(window)
    window.session_view.set_output_directory(tmp_path)
    return controller, window


def test_session_view_emits_intents_and_only_renders_snapshot(qt_app, tmp_path: Path) -> None:
    view = SessionView()
    previews = []
    starts = []
    ends = []
    recoveries = []
    view.preview_requested.connect(lambda *args: previews.append(args))
    view.start_requested.connect(lambda *args: starts.append(args))
    view.end_requested.connect(ends.append)
    view.recovery_requested.connect(lambda: recoveries.append(True))

    view.subject_input.setText("  CON  ")
    view.condition_input.setText("条件 A")
    view.set_output_directory(tmp_path)
    assert previews[-1] == ("  CON  ", "条件 A", str(tmp_path))
    assert list(tmp_path.iterdir()) == []

    snapshot = SessionViewSnapshot(
        status=SessionStatus.IDLE,
        status_text="可开始",
        subject_original="  CON  ",
        subject_clean="_CON",
        condition_original="条件 A",
        condition_clean="条件-A",
        stem="20260727-180000-123__CON_条件-A",
        staging_path=str(tmp_path / ".demo.session.part"),
        final_path=str(tmp_path / "demo"),
        raw_path=str(tmp_path / "demo" / "demo.raw"),
        log_path=str(tmp_path / "demo" / "demo.log"),
        can_start=True,
        can_end=False,
        inputs_enabled=True,
        recovery_messages=("发现未完成会话：demo",),
    )
    view.render_snapshot(snapshot)

    assert view.normalized_label.text() == "_CON / 条件-A"
    assert "20260727" in view.stem_label.text()
    assert ".session.part" in view.staging_path_label.text()
    assert view.start_button.isEnabled()
    assert not view.end_button.isEnabled()
    assert view.recovery_label.text() == "发现未完成会话：demo"
    view.start_button.click()
    view.end_button.setEnabled(True)
    view.end_button.click()
    view.recovery_button.click()
    assert starts[-1] == ("  CON  ", "条件 A", str(tmp_path))
    assert ends[-1] == "user_end"
    assert recoveries == [True]


def test_main_window_adds_chinese_file_tab_and_controller_preview(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view

    view.subject_input.setText("  CON  ")
    view.condition_input.setText("条件 A")
    qt_app.processEvents()

    labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    assert "文件" in labels
    assert view.normalized_label.text() == "_CON / 条件-A"
    assert view.start_button.isEnabled()
    assert not view.stem_input_is_editable
    assert not list(tmp_path.glob(".*.session.part"))
    assert controller.session_state.status == SessionStatus.IDLE


def test_active_session_freezes_inputs_and_protocol_capability(qt_app, tmp_path: Path) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()

    view.start_button.click()
    qt_app.processEvents()
    assert controller.session_state.status == SessionStatus.PREPARED
    view.start_button.click()
    qt_app.processEvents()

    assert controller.session_state.status == SessionStatus.RECORDING
    assert not view.subject_input.isEnabled()
    assert not view.condition_input.isEnabled()
    assert not view.output_button.isEnabled()
    assert not view.start_button.isEnabled()
    assert view.end_button.isEnabled()
    assert window.protocol_view._start_button.isEnabled()

    view.end_button.click()
    result = controller.wait_for_session_finalization(2.0)
    qt_app.processEvents()
    assert result is not None and result.complete
    assert view.subject_input.isEnabled()


def test_invalid_preview_shows_chinese_reason_and_disables_start(
    qt_app,
    tmp_path: Path,
) -> None:
    _, window = _controller_and_window(tmp_path)
    view = window.session_view

    view.subject_input.setText("<>..")
    view.condition_input.setText("A")
    qt_app.processEvents()

    assert not view.start_button.isEnabled()
    assert "不能为空" in view.status_label.text()


def test_closed_session_does_not_fall_back_to_stale_preview_and_protocol_refreshes_capability(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()
    stale_preview_stem = view.stem_label.text()
    view.start_button.click()
    qt_app.processEvents()
    view.start_button.click()
    qt_app.processEvents()
    descriptor = controller.session_state.descriptor
    assert descriptor is not None
    view.end_button.click()
    assert controller.wait_for_session_finalization(2.0).complete
    qt_app.processEvents()

    assert controller._session_preview is None
    assert view.stem_label.text() == descriptor.stem
    assert descriptor.stem != stale_preview_stem
    assert view.subject_input.isEnabled()
    controller.state.loaded_protocol = None
    controller._handle_document_result({"success": False, "document": None})
    qt_app.processEvents()
    assert not view.start_button.isEnabled()


def test_closed_session_input_edit_replaces_old_descriptor_preview(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()
    view.start_button.click()
    qt_app.processEvents()
    assert controller.session_state.status.value == "prepared"
    view.start_button.click()
    qt_app.processEvents()
    old = controller.session_state.descriptor
    assert old is not None
    view.end_button.click()
    assert controller.wait_for_session_finalization(2.0).complete
    qt_app.processEvents()

    view.subject_input.setText("S02")
    qt_app.processEvents()

    assert controller._session_preview is not None
    assert view.stem_label.text() == controller._session_preview.stem
    assert view.stem_label.text() != old.stem
    assert view.final_path_label.text() == str(controller._session_preview.final_dir)


def test_first_click_locks_current_timestamp_and_collision_path_before_recording(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view

    class MutableClock:
        def __init__(self) -> None:
            self.value = datetime(2026, 7, 29, 12, 0, 0, 1000).astimezone()

        def __call__(self):
            return self.value

    clock = MutableClock()
    controller.session_file_service._clock = clock
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()
    preview_before_click = controller._session_preview
    assert preview_before_click is not None
    clock.value = datetime(2026, 7, 29, 12, 0, 5, 2000).astimezone()
    click_stem = f"{clock.value.strftime('%Y%m%d-%H%M%S-%f')[:-3]}_S01_A"
    (tmp_path / click_stem).mkdir()

    view.start_button.click()
    qt_app.processEvents()

    locked = controller.session_state.descriptor
    assert locked is not None
    assert controller.session_state.status.value == "prepared"
    assert locked.started_at == clock.value.timestamp()
    assert locked.stem == click_stem + "__001"
    assert view.stem_label.text() == locked.stem
    assert view.final_path_label.text() == str(locked.paths.final_dir)
    assert view.start_button.text() == "确认开始记录"

    view.start_button.click()
    qt_app.processEvents()

    assert controller.session_state.status == SessionStatus.RECORDING
    assert controller.session_state.descriptor is locked
    view.end_button.click()
    result = controller.wait_for_session_finalization(2.0)
    assert result is not None and result.complete
    first_record = json.loads(
        locked.paths.final_log_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert datetime.fromisoformat(first_record["recording_started_at"]).tzinfo is not None


def test_failed_session_input_edit_replaces_failed_descriptor_preview(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()
    view.start_button.click()
    qt_app.processEvents()
    failed = controller.session_state.descriptor
    assert failed is not None
    assert controller.session_state.fail_start(
        failed,
        "synthetic failed preparation",
        recovery_required=False,
    )
    controller._render_session_snapshot()

    view.subject_input.setText("S03")
    qt_app.processEvents()

    assert controller.session_state.status == SessionStatus.FAILED
    assert controller._session_preview is not None
    assert view.stem_label.text() == controller._session_preview.stem
    assert view.stem_label.text() != failed.stem


def test_recovery_scan_is_async_refreshable_and_displays_last_sequence(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    deadline = time.time() + 2
    while controller._recovery_scan_worker is not None and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    staging = tmp_path / ".20260727-180000-123_S01_A.session.part"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        '{"schema":"olfactorypilot.session","schema_version":1,'
        '"status":"recording","session_id":"session-recovery",'
        '"session_generation":1,'
        '"stem":"20260727-180000-123_S01_A",'
        '"raw_file":"20260727-180000-123_S01_A.raw",'
        '"log_file":"20260727-180000-123_S01_A.log",'
        '"last_session_sequence":42}\n',
        encoding="utf-8",
    )
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original_scan = controller.session_file_service.scan_recovery

    def slow_scan(path, *, cancel_event=None):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return original_scan(path, cancel_event=cancel_event)

    controller.session_file_service.scan_recovery = slow_scan  # type: ignore[method-assign]
    started = time.perf_counter()
    controller.handle_session_preview_requested("S01", "A", str(tmp_path))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05
    deadline = time.time() + 1
    while not entered.is_set() and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert entered.is_set()
    release.set()
    deadline = time.time() + 2
    while controller._recovery_scan_worker is not None and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert calls == 1
    assert "最后成功序号：42" in window.session_view.recovery_label.text()

    controller.handle_session_preview_requested("S01", "A", str(tmp_path))
    deadline = time.time() + 2
    while calls < 2 and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert calls == 2


def test_teardown_cancels_recovery_worker_before_dropping_reference(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    existing = controller._recovery_scan_worker
    if existing is not None:
        existing.wait(2000)
        qt_app.processEvents()
    entered = threading.Event()
    release_legacy = threading.Event()

    def cancellable_scan(_path, *, cancel_event=None):
        entered.set()
        if cancel_event is not None:
            assert cancel_event.wait(2)
        else:
            assert release_legacy.wait(2)
        return ()

    controller.session_file_service.scan_recovery = cancellable_scan  # type: ignore[method-assign]
    controller._start_recovery_scan(tmp_path)
    worker = controller._recovery_scan_worker
    assert worker is not None and entered.wait(1)

    controller.teardown(timeout_ms=100)
    was_running_after_teardown = worker.isRunning()
    release_legacy.set()
    worker.wait(2000)

    assert not was_running_after_teardown
    assert controller._recovery_scan_worker is None


def test_stale_recovery_completion_cannot_clear_or_wait_on_new_worker(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    existing = controller._recovery_scan_worker
    if existing is not None:
        existing.wait(2000)
        qt_app.processEvents()
    old_worker = RecoveryScanWorker(
        controller.session_file_service,
        tmp_path,
        request_id=controller._recovery_scan_request,
    )
    old_worker.completed.connect(controller._handle_recovery_scan_completed)
    new_worker = MagicMock()
    new_worker.isRunning.return_value = True
    new_worker.wait.return_value = True
    controller._recovery_scan_worker = new_worker

    old_worker.completed.emit(
        controller._recovery_scan_request - 1,
        tmp_path,
        (),
    )

    assert controller._recovery_scan_worker is new_worker
    new_worker.wait.assert_not_called()
    new_worker.deleteLater.assert_not_called()


def test_teardown_releases_prepared_reservation_for_recovery(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("A")
    qt_app.processEvents()

    view.start_button.click()
    qt_app.processEvents()
    descriptor = controller.session_state.descriptor
    assert controller.session_state.status == SessionStatus.PREPARED
    assert descriptor is not None

    controller.teardown(timeout_ms=2000)
    recovery = controller.session_file_service.scan_recovery(tmp_path)

    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert len(recovery) == 1
    assert recovery[0].original_path == descriptor.paths.staging_dir


def test_prepared_session_has_explicit_cancel_to_recovery_path(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.session_view
    view.subject_input.setText("S01")
    view.condition_input.setText("wrong-condition")
    qt_app.processEvents()

    view.start_button.click()
    qt_app.processEvents()
    descriptor = controller.session_state.descriptor
    assert descriptor is not None
    assert controller.session_state.status == SessionStatus.PREPARED
    assert view.end_button.isEnabled()
    assert view.end_button.text() == "取消准备"

    view.end_button.click()
    qt_app.processEvents()

    assert controller.session_state.status == SessionStatus.RECOVERY_REQUIRED
    assert not descriptor.paths.final_dir.exists()
    assert view.subject_input.isEnabled()
    assert "取消" in view.status_label.text()


def test_recovery_action_opens_original_part_when_quarantine_move_failed(
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    retained = tmp_path / ".retained.session.part"
    retained.mkdir()
    opened: list[Path] = []
    controller._last_recovery_output = tmp_path
    controller._last_recovery_location = retained
    controller._open_with_system = opened.append  # type: ignore[method-assign]

    controller.handle_session_recovery_requested()

    assert opened == [retained]


def test_finished_recovery_worker_keeps_latest_pending_scan_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    existing = controller._recovery_scan_worker
    if existing is not None:
        existing.wait(2000)
    finished_worker = MagicMock()
    finished_worker.isRunning.return_value = False
    controller._recovery_scan_worker = finished_worker
    older_pending = tmp_path / "older"
    latest = tmp_path / "latest"
    controller._pending_recovery_output = older_pending
    created = []

    class FakeRecoveryScanWorker:
        def __init__(self, *args, **kwargs):
            created.append((args, kwargs))

        def start(self):
            return None

    monkeypatch.setattr(
        controller_module,
        "RecoveryScanWorker",
        FakeRecoveryScanWorker,
    )

    controller._start_recovery_scan(latest)

    assert controller._recovery_scan_worker is finished_worker
    assert controller._pending_recovery_output == latest
    assert created == []
