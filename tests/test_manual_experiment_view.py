from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

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
    assert view.port_layout.getItemPosition(
        view.port_layout.indexOf(view.port_buttons[1])
    )[:2] == (0, 0)
    assert view.port_layout.getItemPosition(
        view.port_layout.indexOf(view.port_buttons[20])
    )[:2] == (1, 9)
    assert not view.port_buttons[1].isEnabled()
    view.port_buttons[1].click()
    assert intents == []

    view.port_buttons[4].click()
    assert intents[-1].draft.selected_external_ports == (2, 4)
    assert view.release_button.isEnabled()


def test_direct_domain_snapshot_enables_idle_draft_and_stops_active_run(qtbot) -> None:
    view = ManualExperimentView()
    qtbot.addWidget(view)
    view.set_registry(_registry(), allow_mock=True)
    view.render_snapshot(ManualExperimentSnapshot())
    assert view.port_buttons[2].isEnabled()
    assert view.apply_flow_button.isEnabled()
    assert not view.release_button.isEnabled()

    view.port_buttons[2].click()
    assert view.release_button.isEnabled()
    view.render_snapshot(
        ManualExperimentSnapshot(status=ManualExperimentStatus.STIMULATING)
    )
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
    assert all(
        value in text
        for value in ("可用", "已选择", "开启回执已确认（非机械确认）", "故障")
    )
    assert view.port_buttons[2].property("portState") == "fault"
    assert "故障" in view.port_buttons[2].accessibleDescription()
    assert "不可用" in view.port_buttons[1].text()
    assert view.port_buttons[1].property("portState") == "unavailable"


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
                duration_s=2.5,
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
    assert releases[-1].duration_ns == 2_500_000_000


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

    assert view.telemetry_a_label.text() == "A路当前观测：123.4 sccm"
    assert "总流量" not in view.telemetry_a_label.text()
    assert "稳定" not in view.telemetry_a_label.text()
    assert view.countdown_label.text() == "剩余：2.0 秒"
    now[0] = 2_500_000_000
    view.refresh_countdown_display()
    assert view.countdown_label.text() == "剩余：0.5 秒"
    assert emitted == []

    view.update_a_observation(88.0)
    assert view.telemetry_a_label.text() == "A路当前观测：88.0 sccm"


def test_manual_view_data_objects_are_frozen() -> None:
    draft = ManualExperimentDraft()
    snapshot = ManualExperimentViewSnapshot(draft=draft)
    with pytest.raises(FrozenInstanceError):
        draft.duration_s = 10  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.status_text = "已完成"  # type: ignore[misc]
