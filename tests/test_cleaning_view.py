from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from app.controllers.main_controller import MainController
from app.models import (
    AppState,
    CleaningStatus,
    CleaningViewSnapshot,
    DeviceLeaseKind,
)
from app.services.mock_hal import MockHAL
from app.views.cleaning_view import CleaningView
from app.views.main_window import MainWindow
from app.workers.hardware_worker import HardwareWorker


def _config(local_path: Path, *, duration_s: float = 0.01) -> dict:
    return {
        "language": "zh-CN",
        "window_title": "清洗测试",
        "telemetry_hz": 5,
        "safety_state": "SAFE",
        "hal_mode": "mock",
        "_local_config_path": local_path,
        "_config_write_path": local_path,
        "session_writer_close_timeout_ms": 2000,
        "cleaning": {
            "enabled": True,
            "gas_label": "Air",
            "flow_channel": "A",
            "default_flow_sccm": 1500,
            "max_approved_flow_sccm": 1500,
            "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
            "default_open_duration_s": duration_s,
            "max_open_duration_s": 120,
            "default_cycles": 1,
            "max_cycles": 20,
            "parallel_open_limit": 1,
            "default_channels": [2, 3],
            "external_labels": {"2": "2", "3": "4"},
        },
        "valve_mapping": {
            "master_valve": "Dev2/P1.0",
            "variants": {
                "20-channel": {
                    "1": "Dev1/P0.0",
                    "2": "Dev1/P0.1",
                    "3": "Dev1/P0.2",
                }
            },
        },
        "hardware_variant": "20-channel",
    }


