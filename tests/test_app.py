import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, build_application, load_config, save_config
from app.models import AppState, SafetyState
from app.services import SafetyManager, ShutdownService
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
    assert "硬件上报安全状态 FAULT" in state.status_message


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
    assert "串口" in serial_result.reason
    assert "建议" in serial_result.suggestion or serial_result.suggestion


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


def test_worker_self_check_exception_sets_failure(qt_app):
    class BoomService:
        def run_checks(self):
            raise RuntimeError("boom")

    worker = HardwareWorker(telemetry_hz=1, check_service=BoomService())
    captured = {}

    def capture(results, ready):
        captured["results"] = results
        captured["ready"] = ready

    worker.self_check_completed.connect(capture)
    worker._run_self_check()

    assert captured["ready"] is False
    assert captured["results"]
    assert any("boom" in item.reason for item in captured["results"])


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
    started = {"flag": False}

    def fake_request():
        called["flag"] = True

    def fake_start():
        started["flag"] = True

    worker.request_self_check = fake_request  # type: ignore[assignment]
    worker.start = fake_start  # type: ignore[assignment]
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))

    controller.request_self_check()
    assert called["flag"] is True
    assert started["flag"] is True or worker.isRunning()
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


def test_ensure_safe_command_blocks_stale_data(qt_app):
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
    controller = MainController(
        state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2, stale_after_s=0.5)
    )
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="ok",
    )
    state.telemetry.connected = True
    state.telemetry.airflow = 0.5
    state.telemetry.timestamp = 0.0
    state.hardware_ready = True

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is False
    assert "过期" in state.status_message
    assert controller._last_safety_state.state == "DATA_STALE"


def test_ensure_safe_command_blocks_when_disconnected(qt_app):
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
    state.telemetry.connected = False
    state.hardware_ready = True

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is False
    assert "未连接" in state.status_message


def test_ensure_safe_command_blocks_low_flow(qt_app):
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
    controller._last_safety_state = SafetyState(
        state="LOW_FLOW",
        airflow=0.1,
        threshold=0.2,
        updated_at=1.0,
        reason="气流低于阈值 0.20",
    )
    state.hardware_ready = True
    state.telemetry.connected = True

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is False
    assert "气流" in state.status_message


def test_ensure_safe_command_blocks_stale(qt_app):
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
    controller._last_safety_state = SafetyState(
        state="DATA_STALE",
        airflow=0.3,
        threshold=0.2,
        updated_at=1.0,
        reason="气流数据过期",
    )
    state.hardware_ready = True
    state.telemetry.connected = True

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is False
    assert "过期" in state.status_message


def test_ensure_safe_command_blocks_when_hardware_not_ready(qt_app):
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
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=0.8,
        threshold=0.2,
        updated_at=1.0,
        reason="正常",
    )
    state.hardware_ready = False
    state.telemetry.connected = True

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is False
    assert "硬件未就绪" in state.status_message


def test_ensure_safe_command_allows_when_safe(qt_app):
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
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=0.8,
        threshold=0.2,
        updated_at=time.time(),
        reason="正常",
    )
    state.hardware_ready = True
    state.telemetry.connected = True
    state.telemetry.timestamp = controller._last_safety_state.updated_at
    state.telemetry.airflow = 0.8

    allowed = controller.ensure_safe_command("FlowApply", source="Protocol")

    assert allowed is True


def test_ui_renders_data_stale_state(qt_app):
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

    state.telemetry.safety_state = "DATA_STALE"
    state.telemetry.safety_reason = "气流数据过期"
    state.telemetry.connected = True
    state.telemetry.airflow = 0.3

    window.render_telemetry(state.telemetry)

    assert "DATA_STALE" in window._telemetry_label.text()
    assert "数据过期" in window._telemetry_label.text()


