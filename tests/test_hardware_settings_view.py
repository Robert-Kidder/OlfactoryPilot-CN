from __future__ import annotations

from dataclasses import replace

import pytest

from app.models import (
    ChannelDescriptor,
    ChannelVerification,
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
            _channel(port, status=status if port == 2 else VerificationStatus.PENDING)
            for port in range(1, 21)
        ),
        selector=SelectorConfig(target="Dev2/P1.0"),
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
    assert view.verification_labels[2].text() == "Mock 验证通过 · 2026-08-18"
    assert view.verification_labels[1].text() == "待验证"


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (VerificationStatus.PHYSICAL_VERIFIED, "物理验证通过 · 2026-08-18"),
        (VerificationStatus.INCOMPLETE, "验证未完成"),
        (VerificationStatus.FAILED, "验证失败，请检查接线"),
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
    assert view.verification_labels[2].text() == "Mock 验证通过 · 2026-08-18"


def test_mapping_edit_requires_reverification_and_mock_is_only_an_intent(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=4, can_save=True)
    candidates = []
    mock_requests = []
    view.candidate_changed.connect(candidates.append)
    view.mock_verify_requested.connect(
        lambda port, candidate: mock_requests.append((port, candidate))
    )

    view.target_inputs[2].setText("Dev2/P0.2")
    assert view.verification_labels[2].text() == "配置已变更，请重新验证"
    assert candidates[-1].registry.by_external_port(2).verification.status is (
        VerificationStatus.MAPPING_CHANGED
    )

    view.mock_buttons[2].click()
    assert mock_requests[0][0] == 2
    assert isinstance(mock_requests[0][1], HardwareProfile)
    assert mock_requests[0][1].registry.by_external_port(2).verification.status is (
        VerificationStatus.MAPPING_CHANGED
    )


def test_physical_verification_only_requests_authorization(qtbot) -> None:
    view = HardwareSettingsView()
    qtbot.addWidget(view)
    view.render_profile(_profile(), revision=2, can_save=True)
    requests = []
    mock_requests = []
    view.physical_verify_requested.connect(requests.append)
    view.mock_verify_requested.connect(lambda *args: mock_requests.append(args))

    assert "只提交授权申请" in view.physical_buttons[2].toolTip()
    assert "不会直接操作" in view.physical_buttons[2].toolTip()
    view.physical_buttons[2].click()
    assert requests == [2]
    assert mock_requests == []


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
    view.save_requested.connect(
        lambda candidate, revision: saves.append((candidate, revision))
    )
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
    physical = []
    view.candidate_changed.connect(candidates.append)
    view.physical_verify_requested.connect(physical.append)

    assert not view.name_inputs[2].isEnabled()
    assert not view.mock_buttons[2].isEnabled()
    assert not view.physical_buttons[2].isEnabled()
    assert not view.save_button.isEnabled()
    view.physical_buttons[2].click()
    assert candidates == []
    assert physical == []
