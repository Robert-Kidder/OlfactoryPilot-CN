from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from qfluentwidgets import InfoLevel

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
    HardwareConnectionConfig,
    HardwareProfile,
    SelectorConfig,
    VerificationStatus,
)
from app.views.hardware_settings_view import (
    HardwareSettingsSnapshot,
    HardwareSettingsView,
)


def _channel(port: int, *, status: VerificationStatus = VerificationStatus.PENDING):
    if port not in {2, 4}:
        return ChannelDescriptor(external_port=port)
    base = ChannelDescriptor(
        external_port=port,
        internal_valve=port,
        target=f"Dev1/P0.{port - 1}",
        active_high=port == 2,
        enabled=True,
        display_name="薄荷" if port == 2 else "柠檬",
    )
    if status is VerificationStatus.PENDING:
        return base
    return replace(
        base,
        verification=ChannelVerification(
            status=status,
            fingerprint=base.mapping_fingerprint,
            verified_at="2026-08-18",
        ),
    )


def _profile(
    *,
    status: VerificationStatus = VerificationStatus.MOCK_VERIFIED,
) -> HardwareProfile:
    return HardwareProfile(
        schema_version=1,
        profile_name="Mock 实验台",
        channels=tuple(
            _channel(port, status=status if port == 2 else VerificationStatus.PENDING) for port in range(1, 21)
        ),
        selector=SelectorConfig(target="Dev2/P1.0"),
        connections=HardwareConnectionConfig(
            serial_port="COM6",
            ni_device_ids=("Dev1", "Dev2"),
            alicat_a_unit_id="a",
            alicat_b_unit_id="b",
            alicat_c_unit_id="c",
        ),
    )


def test_settings_renders_20_port_candidate_and_natural_verification_text(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=7, can_save=True)

    assert len(view.name_inputs) == 20
    assert view.name_inputs[2].text() == "薄荷"
    assert view.internal_inputs[2].value() == 2
    assert view.target_inputs[2].text() == "Dev1/P0.1"
    assert view.polarity_inputs[2].currentData() is True
    assert view.enabled_checks[2].isChecked()
    assert view.verification_labels[2].text() == "模拟验证完成 · 2026-08-18"
    assert view.verification_labels[1].text() == "需要验证"
    assert view.serial_port_input.text() == "COM6"
    assert view.ni_device_ids_input.text() == "Dev1, Dev2"
    assert view.alicat_unit_inputs["A"].text() == "a"


def test_settings_default_profile_keeps_physical_two_by_ten_mapping(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=0, can_save=False, can_mock_verify=True)

    assert {
        port: view.internal_inputs[port].value()
        for port in (2, 4, 6, 8, 12, 14, 16, 18)
    } == {2: 2, 4: 3, 6: 4, 8: 5, 12: 6, 14: 7, 16: 8, 18: 9}
    for index, port in enumerate(range(1, 21)):
        row, column, _row_span, _column_span = view.overview_layout.getItemPosition(index)
        assert (row, column) == ((port - 1) // 10, (port - 1) % 10)
    view.select_port(4)
    assert view.editor_title.text() == "编辑气口 04"
    assert view.target_inputs[4].text() == "Dev1/P0.2"


def test_settings_long_alias_uses_shared_elision_and_only_truncated_tooltip(qtbot) -> None:
    raw = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    raw["hardware_profile"]["channels"][3]["display_name"] = (
        "这是一个需要在固定高度气口卡片中截断的很长别名"
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        HardwareProfile.from_config(raw), revision=0, can_save=False
    )
    tile = view.overview_buttons[4]
    tile.resize(72, tile.height())
    tile._refresh_elision()

    assert tile.title_label.text().endswith("…")
    assert tile.title_label.toolTip().startswith("这是一个需要")
    assert tile.port_label.text() == "气口 04"
    assert tile.height() == 82


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (VerificationStatus.PHYSICAL_VERIFIED, "已验证 · 2026-08-18"),
        (VerificationStatus.INCOMPLETE, "验证未完成"),
        (VerificationStatus.FAILED, "验证失败"),
    ),
)
def test_settings_uses_natural_text_for_each_verification_result(
    qtbot,
    status: VerificationStatus,
    expected: str,
) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(status=status), revision=1, can_save=True)
    assert view.verification_labels[2].text() == expected


