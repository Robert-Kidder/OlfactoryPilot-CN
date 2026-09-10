from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QStackedWidget
from qfluentwidgets import ComboBox, HeaderCardWidget, InfoLevel, Pivot, TableWidget

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
    HardwareConnectionConfig,
    HardwareProfile,
    HardwareVerificationPhase,
    HardwareVerificationSnapshot,
    SelectorConfig,
    VerificationConfig,
    VerificationStatus,
)
from app.views.hardware_settings_view import (
    HardwareSettingsSnapshot,
    HardwareSettingsView,
    SettingsPortTile,
)
from app.views.manual_experiment_view import PortTile
from app.views.spin_box_rules import ProductNumericSpinBox


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
    details = (
        {
            "run_identity": "physical-run-1",
            "profile_revision": 0,
            "ni_target": base.target,
            "flow_setpoint_sccm": 1500,
            "flow_readback_sccm": 1499,
            "open_command_id": "physical-run-1:open",
            "close_command_id": "physical-run-1:close",
            "opened_at_ns": 100,
            "closed_at_ns": 200,
            "action_completed": True,
            "safe_closed": True,
            "authorized": True,
            "user_confirmed": True,
        }
        if status is VerificationStatus.PHYSICAL_VERIFIED
        else {}
    )
    return replace(
        base,
        verification=ChannelVerification(
            status=status,
            fingerprint=base.mapping_fingerprint,
            verified_at="2026-08-18",
            **details,
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
    assert view.current_channel_labels[2].text() == "02"
    assert view.target_inputs[2].text() == "Dev1/P0.1"
    assert view.polarity_inputs[2].isChecked()
    assert view.enabled_checks[2].isChecked()
    assert view.verification_labels[2].text() == "待现场确认"
    assert view.verification_labels[1].text() == "未启用"
    assert "2026-08-18" not in view.verification_labels[2].text()
    assert view.serial_port_input.text() == "COM6"
    assert view.ni_device_ids_input.text() == "Dev1, Dev2"
    assert view.alicat_unit_inputs["A"].text() == "a"


def test_verification_parameters_use_domain_ranges_and_keep_validation_feedback(
    qtbot,
) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    candidates = []
    view.candidate_changed.connect(candidates.append)
    view.render_profile(
        _profile(),
        revision=1,
        can_save=True,
        can_mock_verify=True,
        can_request_physical_verification=True,
    )

    assert isinstance(view.verification_flow_input, ProductNumericSpinBox)
    assert isinstance(view.verification_duration_input, ProductNumericSpinBox)
    assert view.verification_flow_input.minimum() == 0
    assert view.verification_flow_input.maximum() == 1500
    assert view.verification_duration_input.minimum() == 1
    assert view.verification_duration_input.maximum() == 60

    view.verification_flow_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_flow_input.lineEdit(), "2000")
    QTest.keyClick(view.verification_flow_input.lineEdit(), Qt.Key.Key_Return)
    assert view.verification_flow_input.value() == 1500
    assert view._draft.verification_config.flow_sccm == 1500
    assert not view.save_button.isEnabled()
    assert all(not button.isEnabled() for button in view.mock_buttons.values())
    assert "现场证据支持范围" in view.status_label.text()

    view.verification_flow_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_flow_input.lineEdit(), "1400")
    QTest.keyClick(view.verification_flow_input.lineEdit(), Qt.Key.Key_Return)
    assert view._draft.verification_config.flow_sccm == 1400

    view.verification_duration_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_duration_input.lineEdit(), "61")
    QTest.keyClick(view.verification_duration_input.lineEdit(), Qt.Key.Key_Return)
    assert view.verification_duration_input.value() == 20
    assert view._draft.verification_config.duration_s == 20
    assert "1–60 秒" in view.status_label.text()

    view.verification_duration_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_duration_input.lineEdit(), "30")
    QTest.keyClick(view.verification_duration_input.lineEdit(), Qt.Key.Key_Return)

    assert candidates[-1].verification_config.flow_sccm == 1400
    assert candidates[-1].verification_config.duration_s == 30
    assert view.mock_buttons[2].isEnabled()


