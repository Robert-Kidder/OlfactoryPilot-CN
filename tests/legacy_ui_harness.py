"""Explicit legacy View host for controller regressions, never used by app runtime."""

from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QWidget

from app.views import MainWindow
from app.views.calibration_view import CalibrationView
from app.views.cleaning_view import CleaningView
from app.views.hardware_settings_view import HardwareSettingsView
from app.views.pretest_view import PreTestView
from app.views.protocol_view import ProtocolView
from app.views.session_view import SessionView


def build_legacy_test_window(controller, state) -> MainWindow:
    """Attach old Views only when a regression test explicitly asks for them."""

    window = MainWindow(controller, state)
    host = QWidget(window)
    host.setObjectName("legacyControllerTestHost")
    host.hide()
    window._legacy_test_host = host
    window.tabs = QTabWidget(host)
    window.calibration_view = CalibrationView(
        inhale_threshold=state.inhale_threshold,
        exhale_threshold=state.exhale_threshold,
    )
    window.pretest_view = PreTestView(
        valve_map=state.get_active_valve_map(),
        variant=state.hardware_variant,
        master_valve=state.master_valve_line,
        inhale_threshold=state.inhale_threshold,
        exhale_threshold=state.exhale_threshold,
        signal_offset=state.signal_offset,
        signal_gain=state.signal_gain,
    )
    window.protocol_view = ProtocolView()
    window.session_view = SessionView()
    window.cleaning_view = CleaningView()
    window.hardware_settings_view = HardwareSettingsView()
    for label, view in (
        ("文件", window.session_view),
        ("清洗", window.cleaning_view),
        ("协议", window.protocol_view),
    ):
        window.tabs.addTab(view, label)
    for view in (
        window.calibration_view,
        window.pretest_view,
        window.hardware_settings_view,
    ):
        view.setParent(host)

    window.pretest_view.toggle_requested.connect(controller.handle_valve_toggle_request)
    window.pretest_view.apply_requested.connect(controller.handle_apply_request)
    window.pretest_view.valve_sequence_requested.connect(
        controller.handle_valve_sequence_request
    )
    window.pretest_view.sequence_requested.connect(
        controller.handle_pretest_sequence_request
    )
    window.protocol_view.load_requested.connect(controller.handle_protocol_file_selected)
    window.protocol_view.start_requested.connect(controller.handle_protocol_start_requested)
    window.protocol_view.stop_requested.connect(controller.handle_protocol_stop_requested)
    window.protocol_view.next_trial_requested.connect(controller.handle_protocol_next_requested)
    window.protocol_view.trigger_mode_requested.connect(
        controller.handle_protocol_trigger_mode_requested
    )
    window.protocol_view.manual_trigger_requested.connect(
        controller.handle_protocol_manual_trigger_requested
    )
    window.protocol_view.rearm_requested.connect(controller.handle_protocol_rearm_requested)
    window.protocol_view.pause_requested.connect(controller.handle_protocol_pause_requested)
    window.protocol_view.resume_requested.connect(controller.handle_protocol_resume_requested)
    window.session_view.preview_requested.connect(controller.handle_session_preview_requested)
    window.session_view.start_requested.connect(controller.handle_session_start_requested)
    window.session_view.end_requested.connect(controller.handle_session_end_requested)
    window.session_view.recovery_requested.connect(controller.handle_session_recovery_requested)
    window.cleaning_view.candidate_changed.connect(controller.handle_cleaning_candidate_changed)
    window.cleaning_view.save_requested.connect(controller.handle_cleaning_save_requested)
    window.cleaning_view.revert_requested.connect(controller.handle_cleaning_revert_requested)
    window.cleaning_view.start_requested.connect(controller.handle_cleaning_start_requested)
    window.cleaning_view.stop_requested.connect(controller.handle_cleaning_stop_requested)
    window.cleaning_view.recover_requested.connect(controller.handle_cleaning_recover_requested)
    window.cleaning_view.output_requested.connect(lambda: None)
    window.hardware_settings_view.mock_verify_requested.connect(
        controller.handle_hardware_mock_verify_requested
    )
    window.hardware_settings_view.save_requested.connect(
        controller.handle_hardware_profile_save_requested
    )
    window.hardware_settings_view.rollback_requested.connect(
        controller.handle_hardware_profile_rollback_requested
    )
    return window
