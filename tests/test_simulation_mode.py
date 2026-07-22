from __future__ import annotations

from unittest.mock import MagicMock

from app.main import DEFAULT_CONFIG, build_application, parse_args
from app.services import MockHAL
from app.workers import HardwareWorker


def test_parse_args_supports_simulation_flag():
    args = parse_args(["--simulation"])
    assert getattr(args, "simulation", False) is True

    args_default = parse_args([])
    assert getattr(args_default, "simulation", False) is False


def test_build_application_marks_simulation_title_and_state(qt_app):
    _, window = build_application(DEFAULT_CONFIG, start_worker=False, simulation=True)
    assert "[模拟模式]" in window.windowTitle()
    assert window.controller.state.simulation_mode is True


def test_mock_hal_generates_signal_and_tracks_state():
    hal = MockHAL()

    first = hal.read_ai0(0.0)
    second = hal.read_ai0(0.25)
    assert first != second
    assert -1.0 <= first <= 1.0
    assert -1.0 <= second <= 1.0

    hal.write_digital(device="Dev1", line="P0.0", state=True)
    hal.write_digital(device="Dev1", line="P0.0", state=False)
    assert hal.get_line_state("Dev1/P0.0") is False

    hal.set_flow(750.0)
    assert hal.read_flow() == 750.0

    results, ready = hal.self_check()
    assert ready is True
    assert all(item.status == "PASS" for item in results)


def test_worker_uses_hal_for_signal_generation(qt_app):
    hal = MagicMock()
    hal.read_ai0.return_value = 0.42
    hal.read_flow.return_value = 123.0
    hal.write_digital.return_value = True
    hal.set_flow.return_value = True
    hal.close_all.return_value = True
    hal.stop_heaters.return_value = True
    hal.flush_logs.return_value = None
    hal.self_check.return_value = ([], True)

    worker = HardwareWorker(telemetry_hz=10, breath_hz=10, hal=hal, simulation=True)

    captured = []
    worker.breath_samples.connect(lambda batch: captured.append(batch.samples[0].value))

    worker._emit_breath_sample(1.23)
    assert captured == [0.42]
    hal.read_ai0.assert_called_once_with(1.23)

    worker.write_digital(device=None, line="L0", state=True)
    hal.write_digital.assert_called_once_with(device=None, line="L0", state=True)

    assert worker._read_flow() == 123.0
    hal.read_flow.assert_called_once()

    worker._run_self_check()
    hal.self_check.assert_called_once()
    assert worker.is_connected is True
