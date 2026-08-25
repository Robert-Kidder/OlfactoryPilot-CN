from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qfluentwidgets import DoubleSpinBox

from app.models import (
    ChannelDescriptor,
    ChannelRegistry,
    ChannelVerification,
    ManualExperimentIntent,
    ManualExperimentSnapshot,
    ManualExperimentStatus,
    ManualSupplyIntent,
    VerificationStatus,
)
from app.views.manual_experiment_view import (
    DURATION_STEP_S,
    FLOW_STEP_ML_MIN,
    ManualExperimentDraft,
    ManualExperimentView,
    ManualExperimentViewSnapshot,
    ManualPortSnapshot,
)


def _channel(
    port: int,
    *,
    enabled: bool = False,
    verified: bool = False,
    name: str = "",
) -> ChannelDescriptor:
    if not enabled:
        return ChannelDescriptor(external_port=port, display_name=name)
    base = ChannelDescriptor(
        external_port=port,
        internal_valve=port,
        target=f"Dev1/P0.{port - 1}",
        enabled=True,
        display_name=name,
    )
    if not verified:
        return base
    return replace(
        base,
        verification=ChannelVerification(
            status=VerificationStatus.MOCK_VERIFIED,
            fingerprint=base.mapping_fingerprint,
            verified_at="2026-08-18",
        ),
    )


def _registry() -> ChannelRegistry:
    return ChannelRegistry(
        _channel(
            port,
            enabled=port in {2, 4},
            verified=port in {2, 4},
            name="薄荷" if port == 2 else "",
        )
        for port in range(1, 21)
    )


def _port_snapshots() -> tuple[ManualPortSnapshot, ...]:
    return tuple(
        ManualPortSnapshot(
            external_port=port,
            display_name="薄荷" if port == 2 else "",
            available=port in {2, 4},
            actually_open=port == 2,
            fault="回执不确定" if port == 2 else "",
        )
        for port in range(1, 21)
    )


def test_manual_view_is_fixed_2_by_10_and_unavailable_port_emits_no_intent(
    qtbot,
) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_registry(_registry(), allow_mock=True)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            ports=_port_snapshots(),
            draft=ManualExperimentDraft(selected_external_ports=(2,)),
        )
    )
    intents = []
    view.draft_changed.connect(intents.append)

    assert len(view.port_buttons) == 20
    assert view.port_layout.getItemPosition(view.port_layout.indexOf(view.port_buttons[1]))[:2] == (0, 0)
    assert view.port_layout.getItemPosition(view.port_layout.indexOf(view.port_buttons[20]))[:2] == (1, 9)
    assert not view.port_buttons[1].isEnabled()
    view.port_buttons[1].click()
    assert intents == []

    view.port_buttons[4].click()
    assert intents[-1].draft.selected_external_ports == (2, 4)
    assert not view.release_button.isEnabled()


def test_direct_domain_snapshot_enables_idle_draft_and_stops_active_run(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_registry(_registry(), allow_mock=True)
    view.render_snapshot(ManualExperimentSnapshot())
    assert view.port_buttons[2].isEnabled()
    assert view.apply_flow_button.isEnabled()
    assert not view.release_button.isEnabled()

    view.port_buttons[2].click()
    assert not view.release_button.isEnabled()
    view.render_snapshot(ManualExperimentSnapshot(status=ManualExperimentStatus.STIMULATING))
    assert not view.port_buttons[2].isEnabled()
    assert view.stop_button.isEnabled()


def test_manual_port_state_has_text_and_visual_distinction(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            ports=_port_snapshots(),
            draft=ManualExperimentDraft(selected_external_ports=(2,)),
        )
    )

    text = view.port_buttons[2].text()
    assert text == "薄荷\n气口 02"
    assert view.port_buttons[2].property("portState") == "fault"
    assert "故障" in view.port_buttons[2].accessibleDescription()
    assert not view.port_buttons[2].selection_accent.isHidden()
    assert not view.port_buttons[2].open_group.isHidden()
    assert not view.port_buttons[2].fault_group.isHidden()
    assert view.port_buttons[1].text() == "气口 01"
    assert "不可用" in view.port_buttons[1].accessibleDescription()
    assert view.port_buttons[1].property("portState") == "disabled"