def test_settings_candidate_signal_is_hardware_profile_and_name_keeps_verification(
    qtbot,
) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=3, can_save=True)
    candidates = []
    view.candidate_changed.connect(candidates.append)

    view.name_inputs[2].setText("新薄荷")

    assert isinstance(candidates[-1], HardwareProfile)
    channel = candidates[-1].registry.by_external_port(2)
    assert channel.display_name == "新薄荷"
    assert channel.verification.status is VerificationStatus.MOCK_VERIFIED
    assert view.verification_labels[2].text() == "模拟验证完成 · 2026-08-18"


def test_mapping_edit_requires_reverification_and_disables_verification_intent(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=4, can_save=True, can_mock_verify=True)
    candidates = []
    mock_requests = []
    view.candidate_changed.connect(candidates.append)
    view.mock_verify_requested.connect(
        lambda port, candidate, revision: mock_requests.append(
            (port, candidate, revision)
        )
    )

    view.internal_inputs[2].setValue(10)
    assert view.verification_labels[2].text() == "配置已变更，需要重新验证"
    assert candidates[-1].registry.by_external_port(2).verification.status is (VerificationStatus.MAPPING_CHANGED)

    view.mock_buttons[2].click()
    assert not view.mock_buttons[2].isEnabled()
    assert mock_requests == []


def test_settings_uses_overview_and_collapsed_advanced_editor(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=2, can_save=True)

    assert len(view.overview_buttons) == 20
    assert view.advanced_card.isExpand is False
    view.overview_buttons[2].click()
    assert view.editor_stack.currentIndex() == 1
    assert view.editor_title.text() == "编辑气口 02"
    view.advanced_toggle.toggleExpand()
    assert view.advanced_card.isExpand is True
    assert view.settings_scroll.widgetResizable()
    assert view.save_button.parentWidget() is view


