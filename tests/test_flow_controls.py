import pytest
import time

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, load_config
from app.models import AppState
from app.services import MockHAL, SafetyManager
from app.views import MainWindow
from app.workers import HardwareWorker


def _build_flow_context(low_flow_threshold: float = 0.2):
    config = load_config(DEFAULT_CONFIG)
    config["low_flow_threshold"] = low_flow_threshold
    state = AppState.from_config(config)
    state.hardware_ready = True
    hal = MockHAL()
    worker = HardwareWorker(telemetry_hz=5, hal=hal, simulation=True)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=low_flow_threshold))
    window = MainWindow(controller, state)
    controller.bind_view(window)
    return state, controller, window, hal


def _wait_until(qt_app, predicate, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_apply_allowed_in_idle_zero_flow(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.5)
    controller.handle_telemetry({"airflow": 0.0, "connected": True, "timestamp": 1.0})

    pretest = window.pretest_view
    assert pretest.is_apply_enabled()

    result = controller.handle_apply_request(100.0, 200.0, 50.0)
    assert result.success is True
    assert hal.flow_commands[-1] == ("A", 150.0, True)
    assert state.flow_setpoints_ready is True
    assert pretest.is_apply_enabled()


def test_apply_success_updates_ui_and_state(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.2, "connected": True, "timestamp": 1.0})

    pretest = window.pretest_view
    pretest.set_targets(a=120.0, b=800.0, c=300.0)
    result = controller.handle_apply_request(120.0, 800.0, 300.0)

    assert result.success is True
    assert abs(result.a_comp - 420.0) < 1e-6
    assert ("流量已应用" in result.message) or ("å·²åº”ç”¨" in result.message)
    assert hal.flow_commands[-1] == ("A", 420.0, True)
    applied = pretest.get_applied_targets()
    assert applied["A_comp"] == pytest.approx(420.0)
    assert applied["B"] == pytest.approx(800.0)
    assert applied["C"] == pytest.approx(300.0)
    assert pretest.is_apply_enabled()


def test_apply_flow_does_not_switch_master_valve(qt_app):
    state, controller, _window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 0.0, "connected": True, "timestamp": 1.0})

    result = controller.handle_apply_request(120.0, 800.0, 300.0)

    assert result.success is True
    assert hal.master_events == []
    assert hal.flow_commands == [
        ("B", 800.0, False),
        ("C", 300.0, False),
        ("A", 420.0, True),
    ]


def test_apply_failure_surfaces_error_and_keeps_previous(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    # Seed an earlier successful apply
    controller.handle_apply_request(50.0, 100.0, 25.0)
    hal.fail_on = {"B"}  # force B channel failure
    result = controller.handle_apply_request(60.0, 200.0, 40.0)

    assert result.success is False
    assert "setpoint 未确认" in result.message
    applied = window.pretest_view.get_applied_targets()
    # Should keep previous applied values
    assert applied["B"] == pytest.approx(100.0)
    assert window.pretest_view.is_apply_enabled()


def test_stim_sequence_opens_master_after_flows(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view.flow_sequence_requested.emit("stim_start", 60.0, 200.0, 40.0)

    # B then C then A (stim_start uses C=0)
    assert hal.flow_commands[0][:2] == ("B", 200.0)
    assert hal.flow_commands[1][:2] == ("C", 0.0)
    assert hal.flow_commands[2][0] == "A"


def test_valve_click_only_stages_channel_until_start(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view._handle_click(1)

    assert window.pretest_view.selected_valves() == [1]
    assert hal.get_line_state("Dev1/P0.0") is None
    assert hal.master_events == []


def test_start_opens_staged_valves(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view._handle_click(1)
    window.pretest_view._toggle_manual_mode(True)
    window.pretest_view._handle_start_clicked(True)
    _wait_until(qt_app, lambda: hal.get_line_state("Dev1/P0.0") is True)

    assert hal.flow_commands[:3] == [
        ("B", 1000.0, False),
        ("C", 0.0, False),
        ("A", 500.0, False),
    ]
    assert hal.get_line_state("Dev1/P0.0") is True
    assert hal.get_line_state("Dev2/P1.0") is True


def test_start_returns_immediately_with_slow_flow_hardware(qt_app):
    class SlowHAL(MockHAL):
        def set_flow(self, channel, value=None, *, comp=False):
            time.sleep(0.08)
            return super().set_flow(channel, value, comp=comp)

    config = load_config(DEFAULT_CONFIG)
    state = AppState.from_config(config)
    state.hardware_ready = True
    hal = SlowHAL()
    worker = HardwareWorker(telemetry_hz=5, hal=hal, simulation=True)
    controller = MainController(state, worker, safety_manager=SafetyManager(low_flow_threshold=0.1))
    window = MainWindow(controller, state)
    controller.bind_view(window)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view._handle_click(1)
    window.pretest_view._toggle_manual_mode(True)
    started_at = time.time()
    window.pretest_view._handle_start_clicked(True)
    elapsed = time.time() - started_at

    assert elapsed < 0.05
    assert controller._pretest_sequence_in_progress is True
    _wait_until(qt_app, lambda: hal.get_line_state("Dev1/P0.0") is True, timeout=1.5)


def test_start_does_not_open_valves_when_flow_setpoint_fails(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})
    hal.fail_on = {"A"}

    window.pretest_view._handle_click(1)
    window.pretest_view._toggle_manual_mode(True)
    window.pretest_view._handle_start_clicked(True)
    _wait_until(qt_app, lambda: controller._pretest_sequence_in_progress is False)

    assert state.flow_setpoints_ready is False
    assert hal.get_line_state("Dev1/P0.0") is None
    assert hal.get_line_state("Dev2/P1.0") is None
    assert "setpoint 未确认" in window.pretest_view._flow_message_label.text()


def test_start_without_selected_valve_applies_rest_flow_only(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view._toggle_manual_mode(True)
    window.pretest_view._handle_start_clicked(True)
    _wait_until(qt_app, lambda: len(hal.flow_commands) >= 3)

    assert hal.flow_commands == [
        ("B", 1000.0, False),
        ("C", 500.0, False),
        ("A", 1000.0, True),
    ]
    assert hal.master_events == []


def test_stim_end_restores_rest_and_closes_master(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view.flow_sequence_requested.emit("stim_start", 60.0, 200.0, 40.0)
    window.pretest_view.flow_sequence_requested.emit("rest", 60.0, 200.0, 40.0)

    # Last three commands correspond to rest apply: B,C,A_comp
    last_three = hal.flow_commands[-3:]
    assert last_three[0][:2] == ("B", 200.0)
    assert last_three[1][:2] == ("C", 40.0)
    assert last_three[2][:2] == ("A", 100.0)  # A_comp = 60 + 40


def test_finish_closes_valve_but_keeps_channel_selected(qt_app):
    state, controller, window, hal = _build_flow_context(low_flow_threshold=0.1)
    controller.handle_telemetry({"airflow": 1.0, "connected": True, "timestamp": 1.0})

    window.pretest_view._handle_click(1)
    window.pretest_view._toggle_manual_mode(True)
    window.pretest_view._handle_start_clicked(True)
    _wait_until(qt_app, lambda: hal.get_line_state("Dev1/P0.0") is True)
    window.pretest_view._finish_delivery()
    _wait_until(qt_app, lambda: hal.get_line_state("Dev1/P0.0") is False)

    assert window.pretest_view.selected_valves() == [1]
    assert hal.get_line_state("Dev1/P0.0") is False
    assert window.pretest_view._buttons[1].button.isChecked() is True
