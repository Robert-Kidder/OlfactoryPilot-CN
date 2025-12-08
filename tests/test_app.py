import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, build_application, load_config
from app.models import AppState
from app.services import SafetyManager
from app.services.hardware_check_service import HardwareCheckService, SelfCheckResult
from app.views import MainWindow
from app.workers import HardwareWorker


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_load_config_and_state():
    config = load_config(DEFAULT_CONFIG)
    state = AppState.from_config(config)
    assert state.language == "zh-CN"
    assert "OlfactoryPilot" in state.window_title


def test_main_window_builds(qt_app):
    _, window = build_application(DEFAULT_CONFIG, start_worker=False)
    assert window.windowTitle()
    assert window.tabs.count() >= 3


def test_hardware_worker_start_stop(qt_app):
    worker = HardwareWorker(telemetry_hz=20)
    worker.start()
    assert worker.isRunning()
    time.sleep(0.1)
    worker.stop()
    assert not worker.isRunning()


def test_build_application_with_temp_config(tmp_path: Path, qt_app):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "language": "zh-CN",
          "window_title": "测试窗口",
          "log_level": "DEBUG",
          "telemetry_hz": 1
        }
        """,
        encoding="utf-8",
    )
    app, window = build_application(config_path, start_worker=False)
    assert app is qt_app
    assert window.windowTitle() == "测试窗口"


def test_low_airflow_triggers_warning(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.5,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.5))

    controller.handle_telemetry({"airflow": 0.1, "connected": True, "timestamp": 1})
    assert state.telemetry.safety_state == "LOW_FLOW"
    assert "气流低于阈值" in state.status_message

    controller.handle_telemetry({"airflow": 0.6, "connected": True, "timestamp": 2})
    assert state.telemetry.safety_state == "SAFE"
    assert "气流恢复正常" in state.status_message


def test_hysteresis_prevents_flapping(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.5,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(
        state, worker, safety_manager=SafetyManager(low_flow_threshold=0.5, recovery_margin=0.1)
    )

    controller.handle_telemetry({"airflow": 0.45, "connected": True, "timestamp": 1})
    assert state.telemetry.safety_state == "LOW_FLOW"

    controller.handle_telemetry({"airflow": 0.54, "connected": True, "timestamp": 2})
    assert state.telemetry.safety_state == "LOW_FLOW"

    controller.handle_telemetry({"airflow": 0.62, "connected": True, "timestamp": 3})
    assert state.telemetry.safety_state == "SAFE"


def test_initial_state_not_warned_when_disconnected(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.5,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.5))

    controller._apply_safety_check(initial=True)
    assert state.telemetry.safety_state == "SAFE"
    assert state.status_message == "等待硬件连接..."


def test_hardware_safety_overrides_flow(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))

    controller.handle_telemetry(
        {"airflow": 1.0, "connected": True, "timestamp": 1, "safety_state": "FAULT"}
    )
    assert state.telemetry.safety_state == "FAULT"
    assert "硬件报告安全状态 FAULT" in state.status_message


def test_hardware_safe_does_not_clear_flow_sticky_zone(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.5,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(
        state, worker, safety_manager=SafetyManager(low_flow_threshold=0.5, recovery_margin=0.1)
    )

    controller.handle_telemetry(
        {"airflow": 0.55, "connected": True, "timestamp": 1, "safety_state": "FAULT"}
    )
    assert state.telemetry.safety_state == "FAULT"

    controller.handle_telemetry(
        {"airflow": 0.55, "connected": True, "timestamp": 2, "safety_state": "SAFE"}
    )
    assert state.telemetry.safety_state == "LOW_FLOW"  # still in sticky zone
    assert "气流低于阈值" in state.status_message  # still warning, not recovered

    controller.handle_telemetry(
        {"airflow": 0.7, "connected": True, "timestamp": 3, "safety_state": "SAFE"}
    )
    assert state.telemetry.safety_state == "SAFE"
    assert "气流恢复正常" in state.status_message


def test_hardware_check_service_pass(monkeypatch):
    class FakeDevice:
        def __init__(self, name: str, product_type: str) -> None:
            self.name = name
            self.product_type = product_type

    devices = [
        FakeDevice("Dev1", "NI USB-6001"),
        FakeDevice("Dev2", "NI USB-6501"),
    ]
    nidaqmx_loader = lambda: SimpleNamespace(  # noqa: E731
        System=SimpleNamespace(local=lambda: SimpleNamespace(devices=devices))
    )

    class DummySerialException(Exception):
        pass

    class DummySerial:
        def __init__(self, port: str, baudrate: int, timeout: float = 1) -> None:
            self.port = port
            self.baudrate = baudrate
            self.timeout = timeout

        def close(self) -> None:
            return None

    list_ports_module = SimpleNamespace(
        comports=lambda: [SimpleNamespace(device="COM3"), SimpleNamespace(device="COM4")]
    )
    serial_provider = lambda: (  # noqa: E731
        SimpleNamespace(Serial=DummySerial, SerialException=DummySerialException),
        list_ports_module,
    )

    service = HardwareCheckService(
        expected_ni_devices=["USB-6001", "USB-6501"],
        serial_port="COM3",
        baud_rate=115200,
        time_func=lambda: 123.0,
        nidaqmx_loader=nidaqmx_loader,
        serial_provider=serial_provider,
    )

    results, ready = service.run_checks()
    assert ready is True
    ni_status = {r.name: r.status for r in results if r.type == "ni"}
    assert ni_status == {"USB-6001": "PASS", "USB-6501": "PASS"}
    serial_result = next(r for r in results if r.type == "serial")
    assert serial_result.status == "PASS"
    assert serial_result.checked_at == 123.0


def test_hardware_check_service_reports_failures(monkeypatch):
    def failing_nidaqmx_loader():
        raise ModuleNotFoundError("no nidaqmx")

    class DummySerialException(Exception):
        pass

    def raising_serial(*_args, **_kwargs):
        raise DummySerialException("Permission denied")

    list_ports_module = SimpleNamespace(comports=lambda: [SimpleNamespace(device="COM7")])
    serial_provider = lambda: (  # noqa: E731
        SimpleNamespace(Serial=raising_serial, SerialException=DummySerialException),
        list_ports_module,
    )

    service = HardwareCheckService(
        expected_ni_devices=["USB-6001", "USB-6501"],
        serial_port="COM8",
        baud_rate=9600,
        nidaqmx_loader=failing_nidaqmx_loader,
        serial_provider=serial_provider,
    )

    results, ready = service.run_checks()
    assert ready is False
    ni_failures = [r for r in results if r.type == "ni"]
    assert ni_failures and ni_failures[0].status == "FAIL"
    assert "NI-DAQmx" in ni_failures[0].reason

    serial_result = next(r for r in results if r.type == "serial")
    assert serial_result.status == "FAIL"
    assert "未找到配置的串口" in serial_result.reason or "没有找到" in serial_result.reason
    assert (
        "防止其他程序使用该串口" in serial_result.suggestion or "插拔" in serial_result.suggestion
    )


def test_self_check_updates_state_and_view(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))
    window = MainWindow(controller, state)
    controller.bind_view(window)

    results = [
        SelfCheckResult(
            name="USB-6001",
            type="ni",
            status="FAIL",
            reason="未检测到设备",
            suggestion="插拔后重试",
            checked_at=123.0,
        ),
        SelfCheckResult(
            name="COM3",
            type="serial",
            status="PASS",
            reason="串口连接正常",
            suggestion="无需操作",
            checked_at=123.0,
        ),
    ]
    controller.handle_self_check(results, hardware_ready=False)

    assert state.hardware_ready is False
    assert "硬件自检失败" in state.status_message
    assert "USB-6001" in window._self_check_label.text()
    assert "FAIL" in window._self_check_label.text()
    assert "最近自检" in window._self_check_label.text()


def test_recheck_triggers_worker(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    called = {"flag": False}

    def fake_request():
        called["flag"] = True

    worker.request_self_check = fake_request  # type: ignore[assignment]
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))

    controller.request_self_check()
    assert called["flag"] is True
    assert "自检" in state.status_message


def test_ensure_hardware_ready_blocks_when_not_ready(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))

    state.hardware_ready = False
    allowed = controller.ensure_hardware_ready("Connect")
    assert allowed is False
    assert "阻断 Connect" in state.status_message

    state.hardware_ready = True
    assert controller.ensure_hardware_ready("Connect") is True
