from __future__ import annotations

from PySide6.QtWidgets import QAbstractButton, QLabel, QMessageBox

from app.main import DEFAULT_CONFIG, build_application

FORBIDDEN_OPERATOR_TERMS = (
    "V3",
    "Mock",
    "intent",
    "安全收敛",
    "申请物理验证授权",
    "已由用户验证",
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
    assert "开始供气" in visible
    assert "释放气味" in visible
    assert "停止实验" in visible
    assert not any(term in visible for term in FORBIDDEN_OPERATOR_TERMS)
    assert not window.tabs.isVisibleTo(window)


def test_settings_entry_opens_independent_port_window_without_dev_terms(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    window.show()
    window.settings_button.click()
    qt_app.processEvents()

    dialog = window.settings_dialog
    assert dialog.isVisible()
    assert dialog.windowTitle() == "气口设置"
    visible = "\n".join(_visible_texts(dialog))
    assert "气口总览" in visible
    assert "测试气口" in visible
    assert not any(term in visible for term in FORBIDDEN_OPERATOR_TERMS)


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
        window.pretest_view.apply_safety_state("LOW_FLOW", "气流不足")
        controller.state.telemetry.connected = True
        controller.state.telemetry.safety_state = "LOW_FLOW"
        window.render_telemetry(controller.state.telemetry)
        qt_app.processEvents()

        visible_new = {widget for widget in qt_app.topLevelWidgets() if widget not in before and widget.isVisible()}
        assert visible_new == {window}
        assert not any(isinstance(widget, QMessageBox) for widget in visible_new)
        assert window.manual_experiment_view.notice_frame.isVisibleTo(window)
        assert "气流不足" in window.manual_experiment_view.status_label.text()
    finally:
        window.close()