def test_threshold_update_persists_config(tmp_path: Path, qt_app):
    config_path = tmp_path / "cfg.json"
    save_config(
        config_path,
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "telemetry_hz": 1,
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        },
    )
    config = load_config(config_path)
    config["_config_path"] = config_path
    state = AppState.from_config(config)
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))

    # invalid
    result_invalid = controller.set_low_flow_threshold(-1)
    assert result_invalid is False
    assert "无效阈值" in state.status_message
    assert load_config(config_path)["low_flow_threshold"] == 0.2

    # valid
    result_valid = controller.set_low_flow_threshold(0.7)
    assert result_valid is True
    assert abs(state.low_flow_threshold - 0.7) < 1e-6
    assert abs(controller.safety_manager.low_flow_threshold - 0.7) < 1e-6
    assert abs(load_config(config_path)["low_flow_threshold"] - 0.7) < 1e-6


def test_drop_detection_records_shutdown_event(qt_app):
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
    state.hardware_ready = True

    controller.handle_telemetry({"airflow": 0.1, "connected": True, "timestamp": 1.0})

    assert state.telemetry.safety_state == "LOW_FLOW"
    assert state.last_shutdown_event is not None
    assert state.last_shutdown_event["state"] == "LOW_FLOW"
    assert "紧急" in state.status_message or "阻断" in state.status_message


def test_disconnect_marks_data_stale_and_updates_view(qt_app):
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
    state.hardware_ready = True
    state.telemetry.connected = False
    state.telemetry.airflow = 0.3
    state.telemetry.timestamp = 2.0

    controller._apply_safety_check(initial=False, hardware_safety=None)

    assert state.telemetry.safety_state == "DATA_STALE"
    assert "硬件断开" in state.status_message
    assert state.last_shutdown_event is not None
    assert state.last_shutdown_event["source"] == "disconnect"
    assert "DATA_STALE" in window._telemetry_label.text()


def test_disconnect_source_prefers_disconnect_over_safe_hardware_state(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
            "_config_path": Path("config/default_config.json"),
        }
    )
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))
    state.hardware_ready = True
    state.telemetry.connected = False
    state.telemetry.airflow = 0.3
    state.telemetry.timestamp = 2.0
    controller._has_seen_connection = True

    controller._apply_safety_check(initial=False, hardware_safety="SAFE")

    assert state.last_shutdown_event is not None
    assert state.last_shutdown_event["source"] == "disconnect"


def test_data_stale_status_message(qt_app):
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
    state.hardware_ready = True
    # Seed previous state with timestamp 0 to trigger stale gap
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="ok",
    )

    controller.handle_telemetry({"airflow": 0.5, "connected": True, "timestamp": 2.5})

    assert state.telemetry.safety_state == "DATA_STALE"
    assert "过期" in state.status_message


def test_toolbar_buttons_toggle_with_state(qt_app):
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

    assert window._connect_button.isEnabled() is True
    assert window._reset_button.isEnabled() is False
    assert window._stop_button.isEnabled() is False

    state.hardware_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    controller._refresh_toolbar_state()

    assert window._reset_button.isEnabled() is True
    assert window._stop_button.isEnabled() is True


def test_toolbar_tooltips_explain_disabled_reasons(qt_app):
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

    controller._refresh_toolbar_state()
    assert "未连接" in window._reset_button.toolTip()
    assert "未连接" in window._stop_button.toolTip()

    state.telemetry.connected = True
    state.hardware_ready = True
    state.telemetry.safety_state = "LOW_FLOW"
    controller._refresh_toolbar_state()
    assert window._reset_button.isEnabled() is False
    assert "安全状态" in window._reset_button.toolTip()

    state.telemetry.safety_state = "SAFE"
    controller._refresh_toolbar_state()
    assert window._reset_button.isEnabled() is True
    assert "重置硬件" in window._reset_button.toolTip()


def test_connect_button_disables_during_progress(qt_app):
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

    controller._connect_in_progress = True
    controller._refresh_toolbar_state()
    assert window._connect_button.isEnabled() is False
    assert "正在连接" in window._connect_button.toolTip()


