from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qfluentwidgets import DoubleSpinBox, InfoBarIcon

from app.models import (
    ChannelDescriptor,
    ChannelRegistry,
    ChannelVerification,
    ManualExperimentIntent,
    ManualExperimentSnapshot,
    ManualExperimentStatus,
    ManualPresentationSnapshot,
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


def test_manual_flow_fields_edit_independent_abc_and_emit_domain_intents(qtbot) -> None:
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
                sample_a_sccm=200,
                main_b_sccm=800,
                vacuum_c_sccm=50,
                duration_s=5,
            ),
        )
    )
    supply = []
    releases = []
    view.supply_requested.connect(supply.append)
    view.release_requested.connect(releases.append)

    assert view.main_b_input.value() == 800
    view.sample_a_input.setValue(300)
    view.main_b_input.setValue(900)
    assert view.main_b_input.value() == 900
    assert view.derived_total_label.text() == "A+B：1200 ml/min"
    view.apply_flow_button.click()
    view.release_button.click()

    assert isinstance(supply[-1], ManualSupplyIntent)
    assert supply[-1].enabled is True
    assert supply[-1].sample_a_sccm == 300
    assert supply[-1].main_b_sccm == 900
    assert isinstance(releases[-1], ManualExperimentIntent)
    assert releases[-1].external_ports == (2, 4)
    assert releases[-1].duration_ns == 5_000_000_000
    assert view.sample_a_input.singleStep() == FLOW_STEP_ML_MIN
    assert view.main_b_input.singleStep() == FLOW_STEP_ML_MIN
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
            draft=ManualExperimentDraft(sample_a_sccm=500, main_b_sccm=500),
        )
    )
    assert isinstance(view.main_b_input, DoubleSpinBox)
    view.main_b_input.stepUp()
    assert view.main_b_input.value() == 1000
    view.main_b_input.stepDown()
    assert view.main_b_input.value() == 500

    view.render_snapshot(ManualExperimentViewSnapshot(controls_enabled=False))
    assert not view.main_b_input.isEnabled()


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
    creation_count = view.notice_creation_count

    first.close()
    assert view.notice_frame is None
    view.show_notice("状态", "第一条", severity="info")
    assert view.notice_frame is None
    assert view.notice_creation_count == creation_count

    view.clear_notice_event()
    view.show_notice("状态", "第一条", severity="info")
    assert view.notice_frame is not None
    assert view.notice_creation_count == creation_count + 1
    view.show_notice("状态", "第二条", severity="info")
    assert view.notice_frame is not None
    assert view.detail_label.text() == "第二条"


def test_managed_infobar_timeout_retires_transient_but_not_actionable(
    qtbot,
) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show()

    view.show_notice(
        "实验已完成",
        "供气已恢复。",
        severity="success",
        actionable=False,
        source="result",
    )
    assert view.notice_frame is not None
    assert view.notice_frame.duration == -1
    assert view.notice_frame.property("managedDurationMs") == 2500
    assert view._notice_timer.isActive()
    # Exercise the view-owned timeout deterministically.  The coordinator
    # tests cover the exact duration policy; relying on a multi-second GUI
    # wait here also lets unrelated process-global QFluent animations from
    # preceding tests fire during this assertion.
    view._notice_timer.timeout.emit()
    assert view.notice_frame is None
    assert view._notification_coordinator.current is None
    creation_count = view.notice_creation_count
    view.show_notice(
        "实验已完成",
        "供气已恢复。",
        severity="success",
        actionable=False,
        source="result",
    )
    assert view.notice_frame is None
    assert view.notice_creation_count == creation_count

    view.show_notice(
        "请人工处理",
        "需要检查设备。",
        severity="warning",
        actionable=True,
        source="safety-action",
    )
    assert view.notice_frame is not None
    assert view.notice_frame.duration == -1
    assert view.notice_frame.property("managedDurationMs") == -1
    assert not view._notice_timer.isActive()
    assert view.notice_frame is not None


@pytest.mark.parametrize("severity", ("error", "critical"))
def test_same_condition_severity_escalation_updates_infobar_icon_in_place(
    qtbot, qt_app, severity
) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show()
    qt_app.processEvents()
    view.show_condition_notice(
        "设备异常",
        "请检查设备。",
        source="safety",
        condition_key=("safety", "FAULT"),
        severity="warning",
    )
    first = view.notice_frame
    assert first is not None
    initial_icon = first.icon
    initial_type = first.property("type")
    assert initial_icon == InfoBarIcon.WARNING
    assert initial_type == InfoBarIcon.WARNING.value

    view.show_condition_notice(
        "需要立即处理",
        "请立即停止操作。",
        source="safety",
        condition_key=("safety", "FAULT"),
        severity=severity,
    )

    assert view.notice_frame is not None
    assert view.notice_frame is first
    assert view.notice_creation_count == 1
    assert view.current_notice_severity == severity
    assert view.notice_frame.icon == InfoBarIcon.ERROR
    assert view.notice_frame.property("type") == InfoBarIcon.ERROR.value
    assert view.notice_frame.icon != initial_icon
    assert view.notice_frame.property("type") != initial_type