def test_verification_flow_range_uses_smaller_profile_and_approved_limit(qtbot) -> None:
    small_ceiling = 0.000000000123456789
    profile = replace(
        _profile(),
        max_sample_a_sccm=small_ceiling,
        verification_config=VerificationConfig(
            flow_sccm=small_ceiling / 2,
            duration_s=20,
            max_approved_flow_sccm=small_ceiling * 2,
        ),
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)

    view.render_profile(profile, revision=1, can_save=True)

    assert view.verification_flow_input.maximum() == small_ceiling
    assert view.verification_flow_input.value() == small_ceiling / 2


def test_verification_parameters_share_steps_without_quantizing_keyboard_input(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=1, can_save=True)

    assert view.verification_flow_input.singleStep() == 100
    assert view.verification_duration_input.singleStep() == 5
    view.verification_flow_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_flow_input.lineEdit(), "550.25")
    QTest.keyClick(view.verification_flow_input.lineEdit(), Qt.Key.Key_Return)
    assert view.verification_flow_input.value() == 550.25
    view.verification_flow_input.stepUp()
    assert view.verification_flow_input.value() == 600
    assert view.draft.verification_config.flow_sccm == 600
    view.verification_duration_input.lineEdit().selectAll()
    QTest.keyClicks(view.verification_duration_input.lineEdit(), "7.5")
    QTest.keyClick(view.verification_duration_input.lineEdit(), Qt.Key.Key_Return)
    assert view.verification_duration_input.value() == 7.5
    assert view.draft.verification_config.duration_s == 7.5
    view.verification_duration_input.stepUp()
    assert view.verification_duration_input.value() == 10


def test_basic_port_details_hide_polarity_and_verification_explanation(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=1, can_save=True)

    basic_text = {
        label.text()
        for label in view.editor_stack.currentWidget().findChildren(QLabel)
    }
    assert basic_text.isdisjoint({"开启方式", "高电平开启", "低电平开启"})
    settings_text = {
        label.text() for label in view.settings_scroll.findChildren(QLabel)
    }
    assert "每次现场启动前会再次显示气口、流量和时间。" not in settings_text

    view.open_hardware_settings()
    assert view.page_stack.currentWidget() is view.hardware_section
    advanced_text = {
        label.text()
        for label in view.advanced_stack.currentWidget().findChildren(QLabel)
    }
    assert "开启方式" in advanced_text
    assert view.polarity_inputs[2].isChecked()
    view.edit_lines_button.click()
    assert view.polarity_inputs[2].isEnabled()


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
    assert view.editor_card.headerLabel.text() == "气口 04"
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

    assert tile.title_label.text() == "气口 04"
    assert tile.port_label.text().endswith("…")
    assert tile.title_label.toolTip().startswith("这是一个需要")
    assert tile.height() == 82


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (VerificationStatus.PHYSICAL_VERIFIED, "可用"),
        (VerificationStatus.INCOMPLETE, "待验证"),
        (VerificationStatus.FAILED, "需检查"),
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
    assert view.verification_labels[2].text() == "待现场确认"


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
    assert view.verification_labels[2].text() == "待验证"
    assert view.verification_hints[2].text() == "保存后验证"
    assert candidates[-1].registry.by_external_port(2).verification.status is (VerificationStatus.MAPPING_CHANGED)

    view.mock_buttons[2].click()
    assert not view.mock_buttons[2].isEnabled()
    assert mock_requests == []


def test_settings_uses_two_section_information_architecture(qtbot, qt_app) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=2, can_save=True)
    view.show()
    qt_app.processEvents()

    assert len(view.overview_buttons) == 20
    assert isinstance(view.section_pivot, Pivot)
    assert isinstance(view.section_stack, QStackedWidget)
    assert view.section_stack.currentWidget() is view.settings_home
    view.open_port_settings()
    assert view.section_stack.currentWidget() is view.port_section
    assert not view.preset_table.isVisibleTo(view)
    view.overview_buttons[2].click()
    assert view.editor_stack.currentIndex() == 1
    assert view.editor_card.headerLabel.text() == "气口 02"
    assert isinstance(view.overview_card, HeaderCardWidget)
    assert isinstance(view.editor_card, HeaderCardWidget)
    view.open_hardware_settings()
    assert view.section_stack.currentWidget() is view.hardware_section
    assert view.preset_table.isVisibleTo(view)
    assert view.settings_scroll.widgetResizable()
    assert view.save_button.isVisibleTo(view) is True


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

    view.name_inputs[2].setText("新薄荷")
    view.save_button.click()
    view.rollback_button.click()

    assert isinstance(saves[0][0], HardwareProfile)
    assert saves[0][1] == 11
    assert rollbacks == [11]