def test_connect_failure_surfaces_reason_and_allows_retry(qt_app):
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

    controller._connect_in_progress = True
    failing = [
        SelfCheckResult(
            name="USB-6001",
            type="ni",
            status="FAIL",
            reason="未检测到 NI 设备",
            suggestion="检查 USB 连接",
            checked_at=1.0,
        )
    ]
    controller.handle_self_check(failing, hardware_ready=False)

    assert "未检测到 NI 设备" in state.status_message
    assert controller._connect_in_progress is False
    assert window._connect_button.isEnabled() is True
    assert "重试" in window._connect_button.toolTip() or "自检" in window._connect_button.toolTip()


def test_connect_requests_self_check_and_sets_connected_status(qt_app):
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
    started = {"flag": False}

    def fake_request():
        called["flag"] = True

    def fake_start():
        started["flag"] = True

    worker.request_self_check = fake_request  # type: ignore[assignment]
    worker.start = fake_start  # type: ignore[assignment]
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))
    window = MainWindow(controller, state)
    controller.bind_view(window)

    controller.connect_hardware()

    assert called["flag"] is True
    assert started["flag"] is True
    assert "连接" in state.status_message

    controller.handle_self_check([], True)

    assert state.hardware_ready is True
    assert state.telemetry.connected is True
    assert "连接" in state.status_message
    assert window._reset_button.isEnabled()


def test_reset_requires_ready_and_triggers_self_check(qt_app):
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
    now_ts = time.time()
    state.hardware_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.telemetry.airflow = 1.0
    state.telemetry.timestamp = now_ts
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=1.0,
        threshold=0.2,
        updated_at=now_ts,
        reason="ok",
    )
    called = {"flag": False}

    def fake_request():
        called["flag"] = True

    worker.request_self_check = fake_request  # type: ignore[assignment]

    controller.reset_hardware()

    assert called["flag"] is True
    assert state.hardware_ready is False
    assert state.telemetry.connected is False
    assert state.last_shutdown_event is not None
    assert state.last_shutdown_event["reason"] == "reset_request"


def test_reset_blocked_when_not_ready(qt_app):
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

    controller.reset_hardware()

    assert called["flag"] is False
    assert "阻断" in state.status_message or "自检未通过" in state.status_message


def test_stop_records_shutdown_event_and_marks_disconnected(qt_app):
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
    now_ts = time.time()
    state.hardware_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.telemetry.airflow = 1.0
    state.telemetry.timestamp = now_ts
    controller._last_safety_state = SafetyState(
        state="SAFE",
        airflow=1.0,
        threshold=0.2,
        updated_at=now_ts,
        reason="ok",
    )
    stop_called = {"flag": False}

    def fake_stop():
        stop_called["flag"] = True

    worker.stop = fake_stop  # type: ignore[assignment]

    controller.stop_hardware()

    assert stop_called["flag"] is True
    assert state.hardware_ready is False
    assert state.telemetry.connected is False
    assert state.last_shutdown_event is not None
    assert state.last_shutdown_event["source"] == "stop"


def test_shutdown_service_persists_event_and_updates_state(tmp_path: Path):
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
    safety = SafetyManager(low_flow_threshold=0.2)
    record_path = tmp_path / "shutdown.json"
    service = ShutdownService(
        state=state,
        worker=worker,
        safety_manager=safety,
        retry_limit=0,
        retry_interval=0,
        record_path=record_path,
        time_func=lambda: 123.0,
        sleep_func=lambda _s: None,
    )
    state.hardware_ready = True
    state.telemetry.connected = True

    event = service.shutdown(source="tests", reason="unit", force=True)

    assert event["result"] == "success"
    assert event["source"] == "tests"
    assert record_path.exists()
    on_disk = ShutdownService.load_last_event(record_path)
    assert on_disk and on_disk["source"] == "tests"
    assert state.last_shutdown_event["result"] == "success"
    assert state.hardware_ready is False
    assert state.telemetry.connected is False
    assert "已安全关闭" in state.telemetry.safety_reason