def test_manual_port_alias_fallback_never_duplicates_number(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    ports = list(_port_snapshots())
    ports[1] = replace(ports[1], display_name="气口 2")
    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=True, ports=tuple(ports)))

    assert view.port_buttons[2].text() == "气口 02"
    assert view.port_buttons[2].text().count("气口 02") == 1


def test_manual_port_long_alias_is_elided_and_keeps_full_tooltip(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    ports = list(_port_snapshots())
    long_alias = "超长中文薄荷复合香味样品"
    ports[1] = replace(ports[1], display_name=long_alias)
    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=True, ports=tuple(ports)))

    button = view.port_buttons[2]
    assert long_alias in button.toolTip()
    assert "气口 02" in button.toolTip()
    assert button.elided_alias(54).endswith("…")
    assert button.minimumHeight() == button.maximumHeight() == 82


def test_manual_port_short_alias_has_no_redundant_tooltip(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    ports = list(_port_snapshots())
    ports[1] = replace(ports[1], display_name="薄荷")
    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=True, ports=tuple(ports)))

    assert view.port_buttons[2].title_label.text() == "薄荷"
    assert view.port_buttons[2].toolTip() == ""


def test_manual_flow_fields_derive_readonly_b_and_emit_domain_intents(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            can_apply_flow=True,
            can_release=True,
            supply_enabled=False,
            ports=_port_snapshots(),
            draft=ManualExperimentDraft(
                selected_external_ports=(2, 4),
                total_sccm=1000,
                sample_a_sccm=200,
                vacuum_c_sccm=50,
                duration_s=5,
            ),
        )
    )
    supply = []
    releases = []
    view.supply_requested.connect(supply.append)
    view.release_requested.connect(releases.append)

    assert view.main_b_input.isReadOnly()
    assert view.main_b_input.value() == 800
    view.total_input.setValue(1200)
    view.sample_a_input.setValue(300)
    assert view.main_b_input.value() == 900
    view.apply_flow_button.click()
    view.release_button.click()

    assert isinstance(supply[-1], ManualSupplyIntent)
    assert supply[-1].enabled is True
    assert supply[-1].total_sccm == 1200
    assert isinstance(releases[-1], ManualExperimentIntent)
    assert releases[-1].external_ports == (2, 4)
    assert releases[-1].duration_ns == 5_000_000_000
    assert view.total_input.singleStep() == FLOW_STEP_ML_MIN
    assert view.sample_a_input.singleStep() == FLOW_STEP_ML_MIN
    assert view.duration_input.singleStep() == DURATION_STEP_S


def test_selecting_port_does_not_optimistically_mark_it_open(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_registry(_registry(), allow_mock=True)
    view.render_snapshot(ManualExperimentSnapshot())

    tile = view.port_buttons[4]
    assert tile.open_group.isHidden()
    tile.click()

    assert tile.isChecked()
    assert not tile.selection_accent.isHidden()
    assert tile.open_group.isHidden()


def test_fluent_spin_boxes_follow_steps_and_disabled_state(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            draft=ManualExperimentDraft(total_sccm=1000, sample_a_sccm=500),
        )
    )
    assert isinstance(view.total_input, DoubleSpinBox)
    view.total_input.stepUp()
    assert view.total_input.value() == 1500
    view.total_input.stepDown()
    assert view.total_input.value() == 1000

    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=False))
    assert not view.total_input.isEnabled()


def test_manual_timer_only_refreshes_countdown_and_a_wording_is_exact(qtbot) -> None:
    now = [1_000_000_000]
    view = ManualExperimentView(monotonic_ns=lambda: now[0])
    qtbot.addWidget(view)
    emitted = []
    view.release_requested.connect(emitted.append)
    view.supply_requested.connect(emitted.append)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            experiment=ManualExperimentSnapshot(
                status=ManualExperimentStatus.STIMULATING,
                deadline_ns=3_000_000_000,
            ),
            telemetry_a_sccm=123.4,
        )
    )

    assert view.telemetry_a_label.text() == "123"
    assert "总流量" not in view.telemetry_a_label.text()
    assert "稳定" not in view.telemetry_a_label.text()
    assert view.countdown_label.text() == "剩余 2.0 秒"
    now[0] = 2_500_000_000
    view.refresh_countdown_display()
    assert view.countdown_label.text() == "剩余 0.5 秒"
    assert emitted == []

    view.update_a_observation(88.0)
    assert view.telemetry_a_label.text() == "88"


