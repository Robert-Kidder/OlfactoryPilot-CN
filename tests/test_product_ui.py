from __future__ import annotations

import time

from PySide6.QtWidgets import QAbstractButton, QLabel, QMessageBox
from qfluentwidgets import InfoLevel

from app.main import DEFAULT_CONFIG, build_application
from app.views.calibration_view import CalibrationView
from app.views.cleaning_view import CleaningView
from app.views.hardware_settings_view import HardwareSettingsView
from app.views.pretest_view import PreTestView
from app.views.protocol_view import ProtocolView
from app.views.session_view import SessionView

FORBIDDEN_OPERATOR_TERMS = (
    "V3",
    "Mock",
    "intent",
    "安全收敛",
    "申请物理验证授权",
    "已由用户验证",
    "Controller",
    "Worker",
    "HAL",
    "snapshot",
    "owner",
    "lease",
)


def _visible_texts(root) -> list[str]:
    widgets = root.findChildren(QLabel) + root.findChildren(QAbstractButton)
    return [widget.text() for widget in widgets if widget.isVisibleTo(root)]


def test_product_ui_uses_operator_language_and_single_manual_entry(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    window.show()
    qt_app.processEvents()

    visible = "\n".join(_visible_texts(window))
    assert "手动实验" in visible
    assert "停止供气" in visible
    assert "释放气味" in visible
    assert "停止实验" in visible
    assert "全局停止" in visible
    assert not any(term in visible for term in FORBIDDEN_OPERATOR_TERMS)
    assert window.stackedWidget.count() == 1
    assert not hasattr(window, "tabs")


def test_product_window_does_not_construct_legacy_views(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    window.show()
    qt_app.processEvents()

    for legacy_type in (
        CalibrationView,
        CleaningView,
        HardwareSettingsView,
        PreTestView,
        ProtocolView,
        SessionView,
    ):
        assert window.findChildren(legacy_type) == []
    assert not hasattr(window, "settings_dialog")


def test_low_flow_and_connect_never_create_a_message_box_or_orphan_window(qt_app) -> None:
    before = set(qt_app.topLevelWidgets())
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    controller = window.controller
    try:
        window.show()
        controller.state.telemetry.connected = True
        controller.state.telemetry.safety_state = "LOW_FLOW"
        window.render_telemetry(controller.state.telemetry)
        qt_app.processEvents()

        visible_new = {widget for widget in qt_app.topLevelWidgets() if widget not in before and widget.isVisible()}
        assert visible_new == {window}
        assert not any(isinstance(widget, QMessageBox) for widget in visible_new)
        assert window.manual_experiment_view.notice_frame is not None
        assert window.manual_experiment_view.notice_frame.isVisibleTo(window)
        assert "气流不足" in window.manual_experiment_view.status_label.text()
    finally:
        window.close()


def test_production_telemetry_order_keeps_low_flow_feedback_as_error(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        window.controller.handle_telemetry(
            {
                "airflow": 0.0,
                "connected": True,
                "timestamp": time.time(),
                "safety_state": "LOW_FLOW",
            }
        )
        qt_app.processEvents()

        assert window.manual_experiment_view.current_notice_severity == "error"
        assert window.manual_experiment_view.notice_frame is not None
        assert window.manual_experiment_view.notice_frame.isVisibleTo(window)
    finally:
        window.close()


def test_connected_but_unsafe_device_is_not_shown_as_success(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.manual_experiment_view.clear_notice()
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "FAULT"
        telemetry.safety_reason = "设备状态异常"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        assert window._connection_badge.text() == "设备状态异常"
        assert window._connection_action_label.text() == "请检查设备状态"
        assert window._connection_badge.level == InfoLevel.ERROR
        assert window.manual_experiment_view.current_notice_severity == "error"
        assert "当前状态不允许操作" in window.manual_experiment_view.current_notice_title
    finally:
        window.close()


def test_safety_notice_is_transition_driven_and_reentry_can_notify_again(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "LOW_FLOW"
        telemetry.safety_reason = "气流低于安全阈值"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        first = window.manual_experiment_view.notice_frame
        assert first is not None
        first.close()
        qt_app.processEvents()
        notice = window.manual_experiment_view.notice_frame
        assert notice is None or not notice.isVisibleTo(window)
        visible = "\n".join(_visible_texts(window))
        assert "LOW_FLOW" not in visible
        assert "SAFE" not in visible
        assert "气流不足" in visible
        assert "请检查供气和管路" in visible

        window.controller._render_manual_snapshot()
        window.render_telemetry(telemetry)
        qt_app.processEvents()
        assert window.manual_experiment_view.notice_frame is None
        visible = "\n".join(_visible_texts(window))
        assert "气流不足" in visible
        assert "请检查供气和管路" in visible

        telemetry.safety_state = "SAFE"
        telemetry.safety_reason = "Alicat 气流正常"
        window.render_telemetry(telemetry)
        telemetry.safety_state = "LOW_FLOW"
        telemetry.safety_reason = "气流再次低于安全阈值"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        assert window.manual_experiment_view.notice_frame is not None
        assert "气流不足" in window.manual_experiment_view.current_notice_title
    finally:
        window.close()


def test_connected_normal_header_has_no_internal_safety_code_or_success_notice(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        window.manual_experiment_view.clear_notice()
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "SAFE"
        telemetry.safety_reason = "Alicat 气流正常"
        window.render_telemetry(telemetry)
        window.render_self_check([], True)
        qt_app.processEvents()

        visible = "\n".join(_visible_texts(window))
        assert "设备已连接" in visible
        assert "连接设备" not in visible
        assert "SAFE" not in visible
        assert "安全正常" not in visible
        assert "系统正常" not in visible
        notice = window.manual_experiment_view.notice_frame
        assert notice is None or not notice.isVisibleTo(window)
    finally:
        window.close()


def test_connected_data_stale_episode_notifies_after_disconnected_idle(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.manual_experiment_view.clear_notice()
        telemetry = window.state.telemetry
        telemetry.connected = False
        telemetry.safety_state = "DATA_STALE"
        telemetry.safety_reason = "设备未连接"
        window.render_telemetry(telemetry)
        assert "设备数据中断" not in window.manual_experiment_view.current_notice_title

        telemetry.connected = True
        telemetry.safety_reason = "气流采样已过期"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        assert window.manual_experiment_view.notice_frame is not None
        assert "设备数据中断" in window.manual_experiment_view.current_notice_title
        assert window._connection_badge.text() == "设备通信中断"
        assert window._connection_action_label.text() == "请检查设备连接"
        visible = "\n".join(_visible_texts(window))
        assert "DATA_STALE" not in visible
    finally:
        window.close()


def test_unknown_connected_safety_code_uses_natural_persistent_header_text(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "INTERNAL_STATE_42"
        telemetry.safety_reason = "epoch=7 owner=manual"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        assert window._connection_badge.text() == "设备状态异常"
        assert window._connection_action_label.text() == "请检查设备状态"
        visible = "\n".join(_visible_texts(window))
        assert "INTERNAL_STATE_42" not in visible
        assert "epoch=7" not in visible
        assert "owner=manual" not in visible
    finally:
        window.close()