def _controller_and_window(tmp_path: Path):
    config = _config(tmp_path / "local_config.json")
    state = AppState.from_config(config)
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.telemetry.airflow = 1.0
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    controller = MainController(
        state,
        HardwareWorker(hal=MockHAL(), simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    window = MainWindow(controller, state)
    controller.bind_view(window)
    controller._cleaning_output_root = tmp_path
    controller._render_cleaning_snapshot()
    return controller, window


def test_cleaning_view_has_20_routes_and_only_emits_intents(
    qt_app,
    monkeypatch,
) -> None:
    view = CleaningView()
    candidates = []
    starts = []
    outputs = []
    view.candidate_changed.connect(lambda *values: candidates.append(values))
    view.start_requested.connect(lambda: starts.append(True))
    view.output_requested.connect(lambda: outputs.append(True))
    view.render_snapshot(
        CleaningViewSnapshot(
            available_channels=tuple(range(1, 21)),
            selected_channels=(2, 3),
            external_labels=((2, "2"), (3, "4")),
            flow_sccm=1500,
            open_duration_s=10,
            cycles=3,
            estimated_duration_s=60,
            controls_enabled=True,
            can_start=True,
            output_root="D:/experiment",
        )
    )

    assert len(view.channel_checks) == 20
    assert "机外气路 4" in view.channel_checks[3].text()
    view.channel_checks[4].click()
    assert candidates[-1][0] == (2, 3, 4)
    assert "1.0 分钟" in view.estimate_label.text()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    view.start_button.click()
    assert starts == [True]
    view.output_button.click()
    assert outputs == [True]


def test_cleaning_view_locks_candidate_controls_while_running(qt_app) -> None:
    view = CleaningView()
    view.render_snapshot(
        CleaningViewSnapshot(
            status=CleaningStatus.RUNNING,
            status_text="正在自动清洗",
            available_channels=(2, 3),
            selected_channels=(2, 3),
            controls_enabled=False,
            can_stop=True,
            recording_ready=True,
            close_progress_text="4/4",
        )
    )

    assert not view.channel_checks[2].isEnabled()
    assert not view.flow_input.isEnabled()
    assert not view.start_button.isEnabled()
    assert view.stop_button.isEnabled()
    assert "记录就绪：是" in view.detail_label.text()


def test_controller_saves_only_while_disconnected_and_restores_local_override(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    view = window.cleaning_view
    controller.handle_cleaning_candidate_changed((2,), 1200, 2.5, 2)
    assert controller._cleaning_dirty
    assert not view.save_button.isEnabled()
    assert not view.start_button.isEnabled()
    assert not controller.handle_cleaning_save_requested((2,), 1200, 2.5, 2)
    assert all(
        text in controller._cleaning_display_message
        for text in ("保存失败", "安全动作", "下一步")
    )

    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    controller._render_cleaning_snapshot()
    assert view.save_button.isEnabled()
    assert controller.handle_cleaning_save_requested((2,), 1200, 2.5, 2)
    deadline = time.monotonic() + 2
    while controller._cleaning_save_in_progress and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()
    assert not controller._cleaning_save_in_progress

    saved = json.loads((tmp_path / "local_config.json").read_text(encoding="utf-8"))
    assert saved["cleaning"]["selected_channels"] == [2]
    assert saved["cleaning"]["flow_sccm"] == 1200
    restored = MainController(
        AppState.from_config(_config(tmp_path / "local_config.json")),
        HardwareWorker(hal=MockHAL(), simulation=True),
        config=_config(tmp_path / "local_config.json"),
        allow_test_actuation_bridge=True,
    )
    assert restored._cleaning_published is not None
    assert restored._cleaning_published.selected_channels == (2,)
    assert restored._cleaning_published.flow_sccm == 1200
    assert restored._cleaning_published.open_duration_s == 2.5
    assert restored._cleaning_published.cycles == 2


def test_controller_cleaning_publishes_complete_maintenance_bundle(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, window = _controller_and_window(tmp_path)
    assert window.cleaning_view.start_button.isEnabled()
    assert controller.handle_cleaning_start_requested(), controller._cleaning_display_message

    deadline = time.monotonic() + 2
    while (
        controller._cleaning_runtime.status != CleaningStatus.COMPLETED
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
        controller._drain_cleaning_if_not_running()
        qt_app.processEvents()

    result = controller.wait_for_cleaning_finalization(2)
    qt_app.processEvents()
    assert result is not None and result.complete, (
        f"status={controller._cleaning_runtime.status.value}; "
        f"reason={controller._cleaning_runtime.recovery_reason}; "
        f"message={controller._cleaning_display_message}; "
        f"possible={controller._cleaning_runtime.possibly_open}"
    )
    assert result.status == CleaningStatus.COMPLETED
    assert controller.device_lease.snapshot.kind == DeviceLeaseKind.IDLE
    assert controller._cleaning_runtime.close_confirmed >= (
        controller._cleaning_runtime.close_required
    )
    assert result.final_dir is not None
    assert (result.final_dir / "manifest.json").is_file()
    assert not list(result.final_dir.glob("*.raw"))


def test_global_stop_fences_and_finalizes_active_cleaning_bundle(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    controller.shutdown_service.record_path = tmp_path / "shutdown.json"
    assert controller.handle_cleaning_start_requested()
    assert controller._cleaning_lease_token is not None

    controller.stop_hardware()
    qt_app.processEvents()

    event = controller.state.last_shutdown_event
    assert event["result"] == "success", event
    assert controller.device_lease.snapshot.kind == DeviceLeaseKind.IDLE
    assert controller._cleaning_lease_token is None
    assert controller.actuation_worker._cleaning_lease_token is None
    assert controller.actuation_worker._cleaning_plan is None
    assert controller._cleaning_finalize_result is not None
    assert not controller._cleaning_writer.isRunning()
    assert not controller._cleaning_finalize_result.complete
    assert controller._cleaning_finalize_result.status == CleaningStatus.RECOVERY_REQUIRED


def test_controller_allows_cleaning_to_recover_from_idle_low_flow(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    controller.state.telemetry.safety_state = "LOW_FLOW"

    assert controller._cleaning_start_rejection() == ""


def test_controller_accepts_confirmed_startup_zero_for_cleaning_gate(
    qt_app,
    tmp_path: Path,
) -> None:
    controller, _window = _controller_and_window(tmp_path)
    controller.state.flow_setpoints_ready = False
    controller._startup_zero_confirmed = True

    assert controller._cleaning_start_rejection() == ""
