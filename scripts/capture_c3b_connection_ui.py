from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QAbstractButton, QLabel

from app.main import DEFAULT_CONFIG, build_application
from scripts.dev_temp import cleanup_session, create_session

EVIDENCE_DIR = (
    REPO_ROOT
    / "docs"
    / "sprint-artifacts"
    / "evidence"
    / "c-3b-connection-presentation-2026-09-14"
)


def _wait_until(app, predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    if not predicate():
        raise RuntimeError("simulation UI 状态等待超时")


def _save(window, output_dir: Path, name: str) -> Path:
    path = output_dir / name
    window.repaint()
    if not window.grab().save(str(path)):
        raise RuntimeError(f"无法保存截图：{path}")
    return path


def _pump_for(app, duration_s: float) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _visible_text(window) -> str:
    widgets = window.findChildren(QLabel) + window.findChildren(QAbstractButton)
    return "\n".join(widget.text() for widget in widgets if widget.isVisibleTo(window))


def main() -> int:
    if EVIDENCE_DIR.exists():
        raise FileExistsError(f"证据目录已存在，拒绝覆盖：{EVIDENCE_DIR}")
    session = create_session("ui-capture", REPO_ROOT)
    capture_dir = session.path / "evidence"
    capture_dir.mkdir()
    screenshot_names: list[str] = []
    window = None
    capture_complete = False
    request_count_after_wait = 0
    request_count_after_reconnect = 0

    try:
        app, window = build_application(
            DEFAULT_CONFIG,
            simulation=True,
            local_config_path=session.path / "config.json",
        )
        controller = window.controller
        window.resize(1360, 820)
        window.show()
        if not controller.schedule_startup_auto_connect():
            raise RuntimeError("startup auto-connect 未能调度")
        _wait_until(app, lambda: controller._connection_phase == "CONNECTED")
        assert controller._connection_request_count == 1
        assert window._connection_badge.text() == "设备已连接"
        assert not window._connect_button.isVisible()
        screenshot_names.append("device-connected.png")
        _save(window, capture_dir, screenshot_names[-1])

        controller.stop_hardware()
        app.processEvents()
        assert controller._connection_phase == "DISCONNECTED"
        assert window._connection_badge.text() == "设备未连接"
        assert window._connect_button.text() == "重新连接"
        assert window._connect_button.isVisible()
        assert "已安全停止" not in _visible_text(window)
        request_count = controller._connection_request_count
        _pump_for(app, 10.0)
        if controller._connection_request_count != request_count:
            raise RuntimeError("Global Stop 后发生了非预期自动重新连接")
        request_count_after_wait = controller._connection_request_count
        screenshot_names.append("device-disconnected-reconnect.png")
        _save(window, capture_dir, screenshot_names[-1])
        window._connect_button.click()
        _wait_until(
            app,
            lambda: window.controller._connection_phase == "CONNECTED",
        )
        assert window.controller._connection_request_count == request_count + 1
        assert window._connection_badge.text() == "设备已连接"
        assert not window._connect_button.isVisible()
        assert "已安全停止" not in _visible_text(window)
        request_count_after_reconnect = window.controller._connection_request_count
        screenshot_names.append("device-reconnected.png")
        _save(window, capture_dir, screenshot_names[-1])

        screenshots = []
        for name in screenshot_names:
            path = capture_dir / name
            screenshots.append(
                {
                    "file": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
                }
            )
        manifest = {
            "schema_version": 1,
            "evidence_kind": "simulation_ui_reference",
            "hal": "MockHAL",
            "real_hardware_access": False,
            "post_stop_wait_seconds": 10.0,
            "request_count_after_startup": 1,
            "request_count_after_wait": request_count_after_wait,
            "request_count_after_reconnect": request_count_after_reconnect,
            "screenshots": screenshots,
        }
        (capture_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        capture_complete = True
    finally:
        if window is not None:
            window.controller.shutdown_and_teardown()
            window.close()
        if not capture_complete:
            cleanup_session(session.path, session.token)

    try:
        EVIDENCE_DIR.parent.mkdir(parents=True, exist_ok=True)
        capture_dir.replace(EVIDENCE_DIR)
    except Exception:
        cleanup_session(session.path, session.token)
        raise
    cleanup_session(session.path, session.token)
    for name in screenshot_names:
        print(EVIDENCE_DIR / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