def test_save_and_rollback_emit_candidate_with_expected_revision(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_snapshot(
        HardwareSettingsSnapshot(
            profile=_profile(),
            revision=11,
            can_edit=True,
            can_save=True,
            can_mock_verify=True,
            can_request_physical_verification=True,
            rollback_available=True,
            status_text="候选配置可编辑",
        )
    )
    saves = []
    rollbacks = []
    view.save_requested.connect(lambda candidate, revision: saves.append((candidate, revision)))
    view.rollback_requested.connect(rollbacks.append)

    view.save_button.click()
    view.rollback_button.click()

    assert isinstance(saves[0][0], HardwareProfile)
    assert saves[0][1] == 11
    assert rollbacks == [11]


def test_settings_readonly_state_blocks_channel_edits_and_keeps_connections_readonly(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=5, can_save=False, message="设备已连接")
    candidates = []
    rollbacks = []
    view.candidate_changed.connect(candidates.append)
    view.rollback_requested.connect(rollbacks.append)

    view.render_snapshot(
        HardwareSettingsSnapshot(
            profile=_profile(),
            revision=5,
            can_edit=False,
            can_save=False,
            can_mock_verify=False,
            can_request_physical_verification=False,
            rollback_available=True,
            status_text="设备已连接",
        )
    )

    assert not view.name_inputs[2].isEnabled()
    assert view.serial_port_input.isEnabled() and view.serial_port_input.isReadOnly()
    assert view.ni_device_ids_input.isEnabled() and view.ni_device_ids_input.isReadOnly()
    assert view.alicat_unit_inputs["A"].isEnabled()
    assert view.alicat_unit_inputs["A"].isReadOnly()
    assert not view.mock_buttons[2].isEnabled()
    assert not view.save_button.isEnabled()
    assert not view.rollback_button.isEnabled()
    view.rollback_button.click()
    assert candidates == []
    assert rollbacks == []


def test_settings_connections_are_readonly_and_emit_no_candidate(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=6, can_save=True)
    candidates = []
    view.candidate_changed.connect(candidates.append)

    view.serial_port_input.setText("com18")
    view.ni_device_ids_input.setText("RackA, RackB")
    view.alicat_unit_inputs["A"].setText("1")
    view.alicat_unit_inputs["B"].setText("2")
    view.alicat_unit_inputs["C"].setText("3")

    assert candidates == []
    assert view.serial_port_input.isReadOnly()
    assert view.ni_device_ids_input.isReadOnly()
    assert all(control.isReadOnly() for control in view.alicat_unit_inputs.values())


def test_permission_only_refresh_preserves_unsaved_draft(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=9, can_save=True, rollback_available=True)
    view.name_inputs[2].setText("未保存名称")
    before = view.draft

    view.render_permissions(
        can_save=False,
        message="已连接，暂停保存",
        rollback_available=True,
    )

    assert view.draft == before
    assert view.name_inputs[2].text() == "未保存名称"
    assert not view.save_button.isEnabled()
    assert not view.rollback_button.isEnabled()


def test_missing_preset_restores_previous_control_channel(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=9, can_save=True)
    before = view.draft
    view.internal_inputs[2].setValue(10)

    assert view.internal_inputs[2].value() == 2
    assert view.draft == before
    assert not view.save_button.isEnabled()


def test_normal_channel_edit_resolves_preset_and_blocks_duplicate_immediately(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=3, can_save=True, can_mock_verify=False)

    view.internal_inputs[4].setValue(10)
    assert view.target_inputs[4].text() == "Dev1/P1.1"
    assert view.draft.to_profile().registry.by_external_port(4).verification.status is (
        VerificationStatus.MAPPING_CHANGED
    )

    view.internal_inputs[6].setValue(10)
    assert "控制通道 10 已被气口 04 使用" in view.validation_label.text()
    assert not view.save_button.isEnabled()


def test_advanced_mapping_and_rollback_are_not_exposed_as_normal_controls(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=1, can_save=True)

    assert view.target_inputs[2].isReadOnly()
    assert not view.polarity_inputs[2].isEnabled()
    assert view.rollback_button.isHidden()
    assert view.preset_target_labels[20].text() == "Dev2/P0.7"


def test_clean_revert_restores_verification_permission(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=3, can_save=True, can_mock_verify=True)

    view.name_inputs[2].setText("dirty")
    assert not view.mock_buttons[2].isEnabled()
    view.name_inputs[2].setText(_profile().registry.by_external_port(2).display_name)

    assert view.mock_buttons[2].isEnabled()


def test_physical_request_routes_directly_without_mock_confirmation(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        _profile(),
        revision=4,
        can_save=False,
        can_mock_verify=False,
        can_request_physical_verification=True,
    )
    physical = []
    mock = []
    view.physical_verify_requested.connect(physical.append)
    view.mock_verify_requested.connect(lambda *args: mock.append(args))

    view.mock_buttons[2].click()

    assert physical == [2]
    assert mock == []


@pytest.mark.parametrize(
    ("status", "level"),
    (
        (VerificationStatus.PENDING, InfoLevel.INFOAMTION),
        (VerificationStatus.MOCK_VERIFIED, InfoLevel.SUCCESS),
        (VerificationStatus.PHYSICAL_VERIFIED, InfoLevel.SUCCESS),
        (VerificationStatus.INCOMPLETE, InfoLevel.WARNING),
        (VerificationStatus.FAILED, InfoLevel.ERROR),
    ),
)
def test_verification_badge_level_matches_status(qtbot, status, level) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(status=status), revision=1, can_save=True)

    assert view.verification_labels[2].level == level


def test_settings_tiles_distinguish_disabled_but_remain_selectable(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=1, can_save=True)

    disabled = view.overview_buttons[1]
    enabled = view.overview_buttons[2]
    assert disabled.isEnabled() and enabled.isEnabled()
    assert disabled._normalBackgroundColor() != enabled._normalBackgroundColor()
    disabled.click()
    assert view.editor_title.text().endswith("01")
