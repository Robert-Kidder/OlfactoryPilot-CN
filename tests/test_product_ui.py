from __future__ import annotations

import time
from types import SimpleNamespace

from PySide6.QtWidgets import QAbstractButton, QLabel, QMessageBox
from qfluentwidgets import InfoLevel

from app.main import DEFAULT_CONFIG, build_application
from app.models import ManualExperimentSnapshot, ManualPresentationSnapshot
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

FORBIDDEN_SETTINGS_TERMS = (
    "固定 2×10 布局",
    "别名、控制通道和启用状态",
    "标准通道预设",
    "编辑气口",
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
    assert window.stackedWidget.count() == 2
    assert isinstance(window.hardware_settings_view, HardwareSettingsView)
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
        PreTestView,
        ProtocolView,
        SessionView,
    ):
        assert window.findChildren(legacy_type) == []
    assert len(window.findChildren(HardwareSettingsView)) == 1
    assert not hasattr(window, "settings_dialog")


def test_settings_is_bottom_navigation_and_manual_shortcut_opens_same_view(
    qt_app,
) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    window.show()
    qt_app.processEvents()

    panel = window.navigationInterface.panel
    assert panel.bottomLayout.indexOf(window._settings_navigation_item) >= 0
    assert panel.topLayout.indexOf(window._settings_navigation_item) == -1
    window.manual_experiment_view.port_settings_button.click()
    qt_app.processEvents()
    assert window.stackedWidget.currentWidget() is window.hardware_settings_view
    assert window.findChildren(HardwareSettingsView) == [window.hardware_settings_view]


def test_settings_visible_copy_uses_two_clear_sections_without_removed_terms(
    qt_app,
) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        window.stackedWidget.setCurrentWidget(window.hardware_settings_view)
        qt_app.processEvents()
        settings = window.hardware_settings_view

        ports_text = "\n".join(_visible_texts(settings))
        assert "气口配置" in ports_text
        assert "线路与设备" in ports_text
        assert "气口总览" in ports_text
        assert "保存设置" in ports_text
        assert "控制通道表" not in ports_text

        settings.section_pivot.items["hardware"].click()
        qt_app.processEvents()
        hardware_text = "\n".join(_visible_texts(settings))
        assert "控制通道表" in hardware_text
        assert "设备连接" in hardware_text
        combined = f"{ports_text}\n{hardware_text}"
        assert not any(term in combined for term in FORBIDDEN_SETTINGS_TERMS)
        assert not any(term in combined for term in FORBIDDEN_OPERATOR_TERMS)
        assert settings.draft.profile_name not in combined
    finally:
        window.close()


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
        window.manual_experiment_view.resolve_notice_condition(source="last-shutdown")
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