def test_settings_readonly_state_blocks_channel_and_connection_edits(qtbot) -> None:
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
    assert not view.serial_port_input.isEnabled()
    assert not view.ni_device_ids_input.isEnabled()
    assert not view.alicat_unit_inputs["A"].isEnabled()
    assert isinstance(view.serial_port_input, QLineEdit)
    assert isinstance(view.ni_device_ids_input, QLineEdit)
    assert isinstance(view.alicat_unit_inputs["A"], QLineEdit)
    assert not view.mock_buttons[2].isEnabled()
    assert not view.save_button.isEnabled()
    assert not view.rollback_button.isEnabled()
    view.rollback_button.click()
    assert candidates == []
    assert rollbacks == []


def test_settings_connections_are_editable_values_in_two_bounded_columns(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=6, can_save=True)
    candidates = []
    view.candidate_changed.connect(candidates.append)

    assert candidates == []
    assert view.serial_port_input.text() == "COM6"
    assert view.ni_device_ids_input.text() == "Dev1, Dev2"
    assert all(isinstance(control, QLineEdit) for control in view.alicat_unit_inputs.values())
    assert view.serial_port_input.maximumWidth() == 320
    assert view.ni_device_ids_input.maximumWidth() == 420
    assert len(view.connection_cards) == 2
    assert all(card.maximumWidth() == 420 for card in view.connection_cards)

    candidates = []
    saves = []
    view.candidate_changed.connect(candidates.append)
    view.save_requested.connect(lambda profile, revision: saves.append((profile, revision)))
    view.profile_name_input.setText("现场连接方案")
    view.serial_port_input.setText("COM12")
    view.ni_device_ids_input.setText("DevA, DevB")
    view.alicat_unit_inputs["A"].setText("x")
    view.alicat_unit_inputs["B"].setText("y")
    view.alicat_unit_inputs["C"].setText("z")

    candidate = candidates[-1]
    assert candidate.profile_name == "现场连接方案"
    assert candidate.connections.serial_port == "COM12"
    assert candidate.connections.ni_device_ids == ("DevA", "DevB")
    assert candidate.connections.alicat_unit_ids == {"A": "x", "B": "y", "C": "z"}
    view._request_save()
    assert saves == [(candidate, 6)]


def test_settings_scroll_surfaces_reveal_the_product_page_background(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)

    surfaces = (
        view.settings_scroll,
        view.settings_scroll.viewport(),
        view.settings_scroll.widget(),
        view.hardware_scroll,
        view.hardware_scroll.viewport(),
        view.hardware_scroll.widget(),
    )
    assert all("background: transparent" in surface.styleSheet() for surface in surfaces)


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
    occupied_index = view.internal_inputs[6].findData(10)
    assert not view.internal_inputs[6].items[occupied_index].isEnabled
    assert "已用于气口 04" in view.internal_inputs[6].items[occupied_index].text

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

    assert not isinstance(view.target_inputs[2], QLineEdit)
    assert not isinstance(view.polarity_inputs[2], ComboBox)
    assert view.rollback_button.isHidden()
    assert isinstance(view.preset_table, TableWidget)
    assert view.preset_table.rowCount() == 10
    assert view.preset_table.columnCount() == 4
    assert all(field.isHidden() for field in view.preset_target_inputs.values())
    assert all(not label.isHidden() for label in view.preset_target_labels.values())
    view.edit_lines_button.click()
    assert all(not field.isHidden() for field in view.preset_target_inputs.values())
    assert all(label.isHidden() for label in view.preset_target_labels.values())
    assert view.preset_target_inputs[20].text() == "Dev2/P0.7"


def test_current_line_control_channel_tracks_selection_and_draft(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=1, can_save=True)

    view.select_port(4)
    assert view.advanced_stack.currentIndex() == 3
    assert view.current_channel_labels[4].text() == "03"
    view.internal_inputs[4].setValue(10)
    assert view.current_channel_labels[4].text() == "10"
    assert view.target_inputs[4].text() == "Dev1/P1.1"
    assert view.current_channel_labels[2].text() == "02"


