from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qfluentwidgets import MessageBox

from app.main import DEFAULT_CONFIG, build_application
from app.models import (
    ChannelVerification,
    HardwareVerificationPhase,
    HardwareVerificationSnapshot,
    VerificationStatus,
)


def _evidence(channel, status: VerificationStatus) -> ChannelVerification:
    fingerprint = channel.mapping_fingerprint if status in {
        VerificationStatus.MOCK_VERIFIED,
        VerificationStatus.PHYSICAL_VERIFIED,
    } else ""
    return ChannelVerification(status=status, fingerprint=fingerprint)


def _profile_with_visual_states(profile):
    channels = []
    statuses = {
        2: VerificationStatus.PENDING,
        4: VerificationStatus.MOCK_VERIFIED,
        6: VerificationStatus.PHYSICAL_VERIFIED,
        8: VerificationStatus.FAILED,
    }
    for channel in profile.channels:
        if channel.external_port in statuses:
            updated = replace(
                channel,
                display_name="柠檬" if channel.external_port == 4 else channel.display_name,
            )
            updated = replace(
                updated,
                verification=_evidence(updated, statuses[channel.external_port]),
            )
            channels.append(updated)
        else:
            channels.append(channel)
    return replace(profile, channels=tuple(channels))


def _save(widget, output_dir: Path, name: str) -> Path:
    path = output_dir / name
    widget.repaint()
    if not widget.grab().save(str(path)):
        raise RuntimeError(f"无法保存截图：{path}")
    return path


def main() -> int:
    output_dir = REPO_ROOT / "docs" / "screenshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="olfactorypilot-settings-") as temp_dir:
        local_config = Path(temp_dir) / "config.json"
        app, window = build_application(
            DEFAULT_CONFIG,
            start_worker=False,
            simulation=True,
            local_config_path=local_config,
        )
        controller = window.controller
        settings = window.hardware_settings_view
        original_profile = controller.state.hardware_profile
        assert original_profile is not None

        window.resize(1360, 820)
        window.show()
        settings.open_home()
        window.switchTo(settings)
        app.processEvents()
        created.append(_save(window, output_dir, "settings-home.png"))

        visual_profile = _profile_with_visual_states(original_profile)
        settings.render_profile(
            visual_profile,
            revision=0,
            can_save=True,
            can_mock_verify=True,
        )
        settings.open_port_settings()
        settings.select_port(4)
        app.processEvents()
        created.append(_save(window, output_dir, "settings-port-configuration.png"))
        created.append(_save(window, output_dir, "settings-port-states.png"))
        created.append(_save(window, output_dir, "settings-port-alias.png"))

        settings.open_hardware_settings()
        app.processEvents()
        created.append(_save(window, output_dir, "settings-hardware-view.png"))
        settings.edit_lines_button.setChecked(True)
        app.processEvents()
        created.append(_save(window, output_dir, "settings-hardware-edit.png"))

        settings.open_port_settings()
        settings.select_port(8)
        app.processEvents()
        dialog = MessageBox(
            "验证气口 08",
            "开始后，请确认气口 08 是否出气。\n\n约 20 秒",
            window,
        )
        dialog.yesButton.setText("开始验证")
        dialog.cancelButton.setText("取消")
        dialog.show()
        app.processEvents()
        created.append(
            _save(dialog.widget, output_dir, "verification-start-confirmation.png")
        )
        dialog.hide()
        dialog.close()
        dialog.deleteLater()
        app.processEvents()

        now_ns = time.monotonic_ns()
        channel = visual_profile.registry.by_external_port(8)
        running = HardwareVerificationSnapshot(
            phase=HardwareVerificationPhase.RUNNING,
            external_port=8,
            started_ns=now_ns - 8_000_000_000,
            deadline_ns=now_ns + 12_000_000_000,
            duration_s=20,
            can_stop=True,
            awaiting_user_confirmation=False,
            run_identity="screenshot-running",
            revision=0,
            fingerprint=channel.mapping_fingerprint,
        )
        settings.render_profile(
            visual_profile,
            revision=0,
            can_save=False,
            can_mock_verify=True,
            verification=running,
        )
        app.processEvents()
        created.append(_save(window, output_dir, "verification-running.png"))

        awaiting = replace(
            running,
            phase=HardwareVerificationPhase.AWAITING_CONFIRMATION,
            can_stop=False,
            awaiting_user_confirmation=True,
        )
        settings.render_profile(
            visual_profile,
            revision=0,
            can_save=False,
            can_mock_verify=True,
            verification=awaiting,
        )
        app.processEvents()
        created.append(_save(window, output_dir, "verification-result-confirmation.png"))

        controller.state.hardware_profile = original_profile
        controller.state.channel_registry = original_profile.registry
        controller.state.telemetry.connected = False
        controller.state.hardware_ready = False
        controller._refresh_profile_presentation()
        candidate = replace(
            original_profile,
            channels=tuple(
                replace(channel, display_name="柠檬", enabled=False)
                if channel.external_port == 4
                else channel
                for channel in original_profile.channels
            ),
        )
        if not controller.handle_hardware_profile_save_requested(candidate, 0):
            raise RuntimeError("隔离配置保存失败，无法生成跨页面同步截图")
        window.switchTo(window._manual_interface)
        app.processEvents()
        created.append(_save(window, output_dir, "manual-profile-sync.png"))

        window.close()
        app.processEvents()

    for path in created:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
