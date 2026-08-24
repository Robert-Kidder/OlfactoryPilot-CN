from __future__ import annotations

from dataclasses import replace

import pytest

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
    assert view.verification_labels[2].text() == "测试通过 · 2026-08-18"
    assert view.verification_labels[1].text() == "待测试"
    assert view.serial_port_input.text() == "COM6"
    assert view.ni_device_ids_input.text() == "Dev1, Dev2"
    assert view.alicat_unit_inputs["A"].text() == "a"


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (VerificationStatus.PHYSICAL_VERIFIED, "测试通过 · 2026-08-18"),
        (VerificationStatus.INCOMPLETE, "测试未完成"),
        (VerificationStatus.FAILED, "测试失败，请检查线路"),
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
    assert view.verification_labels[2].text() == "测试通过 · 2026-08-18"


def test_mapping_edit_requires_reverification_and_mock_is_only_an_intent(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=4, can_save=True)
    candidates = []
    mock_requests = []
    view.candidate_changed.connect(candidates.append)
    view.mock_verify_requested.connect(lambda port, candidate: mock_requests.append((port, candidate)))

    view.target_inputs[2].setText("Dev2/P0.2")
    assert view.verification_labels[2].text() == "配置已变更，请重新测试"
    assert candidates[-1].registry.by_external_port(2).verification.status is (VerificationStatus.MAPPING_CHANGED)

    view.mock_buttons[2].click()
    assert mock_requests[0][0] == 2
    assert isinstance(mock_requests[0][1], HardwareProfile)
    assert mock_requests[0][1].registry.by_external_port(2).verification.status is (VerificationStatus.MAPPING_CHANGED)


def test_settings_uses_overview_and_collapsed_advanced_editor(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=2, can_save=True)

    assert len(view.overview_buttons) == 20
    assert view.advanced_panel.isHidden()
    view.overview_buttons[2].click()
    assert view.editor_stack.currentIndex() == 1
    assert view.editor_title.text() == "编辑气口 2"
    view.advanced_toggle.click()
    assert not view.advanced_panel.isHidden()
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


def test_settings_readonly_state_blocks_all_verification_and_edits(qtbot) -> None:
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
    assert not view.mock_buttons[2].isEnabled()
    assert not view.save_button.isEnabled()
    assert not view.rollback_button.isEnabled()
    view.rollback_button.click()
    assert candidates == []
    assert rollbacks == []


def test_settings_edits_connections_in_same_frozen_candidate_without_probe(qtbot) -> None:
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

    candidate = candidates[-1]
    assert isinstance(candidate, HardwareProfile)
    assert candidate.connections.serial_port == "COM18"
    assert candidate.connections.ni_device_ids == ("RackA", "RackB")
    assert candidate.connections.alicat_unit_ids == {"A": "1", "B": "2", "C": "3"}
    assert view.draft is not None
    assert view.draft.revision == 6


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


def test_settings_invalid_connection_stays_draft_and_blocks_save_intent(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=9, can_save=True)
    candidates = []
    saves = []
    view.candidate_changed.connect(candidates.append)
    view.save_requested.connect(lambda *args: saves.append(args))

    view.serial_port_input.setText("ttyUSB0")
    view.save_button.click()

    assert candidates == []
    assert saves == []
    assert "COM1–COM256" in view.validation_label.text()
