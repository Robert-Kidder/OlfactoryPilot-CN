from __future__ import annotations

from app.views.pretest_view import PreTestView


def test_pretest_view_disables_when_no_mapping(qt_app):
    view = PreTestView(valve_map={}, variant="20-channel", master_valve="Dev1/P1.0")
    assert view._buttons == {}
    assert view.is_apply_enabled() is False
    assert view._warning_label.isHidden() is False
    assert "未找到 20 通道映射" in view._warning_label.text()


def test_pretest_view_renders_20_channels(qt_app):
    mapping = {i: f"Dev{i}/P0.0" for i in range(1, 21)}
    view = PreTestView(valve_map=mapping, variant="20-channel", master_valve="Dev1/P1.0")
    assert len(view._buttons) == 20
    assert list(view._buttons.keys())[:3] == [1, 2, 3]
    assert view._master_led is not None
    assert view.is_apply_enabled() is True
    assert view._apply_button.isHidden() is True
    assert view._start_button.toolTip() == ""
    assert all(widget.button.toolTip() == "" for widget in view._buttons.values())


def test_pretest_view_hides_safe_status_prompt(qt_app):
    mapping = {i: f"Dev{i}/P0.0" for i in range(1, 21)}
    view = PreTestView(valve_map=mapping, variant="20-channel", master_valve="Dev1/P1.0")

    view.apply_safety_state("DATA_STALE", "stale", disabled=True)
    assert view._status_label.isHidden() is False

    view.apply_safety_state("SAFE", "shutdown completed", disabled=False)

    assert view._status_label.isHidden() is True
    assert "shutdown completed" not in view._status_label.text()