def test_same_order_resync_keeps_transient_deadline_but_payload_update_restarts(
    qtbot, qt_app,
) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show()
    qt_app.processEvents()
    view.show_notice(
        "已保存",
        "第一条结果",
        source="result",
        notice_key="saved",
        severity="success",
        actionable=False,
    )
    first_order = view._notice_order
    QTest.qWait(40)
    before_resync = view._notice_timer.remainingTime()

    view._sync_notice_output()
    after_resync = view._notice_timer.remainingTime()
    assert view._notice_order == first_order
    assert after_resync <= before_resync + 10

    QTest.qWait(40)
    before_update = view._notice_timer.remainingTime()
    view.show_notice(
        "已保存",
        "第二条结果",
        source="result",
        notice_key="saved",
        severity="success",
        actionable=False,
    )
    assert view._notice_order > first_order
    assert view._notice_timer.remainingTime() > before_update + 20


def test_close_callback_uses_updated_actionability_for_same_infobar(
    qtbot, qt_app
) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show()
    qt_app.processEvents()
    view.show_notice(
        "操作结果",
        "等待确认",
        source="result",
        notice_key="stable",
        severity="error",
        actionable=False,
    )
    original = view.notice_frame
    view.show_condition_notice(
        "已有警告",
        "稍后处理",
        source="warning",
        condition_key="warning",
        severity="warning",
    )
    view.show_notice(
        "操作失败",
        "请检查设备",
        source="result",
        notice_key="stable",
        severity="error",
        actionable=True,
    )
    assert view.notice_frame is original

    assert original is not None
    original.close()
    assert view.notice_frame is None
    assert view._notification_coordinator.current is None


def test_plain_snapshot_detail_replaces_stale_notice(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.show_notice(
        "旧错误",
        "旧内容",
        severity="error",
        source="manual-status",
    )

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


def test_identical_snapshot_does_not_mutate_tiles_and_one_port_updates_once(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    snapshot = ManualExperimentViewSnapshot(
        controls_enabled=True,
        ports=_port_snapshots(),
    )
    view.render_snapshot(snapshot)
    baseline = {
        port: tile.visual_mutation_count for port, tile in view.port_tiles.items()
    }

    view.render_snapshot(snapshot)
    assert {
        port: tile.visual_mutation_count for port, tile in view.port_tiles.items()
    } == baseline

    ports = list(snapshot.ports)
    ports[3] = replace(ports[3], actually_open=True)
    view.render_snapshot(replace(snapshot, ports=tuple(ports)))
    changed = [
        port
        for port, tile in view.port_tiles.items()
        if tile.visual_mutation_count != baseline[port]
    ]
    assert changed == [4]


def test_equal_telemetry_values_keep_distinct_sample_timestamps(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    auto_range = tuple(view.plot_widget.getViewBox().state["autoRange"])

    view.update_a_observation(250.0, sampled_at_s=10.0)
    view.update_a_observation(250.0, sampled_at_s=10.2)
    view.update_a_observation(260.0, sampled_at_s=10.2)
    view.update_a_observation(999.0, sampled_at_s=10.1)

    assert list(view._flow_history) == [(10.0, 250.0), (10.2, 260.0)]
    assert tuple(view.plot_widget.getViewBox().state["autoRange"]) == auto_range


def test_old_presentation_generation_cannot_overwrite_new_frame(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_registry(_registry(), allow_mock=True)
    experiment = ManualExperimentSnapshot(supply_enabled=True)
    newer = ManualPresentationSnapshot(
        generation=2,
        connected=True,
        hardware_ready=True,
        safety_state="SAFE",
        safety_reason="",
        airflow=250.0,
        telemetry_timestamp=2.0,
        experiment=experiment,
        controls_enabled=True,
        can_apply_flow=True,
        can_release=False,
        can_stop=False,
    )
    older = replace(
        newer,
        generation=1,
        connected=False,
        hardware_ready=False,
        controls_enabled=False,
        can_apply_flow=False,
    )

    view.render_presentation(newer)
    view.render_presentation(older)

    assert view._last_presentation_generation == 2
    assert view.snapshot.controls_enabled
    assert view.snapshot.can_apply_flow


def test_disconnected_presentation_clears_cached_airflow(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    connected = ManualPresentationSnapshot(
        generation=1,
        connected=True,
        hardware_ready=True,
        safety_state="SAFE",
        safety_reason="",
        airflow=250.0,
        telemetry_timestamp=1.0,
        experiment=ManualExperimentSnapshot(supply_enabled=True),
        controls_enabled=True,
        can_apply_flow=True,
        can_release=False,
        can_stop=False,
    )
    view.render_presentation(connected)
    view.render_presentation(
        replace(
            connected,
            generation=2,
            connected=False,
            hardware_ready=False,
            controls_enabled=False,
            can_apply_flow=False,
        )
    )

    assert view.snapshot.telemetry_a_sccm is None
    assert view.telemetry_a_label.text() == "暂无数据"
