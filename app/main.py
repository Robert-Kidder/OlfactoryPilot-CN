from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

# Ensure package imports work when running as a script (python app/main.py).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.controllers import MainController
from app.models import AppState
from app.services import HalInterface, HardwareCheckService, MockHAL, RealHAL, SafetyManager, ShutdownService
from app.views import MainWindow
from app.workers import HardwareWorker

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DEFAULT_CONFIG = BASE_DIR / "config" / "default_config.json"
DEFAULT_LOCAL_CONFIG = BASE_DIR / "config" / "local_config.json"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\OlfactoryPilot-CN.SessionOwner.v1"
_LINGERING_INSTANCE_GUARDS = []


class SingleInstanceGuard:
    """Own the Windows process-wide application mutex for the GUI lifetime."""

    def __init__(self, name: str = SINGLE_INSTANCE_MUTEX_NAME) -> None:
        self._name = str(name)
        self._handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if os.name != "nt":
            raise RuntimeError("OlfactoryPilot 单实例 mutex 仅支持 Windows。")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建应用单实例 mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        kernel32 = self._kernel32
        self._handle = None
        self._kernel32 = None
        if handle is not None and kernel32 is not None:
            kernel32.CloseHandle(handle)


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_effective_config(config_path: Path, local_config_path: Path | None = None) -> dict:
    config = load_config(config_path)
    if local_config_path and local_config_path.exists():
        config = merge_config(config, load_config(local_config_path))
    return config


def migrate_bundled_user_config(user_config: dict, bundled_config: dict) -> bool:
    if (
        str(user_config.get("serial_port", "")).upper() == "COM3"
        and str(bundled_config.get("serial_port", "")).upper() == "COM6"
    ):
        user_config["serial_port"] = bundled_config["serial_port"]
        if "baud_rate" in bundled_config:
            user_config["baud_rate"] = bundled_config["baud_rate"]
        return True
    return False


def configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_application(
    config_path: Path,
    start_worker: bool = True,
    simulation: bool = False,
    hal: HalInterface | None = None,
    local_config_path: Path | None = None,
) -> tuple[QApplication, MainWindow]:
    config_path = Path(config_path)
    is_default_config = config_path.resolve() == Path(DEFAULT_CONFIG).resolve()
    bundled = bool(getattr(sys, "_MEIPASS", None) and is_default_config)
    if local_config_path is None and not bundled and is_default_config:
        local_config_path = DEFAULT_LOCAL_CONFIG
    config = load_effective_config(config_path, local_config_path)

    # When running from a PyInstaller bundle, keep a user-writable copy of the config
    # to persist threshold updates instead of touching the read-only bundled file.
    user_config_path: Path | None = None
    if bundled:
        user_config_path = Path.home() / ".olfactorypilot" / config_path.name
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        if not user_config_path.exists():
            save_config(user_config_path, config)
        else:
            try:
                bundled_config = config
                config = load_config(user_config_path)
                if migrate_bundled_user_config(config, bundled_config):
                    save_config(user_config_path, config)
            except Exception:
                config = load_config(config_path)

    configure_logging(config.get("log_level", "INFO"))
    config["_config_path"] = config_path
    if local_config_path:
        config["_local_config_path"] = local_config_path
        config["_config_write_path"] = local_config_path
    if user_config_path:
        config["_user_config_path"] = user_config_path
        config["_config_write_path"] = user_config_path
    state = AppState.from_config(config)
    hal_mode = str(config.get("hal_mode", "auto")).strip().lower()
    if hal_mode in {"mock", "simulation"}:
        state.simulation_mode = True
    state.simulation_mode = bool(simulation or state.simulation_mode)
    if state.simulation_mode and "[模拟模式]" not in state.window_title:
        state.window_title = f"{state.window_title} [模拟模式]"
    config["simulation_mode"] = state.simulation_mode
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
    check_service = None if state.simulation_mode else HardwareCheckService.from_config(config)

    hal_instance = hal
    if hal_instance is None:
        if state.simulation_mode:
            hal_instance = MockHAL()
        else:
            try:
                hal_instance = RealHAL.from_config(config)
            except Exception as exc:
                raise RuntimeError(f"RealHAL 初始化失败：{exc}") from exc

    if os.name == "nt" and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    qt_app = QApplication.instance() or QApplication(sys.argv)
    worker = HardwareWorker(
        telemetry_hz=int(config.get("telemetry_hz", 5)),
        ttl_config=config,
        check_service=check_service,
        hal=hal_instance,
        simulation=state.simulation_mode,
    )
    controller = MainController(state, worker, safety_manager=safety, config=config)
    window = MainWindow(controller, state)
    controller.bind_view(window)

    if start_worker:
        controller.start_worker()

    qt_app.aboutToQuit.connect(controller.shutdown_and_teardown)
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
        "--local-config",
        type=Path,
        default=None,
        help="本机覆盖配置路径，默认使用 config/local_config.json（若存在）",
    )
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="跳过占位硬件线程（用于CI/测试）",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="启用模拟模式：跳过物理硬件检查并使用 Mock HAL",
    )
    return parser.parse_args(argv)


def report_startup_error(exc: Exception) -> None:
    message = f"启动失败：{exc}"
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path = Path.home() / ".olfactorypilot" / "startup_error.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(details)
    except Exception:
        pass
    try:
        sys.stderr.write(details)
    except Exception:
        pass
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "OlfactoryPilot 启动失败",
            f"{message}\n\n日志：{log_path}",
        )
        app.processEvents()
    except Exception:
        pass


def report_duplicate_instance() -> None:
    message = "OlfactoryPilot 已在运行。为保护活动会话与硬件，本次启动已取消。"
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        pass
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(
            None,
            "OlfactoryPilot 已在运行",
            message,
        )
        app.processEvents()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    guard = SingleInstanceGuard()
    acquired = False
    controller = None
    try:
        acquired = guard.acquire()
        if not acquired:
            report_duplicate_instance()
            return 2
        args = parse_args(sys.argv[1:] if argv is None else argv)
        qt_app, window = build_application(
            args.config,
            start_worker=not args.no_worker,
            simulation=args.simulation,
            local_config_path=args.local_config,
        )
        controller = getattr(window, "controller", None)
        window.show()
        result = qt_app.exec()
        return result
    except Exception as exc:
        report_startup_error(exc)
        return 1
    finally:
        if acquired:
            retain_guard = False
            if controller is not None:
                lifecycle_stopped = getattr(
                    controller,
                    "lifecycle_stopped",
                    None,
                )
                if not callable(lifecycle_stopped):
                    retain_guard = True
                else:
                    try:
                        retain_guard = not bool(lifecycle_stopped())
                    except Exception:
                        retain_guard = True
            if retain_guard:
                _LINGERING_INSTANCE_GUARDS.append(guard)
            else:
                guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