def test_line_edit_updates_bound_descriptor_and_preserves_other_custom_target(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    channels = list(profile.channels)
    channels[1] = replace(channels[1], target="Dev2/P1.2")
    profile = replace(profile, channels=tuple(channels))
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=1, can_save=True)
    view.edit_lines_button.click()

    view.preset_target_inputs[3].setText("Dev2/P1.1")

    candidate = view.draft.to_profile()
    assert candidate.registry.by_external_port(4).target == "Dev2/P1.1"
    assert candidate.registry.by_external_port(2).target == "Dev2/P1.2"
    assert candidate.target_preset.target_for(3) == "Dev2/P1.1"


def test_invalid_line_text_cannot_save_a_different_hidden_target(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=1, can_save=True)
    view.edit_lines_button.click()
    saves = []
    view.save_requested.connect(lambda candidate, revision: saves.append((candidate, revision)))

    view.preset_target_inputs[3].clear()
    view.name_inputs[4].setText("仍不能保存")

    assert 3 in view._invalid_preset_targets
    assert not view.save_button.isEnabled()
    assert view._validate_draft() is None
    view._request_save()
    assert saves == []


def test_reverting_mapping_field_restores_original_verified_state(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        _profile(status=VerificationStatus.PHYSICAL_VERIFIED),
        revision=3,
        can_save=True,
        can_mock_verify=True,
    )

    assert not view.polarity_inputs[2].isEnabled()
    view.edit_lines_button.click()
    assert view.polarity_inputs[2].isEnabled()
    view.polarity_inputs[2].setChecked(False)
    assert view.draft.channels[1].mapping_changed
    assert view.overview_buttons[2].property("portState") == "pending"
    view.polarity_inputs[2].setChecked(True)

    assert not view.draft.channels[1].mapping_changed
    assert view.overview_buttons[2].property("portState") == "physical"
    assert view.mock_buttons[2].isEnabled()


def test_connected_snapshot_disables_advanced_target_and_polarity(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=3, can_save=True)
    view.edit_lines_button.click()
    assert view.polarity_inputs[2].isEnabled()
    assert not view.preset_target_inputs[2].isHidden()

    view.render_permissions(can_save=False)

    assert not view.edit_lines_button.isChecked()
    assert not view.polarity_inputs[2].isEnabled()
    assert view.preset_target_inputs[2].isHidden()


def test_clean_revert_restores_verification_permission(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=3, can_save=True, can_mock_verify=True)

    view.name_inputs[2].setText("dirty")
    assert not view.mock_buttons[2].isEnabled()
    view.name_inputs[2].setText(_profile().registry.by_external_port(2).display_name)

    assert view.mock_buttons[2].isEnabled()


@pytest.mark.parametrize("field", ("alias", "enabled"))
def test_any_dirty_saved_field_shows_save_before_verification_hint(qtbot, field) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=3, can_save=True, can_mock_verify=True)

    if field == "alias":
        view.name_inputs[2].setText("新别名")
    else:
        view.enabled_checks[2].setChecked(False)

    assert not view.mock_buttons[2].isEnabled()
    assert view.verification_hints[2].text() == "保存后验证"
    assert not view.verification_hints[2].isHidden()


def test_verification_progress_is_owned_by_selected_port_editor(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        _profile(),
        revision=3,
        can_save=False,
        can_mock_verify=True,
        verification=HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=2,
            started_ns=1,
            deadline_ns=20_000_000_001,
            duration_s=20,
            can_stop=True,
            awaiting_user_confirmation=False,
            run_identity="run-2",
            revision=3,
            fingerprint=_profile().registry.by_external_port(2).mapping_fingerprint,
        ),
    )

    assert view.editor_stack.parentWidget() is not None
    assert view.verification_progress.parentWidget() is view.verification_panel
    assert not view.verification_panel.isHidden()
    assert not view.verification_progress.isHidden()
    assert not view.verification_stop_button.isHidden()
    assert view.verification_task_title.text() == "正在验证气口 02"
    assert view.verification_task_detail.text() == "请确认气口 02 是否有气流"
    assert view.verification_stop_button.text() == "立即停止"
    assert view.editor_stack.isHidden()
    assert all(not control.isEnabled() for control in view.name_inputs.values())
    assert all(not control.isEnabled() for control in view.internal_inputs.values())
    assert all(not control.isEnabled() for control in view.enabled_checks.values())
    assert not view.section_pivot.isEnabled()
    assert all(not tile.isEnabled() for tile in view.overview_buttons.values())
    view.open_hardware_settings()
    view.overview_buttons[4].click()
    view.select_port(4)
    assert view.section_stack.currentWidget() is view.port_section
    assert view.editor_stack.currentIndex() == 1
    assert not view.save_button.isEnabled()
    assert view._validate_draft() is not None
    assert not view.save_button.isEnabled()
    assert view.status_label.isHidden()

    view.render_profile(_profile(), revision=3, can_save=True, can_mock_verify=True)
    assert view.verification_panel.isHidden()
    assert not view.editor_stack.isHidden()
    assert view.editor_card.headerLabel.text() == "气口 02"
    assert view.section_pivot.isEnabled()
    assert all(tile.isEnabled() for tile in view.overview_buttons.values())