def test_single_notice_slot_updates_in_place_and_never_replays_backlog(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        view = window.manual_experiment_view
        view.clear_notice()
        view.show_condition_notice(
            "请检查", "已有警告", source="warning",
            condition_key="warning", severity="warning",
        )
        original = view.notice_frame
        creation_count = view.notice_creation_count
        view.show_condition_notice(
            "无法继续", "请先处理", source="warning",
            condition_key="warning", severity="error",
        )
        qt_app.processEvents()
        assert view.notice_frame is original
        assert view.notice_creation_count == creation_count
        assert view.current_notice_severity == "error"

        view.show_condition_notice(
            "普通提醒", "已有信息", source="info",
            condition_key="info", severity="info",
        )
        view.show_notice(
            "已保存", "受阻期间的旧结果", source="result",
            notice_key="stale", severity="success", actionable=False,
        )
        assert original is not None
        original.close()
        qt_app.processEvents()
        assert view.notice_frame is None
        view.resolve_notice_condition(source="warning")
        view.resolve_notice_condition(source="info")
        qt_app.processEvents()
        assert view.notice_frame is None

        view.show_notice(
            "已保存", "恢复后的新结果", source="result",
            notice_key="fresh", severity="success", actionable=False,
        )
        qt_app.processEvents()
        assert view.notice_frame is not None
        assert view.current_notice_title == "已保存"
    finally:
        window.close()


def test_actionable_event_dismiss_keeps_real_window_quiet_until_higher_issue(
    qt_app,
) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        view = window.manual_experiment_view
        view.clear_notice()
        view.show_condition_notice(
            "请检查",
            "已有警告",
            source="warning",
            condition_key="warning",
            severity="warning",
        )
        view.show_notice(
            "操作失败",
            "请检查设备",
            source="action-result",
            notice_key="failure",
            severity="error",
            actionable=True,
        )
        event_bar = view.notice_frame
        view.show_condition_notice(
            "另一项错误",
            "同级问题稍后处理",
            source="equal-error",
            condition_key="equal-error",
            severity="error",
        )
        qt_app.processEvents()
        assert view.notice_frame is event_bar
        assert view.current_notice_title == "操作失败"

        assert event_bar is not None
        event_bar.close()
        qt_app.processEvents()
        assert view.notice_frame is None
        view.show_condition_notice(
            "需要立即处理",
            "请执行安全停止",
            source="critical",
            condition_key="critical",
            severity="critical",
        )
        qt_app.processEvents()
        assert view.notice_frame is not None
        assert view.current_notice_title == "需要立即处理"
    finally:
        window.close()


def test_real_status_caller_updates_stable_notice_in_place(qt_app) -> None:
    _, window = build_application(DEFAULT_CONFIG, start_worker=False, simulation=True)
    try:
        window.show()
        qt_app.processEvents()
        view = window.manual_experiment_view
        view.clear_notice()
        window.state.telemetry.connected = False
        window.update_status("保存失败，请重新保存。")
        frame = view.notice_frame
        identity = view._notice_identity
        creation_count = view.notice_creation_count

        window.update_status("保存失败，请检查设置后重试。")

        assert frame is not None and view.notice_frame is frame
        assert view._notice_identity == identity
        assert view.notice_creation_count == creation_count
        assert view.detail_label.text() == "保存失败，请检查设置后重试。"
    finally:
        window.close()


def test_real_actuation_alert_caller_updates_stable_notice_in_place(qt_app) -> None:
    _, window = build_application(DEFAULT_CONFIG, start_worker=False, simulation=True)
    try:
        window.show()
        qt_app.processEvents()
        view = window.manual_experiment_view
        view.clear_notice()
        window.render_actuation_alert("请停止操作。", severe=True)
        frame = view.notice_frame
        identity = view._notice_identity
        creation_count = view.notice_creation_count

        window.render_actuation_alert("请立即停止并检查气口。", severe=True)

        assert frame is not None and view.notice_frame is frame
        assert view._notice_identity == identity
        assert view.notice_creation_count == creation_count
        assert view.detail_label.text() == "请立即停止并检查气口。"
    finally:
        window.close()


def test_real_self_check_caller_updates_stable_notice_in_place(qt_app) -> None:
    _, window = build_application(DEFAULT_CONFIG, start_worker=False, simulation=True)
    try:
        window.show()
        qt_app.processEvents()
        view = window.manual_experiment_view
        view.clear_notice()
        first = SimpleNamespace(
            name="气流计", status="FAIL", reason="没有读数", suggestion="检查连接"
        )
        updated = SimpleNamespace(
            name="气流计", status="FAIL", reason="读数异常", suggestion="重新连接"
        )
        window.render_self_check([first], False)
        frame = view.notice_frame
        identity = view._notice_identity
        creation_count = view.notice_creation_count

        window.render_self_check([updated], False)

        assert frame is not None and view.notice_frame is frame
        assert view._notice_identity == identity
        assert view.notice_creation_count == creation_count
        assert "读数异常" in view.detail_label.text()
    finally:
        window.close()


def test_expected_manual_low_flow_transition_creates_no_safety_notice(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        window.manual_experiment_view.clear_notice()
        baseline = window.manual_experiment_view.notice_creation_count
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "LOW_FLOW"
        telemetry.safety_reason = "expected A zero transition"

        window.render_telemetry(
            telemetry,
            expected_manual_flow_transition=True,
            hardware_ready=True,
        )
        qt_app.processEvents()

        assert window.manual_experiment_view.notice_creation_count == baseline
        current = window.manual_experiment_view._notification_coordinator.current
        assert current is None or current.identity[0] != "condition"
    finally:
        window.close()


def test_connected_self_check_failure_is_not_presented_as_ready(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "SAFE"

        window.render_telemetry(telemetry, hardware_ready=False)

        assert window._connection_badge.text() == "设备检查未通过"
        assert window._connection_action_label.text() == "请检查设备后重新连接"
        assert window._connection_badge.level == InfoLevel.ERROR
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


def test_mica_is_disabled_and_overlay_does_not_change_core_geometry(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        qt_app.processEvents()
        assert not window.isMicaEffectEnabled()
        assert not hasattr(window.manual_experiment_view, "_notice_host")
        baseline = (
            window._connection_status_slot.geometry(),
            window._connection_action_slot.geometry(),
            window.manual_experiment_view.geometry(),
        )

        telemetry = window.state.telemetry
        telemetry.connected = True
        telemetry.safety_state = "LOW_FLOW"
        window.render_telemetry(telemetry)
        qt_app.processEvents()

        assert window.manual_experiment_view.notice_frame is not None
        assert window.manual_experiment_view.notice_frame.isVisibleTo(window)
        assert (
            window._connection_status_slot.geometry(),
            window._connection_action_slot.geometry(),
            window.manual_experiment_view.geometry(),
        ) == baseline
    finally:
        window.close()


def test_queued_presentation_coalesces_to_latest_coherent_generation(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    try:
        window.show()
        base = ManualPresentationSnapshot(
            generation=100,
            connected=False,
            hardware_ready=False,
            safety_state="DATA_STALE",
            safety_reason="设备未连接",
            airflow=0.0,
            telemetry_timestamp=100.0,
            experiment=ManualExperimentSnapshot(supply_enabled=False),
            controls_enabled=False,
            can_apply_flow=False,
            can_release=False,
            can_stop=False,
            detail_text="当前不可操作：设备尚未连接。",
        )
        latest = ManualPresentationSnapshot(
            generation=101,
            connected=True,
            hardware_ready=True,
            safety_state="SAFE",
            safety_reason="",
            airflow=250.0,
            telemetry_timestamp=101.0,
            experiment=ManualExperimentSnapshot(supply_enabled=True),
            controls_enabled=True,
            can_apply_flow=True,
            can_release=False,
            can_stop=False,
            detail_text="",
        )

        window.queue_presentation(base)
        window.queue_presentation(latest)
        qt_app.processEvents()

        assert window._rendered_presentation_generation == 101
        assert window._connection_badge.text() == "设备已连接"
        assert window.manual_experiment_view.snapshot.controls_enabled
        assert window.manual_experiment_view.snapshot.can_apply_flow
        assert window.manual_experiment_view.snapshot.supply_enabled is True
        assert window.manual_experiment_view.snapshot.detail_text == ""
        assert window.manual_experiment_view.current_notice_title != "设备数据中断"
    finally:
        window.close()
