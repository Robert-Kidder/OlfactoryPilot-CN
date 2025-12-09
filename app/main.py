from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.controllers import MainController
from app.models import AppState
from app.services import HardwareCheckService, SafetyManager, ShutdownService
from app.views import MainWindow
from app.workers import HardwareWorker

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DEFAULT_CONFIG = BASE_DIR / "config" / "default_config.json"


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_application(
    config_path: Path, start_worker: bool = True
) -> tuple[QApplication, MainWindow]:
    config_path = Path(config_path)
    config = load_config(config_path)

    # When running from a PyInstaller bundle, keep a user-writable copy of the config
    # to persist threshold updates instead of touching the read-only bundled file.
    user_config_path: Path | None = None
    if getattr(sys, "_MEIPASS", None) and config_path == DEFAULT_CONFIG:
        user_config_path = Path.home() / ".olfactorypilot" / config_path.name
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not user_config_path.exists():
            save_config(user_config_path, config)
        else:
            try:
                config = load_config(user_config_path)
            except Exception:
                config = load_config(config_path)

    configure_logging(config.get("log_level", "INFO"))
    config["_config_path"] = config_path
    if user_config_path:
        config["_user_config_path"] = user_config_path
    state = AppState.from_config(config)
    if "shutdown_record_path" not in config:
        config["shutdown_record_path"] = str(Path.cwd() / "logs" / "last_shutdown_event.json")
    record_path = config.get("shutdown_record_path")
    shutdown_record_path = None
    if record_path:
        shutdown_record_path = Path(record_path)
        if not shutdown_record_path.is_absolute():
            anchor = user_config_path or config_path
            base = Path(anchor).parent
            if base.name == "config":
                base = base.parent
            shutdown_record_path = base / shutdown_record_path
    last_shutdown = ShutdownService.load_last_event(shutdown_record_path)
    if last_shutdown:
        state.last_shutdown_event = last_shutdown
        state.hardware_ready = False
        state.telemetry.connected = False

    safety = SafetyManager(config.get("low_flow_threshold", 0.2))
    check_service = HardwareCheckService.from_config(config)

    if os.name == "nt" and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    qt_app = QApplication.instance() or QApplication(sys.argv)
    worker = HardwareWorker(
        telemetry_hz=int(config.get("telemetry_hz", 5)),
        check_service=check_service,
    )
    controller = MainController(state, worker, safety_manager=safety, config=config)
    window = MainWindow(controller, state)
    controller.bind_view(window)

    if start_worker:
        controller.start_worker()

    qt_app.aboutToQuit.connect(controller.shutdown)
    return qt_app, window


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OlfactoryPilot 控制台占位应用")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="配置文件路径，默认使用 config/default_config.json",
    )
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="跳过占位硬件线程（用于CI/测试）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    qt_app, window = build_application(args.config, start_worker=not args.no_worker)
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