def test_preparing_verification_shows_only_preparation_and_stop(qtbot) -> None:
    profile = _profile()
    channel = profile.registry.by_external_port(2)
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        profile,
        revision=3,
        can_save=False,
        verification=HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.PREPARING,
            external_port=2,
            duration_s=20,
            can_stop=True,
            awaiting_user_confirmation=False,
            run_identity="preparing-run",
            revision=3,
            fingerprint=channel.mapping_fingerprint,
        ),
    )

    assert not view.verification_stop_button.isHidden()
    assert view.verification_negative_button.isHidden()
    assert view.verification_positive_button.isHidden()
    assert view.verification_task_title.text() == "正在准备验证气口 02"
    assert "正在确认全关" in view.verification_task_detail.text()
    assert view.verification_remaining_label.text() == "准备中"


def test_awaiting_confirmation_only_offers_result_actions(qtbot) -> None:
    profile = _profile()
    channel = profile.registry.by_external_port(2)
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        profile,
        revision=3,
        can_save=False,
        verification=HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.AWAITING_CONFIRMATION,
            external_port=2,
            started_ns=1,
            deadline_ns=30_000_000_001,
            duration_s=30,
            can_stop=False,
            awaiting_user_confirmation=True,
            run_identity="run-2",
            revision=3,
            fingerprint=channel.mapping_fingerprint,
        ),
    )

    assert view.verification_stop_button.isHidden()
    assert not view.verification_negative_button.isHidden()
    assert not view.verification_positive_button.isHidden()
    assert view.verification_task_title.text() == "气口 02 是否正确出气？"
    assert view.verification_task_detail.text() == "请选择刚才观察到的结果"
    assert view.verification_remaining_label.text() == "请选择检查结果"
    assert "0 秒内" not in view.verification_remaining_label.text()


def test_countdown_uses_one_deadline_and_never_resets_on_status_refresh(qtbot) -> None:
    now = [1_000_000_000]
    profile = _profile()
    channel = profile.registry.by_external_port(2)
    verification = HardwareVerificationSnapshot(
        phase=HardwareVerificationPhase.RUNNING,
        external_port=2,
        started_ns=now[0],
        deadline_ns=now[0] + 20_000_000_000,
        duration_s=20,
        can_stop=True,
        awaiting_user_confirmation=False,
        run_identity="countdown-run",
        revision=3,
        fingerprint=channel.mapping_fingerprint,
    )
    view = HardwareSettingsView(monotonic_ns=lambda: now[0])
    qtbot.addWidget(view)
    values = []

    for second in range(20):
        now[0] = 1_000_000_000 + second * 1_000_000_000
        view.render_profile(
            profile,
            revision=3,
            can_save=False,
            message="无关状态刷新",
            verification=verification,
        )
        values.append(int(view.verification_remaining_label.text().split()[1]))

    assert values == list(range(20, 0, -1))
    now[0] = verification.deadline_ns
    view.render_profile(
        profile,
        revision=3,
        can_save=False,
        message="到达截止时间",
        verification=verification,
    )
    assert view.verification_remaining_label.text() == "正在安全收口"
    assert view.verification_progress.value() == 100