def test_manual_view_data_objects_are_frozen() -> None:
    draft = ManualExperimentDraft()
    snapshot = ManualExperimentViewSnapshot(draft=draft)
    with pytest.raises(FrozenInstanceError):
        draft.duration_s = 10  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.status_text = "已完成"  # type: ignore[misc]


def test_release_remains_blocked_until_updated_snapshot_allows_it(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            can_release=False,
            ports=_port_snapshots(),
        )
    )

    view.port_buttons[2].click()
    assert view.draft.selected_external_ports == (2,)
    assert not view.release_button.isEnabled()

    view.render_snapshot(replace(view.snapshot, can_release=True))
    assert view.release_button.isEnabled()


def test_unknown_supply_state_requests_conservative_stop(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            can_apply_flow=True,
            supply_enabled=None,
        )
    )
    intents = []
    view.supply_requested.connect(intents.append)

    assert view.apply_flow_button.text() == "停止供气"
    view.apply_flow_button.click()
    assert intents[-1].enabled is False


def test_alias_whitespace_is_normalized_before_single_line_elision(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    ports = list(_port_snapshots())
    ports[1] = replace(ports[1], display_name="高浓度\n薄荷\t样品")
    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=True, ports=tuple(ports)))

    tile = view.port_buttons[2]
    assert "\n" not in tile.alias
    assert "\t" not in tile.alias
    assert tile.alias == "高浓度 薄荷 样品"


def test_port_tile_ignores_right_click_and_supports_keyboard(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(controls_enabled=True, ports=_port_snapshots())
    )
    intents = []
    view.draft_changed.connect(intents.append)
    tile = view.port_buttons[2]

    QTest.mouseClick(tile, Qt.MouseButton.RightButton)
    assert intents == []
    tile.setFocus()
    QTest.keyClick(tile, Qt.Key.Key_Space)
    assert intents[-1].draft.selected_external_ports == (2,)


def test_closed_infobar_can_be_replaced_without_deleted_qobject_access(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show_notice("状态", "第一条", severity="info")
    first = view.notice_frame
    assert first is not None

    first.close()
    assert view.notice_frame is None
    view.show_notice("状态", "第二条", severity="info")
    assert view.notice_frame is not None
    assert view.detail_label.text() == "第二条"


def test_plain_snapshot_detail_replaces_stale_notice(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show_notice("旧错误", "旧内容", severity="error")

    view.render_snapshot(
        ManualExperimentViewSnapshot(detail_text="当前不可操作：设备尚未连接。")
    )
    assert view.current_notice_title == "状态"
    assert view.detail_label.text() == "当前不可操作：请先连接设备。"


def test_manual_recovery_replaces_transition_safety_notice(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_header_safety_state("LOW_FLOW")
    view.show_notice(
        "气流不足",
        "请检查供气。",
        severity="error",
        notice_key=("safety", 1, "SAFE", "LOW_FLOW"),
    )

    view.render_snapshot(
        ManualExperimentSnapshot(
            status=ManualExperimentStatus.RECOVERY_REQUIRED,
            recovery_reason="关阀回执不确定，请人工检查。",
        )
    )

    assert view.current_notice_title == "需要立即处理"
    assert "本次操作未能确认安全完成" in view.detail_label.text()
    assert "回执" not in view.detail_label.text()
    assert "RECOVERY_REQUIRED" not in view.detail_label.text()


def test_one_second_snapshot_duration_is_not_silently_clamped(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.render_snapshot(
        ManualExperimentViewSnapshot(
            controls_enabled=True,
            can_release=True,
            ports=_port_snapshots(),
            draft=ManualExperimentDraft(selected_external_ports=(2,), duration_s=1),
        )
    )
    intents = []
    view.release_requested.connect(intents.append)

    assert view.duration_input.value() == 1
    view.release_button.click()
    assert intents[-1].duration_ns == 1_000_000_000
