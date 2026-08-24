from __future__ import annotations

import math
import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.main import DEFAULT_CONFIG, build_application
from app.views.manual_experiment_view import ManualExperimentDraft


def main() -> int:
    output_dir = REPO_ROOT / "docs" / "screenshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    app, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
        local_config_path=output_dir / ".screenshot-config-not-created.json",
    )
    controller = window.controller
    controller.state.telemetry.connected = True
    controller.state.telemetry.safety_state = "SAFE"
    controller.state.telemetry.airflow = 1498.0
    controller.state.hardware_ready = True
    controller._render_manual_snapshot()
    window.render_telemetry(controller.state.telemetry)

    manual = window.manual_experiment_view
    ports = tuple(
        replace(port, display_name="薄荷" if port.external_port == 2 else port.display_name)
        for port in manual.snapshot.ports
    )
    manual.render_snapshot(
        replace(
            manual.snapshot,
            draft=replace(
                ManualExperimentDraft(),
                selected_external_ports=(2,),
            ),
            ports=ports,
            controls_enabled=True,
            can_apply_flow=True,
            can_release=True,
            supply_enabled=False,
            status_text="",
            detail_text="",
        )
    )
    for index in range(160):
        baseline = 480.0 if index < 48 else 1498.0
        manual.update_a_observation(baseline + math.sin(index / 4.0) * 12.0)

    window.show()
    app.processEvents()
    main_path = output_dir / "story-4-6-manual-experiment.png"
    if not window.grab().save(str(main_path)):
        raise RuntimeError(f"无法保存截图：{main_path}")

    window.open_settings()
    window.hardware_settings_view.select_port(2)
    app.processEvents()
    settings_path = output_dir / "story-4-6-port-settings.png"
    if not window.settings_dialog.grab().save(str(settings_path)):
        raise RuntimeError(f"无法保存截图：{settings_path}")

    window.hardware_settings_view.advanced_toggle.setChecked(True)
    app.processEvents()
    advanced_path = output_dir / "story-4-6-port-settings-advanced.png"
    if not window.settings_dialog.grab().save(str(advanced_path)):
        raise RuntimeError(f"无法保存截图：{advanced_path}")

    window.settings_dialog.close()
    window.close()
    app.processEvents()
    print(main_path)
    print(settings_path)
    print(advanced_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