@pytest.mark.parametrize(
    ("remaining_ns", "expected"),
    (
        (4_200_000_000, "剩余 5 秒"),
        (4_000_000_000, "剩余 4 秒"),
        (200_000_000, "剩余 1 秒"),
    ),
)
def test_verification_countdown_rounds_fractional_seconds_up(
    qtbot, remaining_ns, expected
) -> None:
    profile = _profile()
    channel = profile.registry.by_external_port(2)
    deadline_ns = 30_000_000_000
    view = HardwareSettingsView(
        monotonic_ns=lambda: deadline_ns - remaining_ns
    )
    qtbot.addWidget(view)

    view.render_profile(
        profile,
        revision=3,
        can_save=False,
        verification=HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=2,
            started_ns=10_000_000_000,
            deadline_ns=deadline_ns,
            duration_s=20,
            can_stop=True,
            run_identity="fractional-countdown",
            revision=3,
            fingerprint=channel.mapping_fingerprint,
        ),
    )

    assert view.verification_remaining_label.text() == expected


@pytest.mark.parametrize("external_port", (None, 0, 21))
def test_verification_snapshot_rejects_missing_or_out_of_range_port(
    external_port,
) -> None:
    with pytest.raises(ValueError, match="1–20"):
        HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=external_port,
            started_ns=1,
            deadline_ns=2,
            duration_s=1,
            can_stop=True,
            awaiting_user_confirmation=False,
            run_identity="invalid-port",
            fingerprint="0" * 64,
        )


def test_verification_snapshot_rejects_inconsistent_timing_and_result_phase() -> None:
    with pytest.raises(ValueError, match="时长"):
        HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=2,
            started_ns=1,
            deadline_ns=2,
            duration_s=1,
            can_stop=True,
            run_identity="bad-timing",
            fingerprint="0" * 64,
        )
    with pytest.raises(ValueError, match="完成阶段"):
        HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.FINISHED,
            external_port=2,
            duration_s=20,
            run_identity="missing-result",
            fingerprint="0" * 64,
        )
    with pytest.raises(ValueError, match="完成阶段"):
        HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.IDLE,
            result=VerificationStatus.FAILED,
        )