def test_shutdown_retry_marks_unsafe_on_failure(tmp_path: Path):
    class FailingWorker:
        def __init__(self) -> None:
            self.stopped = False
            self.calls = 0

        def close_all_channels(self) -> bool:
            self.calls += 1
            raise RuntimeError("comm error")

        def stop_heaters(self) -> bool:
            return False

        def flush_logs(self) -> None:
            return None

        def release_resources(self) -> None:
            return None

        def stop(self) -> None:
            self.stopped = True

    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    worker = FailingWorker()
    safety = SafetyManager(low_flow_threshold=0.2)
    service = ShutdownService(
        state=state,
        worker=worker,  # type: ignore[arg-type]
        safety_manager=safety,
        retry_limit=1,
        retry_interval=0,
        record_path=tmp_path / "fail.json",
        time_func=lambda: 456.0,
        sleep_func=lambda _s: None,
    )
    state.hardware_ready = True
    state.telemetry.connected = True

    event = service.shutdown(source="tests", reason="fail", force=True)

    assert event["result"] == "unsafe"
    assert event["retries"] == 1
    assert "失败" in event["error"] or "error" in event["error"].lower()
    assert state.telemetry.safety_state == "DATA_STALE"
    assert worker.stopped is True


def test_last_shutdown_banner_blocks_controls(qt_app):
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    state.last_shutdown_event = {
        "result": "unsafe",
        "error": "timeout",
        "source": "stop",
        "ts": 999.0,
    }
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))
    window = MainWindow(controller, state)
    controller.bind_view(window)

    assert "未完成" in state.status_message
    assert window._reset_button.isEnabled() is False
    assert window._stop_button.isEnabled() is False
    assert "未完成" in window._shutdown_label.text()


def test_shutdown_record_path_resolves_outside_config_dir(tmp_path: Path, qt_app):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cfg_path = config_dir / "default_config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    config = {
        "language": "zh-CN",
        "window_title": "测试窗口",
        "log_level": "INFO",
        "low_flow_threshold": 0.2,
        "safety_state": "SAFE",
        "_config_path": cfg_path,
        "shutdown_record_path": "logs/last_shutdown_event.json",
    }
    state = AppState.from_config(config)
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(
        state,
        worker,
        safety_manager=SafetyManager(low_flow_threshold=0.2),
        config={"shutdown_record_path": "logs/last_shutdown_event.json"},
    )

    resolved = controller._resolve_record_path(
        {"shutdown_record_path": "logs/last_shutdown_event.json"}
    )

    assert resolved == tmp_path / "logs" / "last_shutdown_event.json"


def test_stop_persists_shutdown_to_configured_path(tmp_path: Path, qt_app):
    config = {
        "shutdown_record_path": str(tmp_path / "stop.json"),
        "shutdown_retry_limit": 0,
        "shutdown_retry_interval_s": 0,
    }
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
    controller = MainController(
        state,
        worker,
        safety_manager=SafetyManager(low_flow_threshold=0.2),
        config=config,
    )
    window = MainWindow(controller, state)
    controller.bind_view(window)
    state.hardware_ready = True
    state.telemetry.connected = True

    controller.stop_hardware()

    record_path = Path(config["shutdown_record_path"])
    assert record_path.exists()
    assert state.last_shutdown_event["result"] == "success"
    assert state.last_shutdown_event["source"] == "stop"


def test_help_manual_missing_sets_status(qt_app):
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
    state.manual_path = Path("docs/non-existent.pdf")

    controller.open_help_manual()

    assert "手册" in state.status_message
    assert "未找到" in state.status_message


def test_help_manual_opens_with_system(monkeypatch, tmp_path: Path, qt_app):
    manual = tmp_path / "manual.pdf"
    manual.write_text("hello", encoding="utf-8")
    state = AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "low_flow_threshold": 0.2,
            "safety_state": "SAFE",
        }
    )
    state.manual_path = manual
    worker = HardwareWorker(telemetry_hz=1)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.2))
    opened = {}

    def fake_open(path: Path):
        opened["path"] = Path(path)

    monkeypatch.setattr(controller, "_open_with_system", fake_open)

    controller.open_help_manual()

    assert opened["path"] == manual
    assert "已打开" in state.status_message