def test_validate_draft_never_reenables_save_in_progress(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_snapshot(
        HardwareSettingsSnapshot(
            profile=_profile(),
            can_edit=True,
            can_save=True,
            save_in_progress=True,
        )
    )

    assert view._validate_draft() is not None
    assert not view.save_button.isEnabled()


def test_save_in_progress_closes_line_edit_mode(qtbot) -> None:
    profile = HardwareProfile.from_config(
        json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    )
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(profile, revision=1, can_save=True)
    view.edit_lines_button.click()
    assert any(not field.isHidden() for field in view.preset_target_inputs.values())

    view.render_snapshot(
        HardwareSettingsSnapshot(
            profile=profile,
            revision=1,
            can_edit=True,
            can_save=True,
            save_in_progress=True,
        )
    )

    assert not view.edit_lines_button.isChecked()
    assert all(field.isHidden() for field in view.preset_target_inputs.values())


@pytest.mark.parametrize(
    ("message", "save_feedback", "verification_feedback"),
    (
            ("设置已保存。", "设置已保存", ""),
            ("设置已保存，重启后生效。", "设置已保存，重启后生效", ""),
        ("验证未完成：已立即停止，结果已保存；映射保持不变。", "", "验证已停止"),
        ("现场验证未开放：气口 02 未执行任何动作。", "", "现场验证暂不可用"),
        ("检查已结束；此气口仍待现场验证。", "", ""),
    ),
)
def test_settings_shows_only_contextual_action_feedback(
    qtbot, message, save_feedback, verification_feedback
) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(
        _profile(), revision=1, can_save=True, can_mock_verify=True, message=message
    )
    view.select_port(2)
    view._apply_permissions(view._snapshot)

    assert view.save_feedback_label.text() == save_feedback
    assert view.save_feedback_label.isHidden() is (not save_feedback)
    assert view.verification_hints[2].text() == verification_feedback
    assert view.verification_hints[2].isHidden() is (not verification_feedback)


def test_new_save_feedback_restarts_its_single_timer(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    profile = _profile()
    view.render_profile(
        profile,
        revision=1,
        can_save=True,
        message="设置已保存。",
    )
    QTest.qWait(30)
    previous_remaining = view._save_feedback_timer.remainingTime()

    view.render_profile(
        profile,
        revision=2,
        can_save=True,
        message="设置已保存。",
    )

    assert view._save_feedback_timer.isActive()
    assert view._save_feedback_timer.remainingTime() > previous_remaining


def test_physical_request_confirms_port_flow_and_maximum_time(qtbot, monkeypatch) -> None:
    captured = {}

    class _Button:
        def __init__(self) -> None:
            self.text = ""

        def setText(self, value: str) -> None:
            self.text = value

    class _Dialog:
        def __init__(self, title, content, parent) -> None:
            captured.update(title=title, content=content, parent=parent)
            self.yesButton = _Button()
            self.cancelButton = _Button()
            captured.update(yes=self.yesButton, cancel=self.cancelButton)

        def exec(self) -> bool:
            return True

    monkeypatch.setattr("app.views.hardware_settings_view.MessageBox", _Dialog)
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

    view.show()
    view.mock_buttons[2].click()

    assert physical == [2]
    assert mock == []
    assert captured["title"] == "验证气口 02"
    assert captured["content"] == (
        "开始后，请确认气口 02 是否出气。\n\n"
        "验证流量：1500 ml/min\n最长时间：20 秒"
    )
    assert captured["yes"].text == "开始验证"
    assert captured["cancel"].text == "取消"
    assert all(
        term not in captured["content"]
        for term in ("当前映射", "2500", "不能执行", "Mock")
    )


@pytest.mark.parametrize(
    ("status", "level"),
    (
        (VerificationStatus.PENDING, InfoLevel.WARNING),
        (VerificationStatus.MOCK_VERIFIED, InfoLevel.WARNING),
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
    assert view.editor_card.headerLabel.text().endswith("01")


def test_settings_tile_preserves_accessible_selection_and_keyboard_activation(
    qtbot,
) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=1, can_save=True)
    tile = view.overview_buttons[2]
    geometry = tile.size()
    activations = []
    tile.clicked.connect(lambda: activations.append(tile.external_port))

    view.select_port(2)
    assert tile.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert tile.accessibleName() == "气口 02 薄荷"
    assert "已选择" in tile.accessibleDescription()
    tile.setFocus()
    QTest.keyClick(tile, Qt.Key.Key_Return)
    QTest.keyClick(tile, Qt.Key.Key_Space)

    assert activations == [2, 2]
    assert tile.size() == geometry


def test_settings_visible_copy_excludes_internal_and_removed_terms(qtbot, qt_app) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=1, can_save=True)
    view.show()
    qt_app.processEvents()

    def visible_text() -> str:
        return "\n".join(
            widget.text()
            for widget in (
                view.findChildren(QLabel) + view.findChildren(QAbstractButton)
            )
            if widget.isVisibleTo(view)
        )

    home_text = visible_text()
    assert "气口配置" in home_text
    assert "线路与设备" in home_text
    view.open_port_settings()
    qt_app.processEvents()
    ports_text = visible_text()
    assert "气口总览" in ports_text
    assert "保存设置" in ports_text
    assert "控制通道表" not in ports_text

    view.open_hardware_settings()
    hardware_text = visible_text()
    assert "控制线路" in hardware_text
    assert "设备连接" in hardware_text
    combined = f"{ports_text}\n{hardware_text}"
    assert all(
        term not in combined
        for term in (
            _profile().profile_name,
            "固定 2×10 布局",
            "别名、控制通道和启用状态",
            "标准通道预设",
            "编辑气口",
        )
    )


def test_settings_owns_five_state_tiles_and_selection_does_not_replace_status(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)

    for status, expected in (
        (VerificationStatus.PENDING, "pending"),
            (VerificationStatus.MOCK_VERIFIED, "mock"),
        (VerificationStatus.PHYSICAL_VERIFIED, "physical"),
        (VerificationStatus.FAILED, "failed"),
    ):
        view.render_profile(_profile(status=status), revision=1, can_save=True)
        tile = view.overview_buttons[2]
        assert isinstance(tile, SettingsPortTile)
        assert not isinstance(tile, PortTile)
        assert tile.property("portState") == expected
        view.select_port(2)
        assert tile.isChecked()
        assert tile.property("portState") == expected

    assert view.overview_buttons[1].property("portState") == "unused"
    assert view.overview_buttons[1].status_text == "未启用"
