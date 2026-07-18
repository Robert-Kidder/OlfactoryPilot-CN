from __future__ import annotations

import math
from types import SimpleNamespace

from app.services.hal import AnalogInputFrame, HalInterface
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlPulse
from app.workers.hardware_worker import HardwareWorker


def test_mock_hal_exposes_shared_ai_frame_and_deterministic_ttl_level() -> None:
    hal = MockHAL()
    hal.set_ttl_level(3.3)

    frame = hal.read_ai_frame(timestamp=12.5)

    assert isinstance(hal, HalInterface)
    assert frame.timestamp == 12.5
    assert isinstance(frame.ai0, float)
    assert frame.ai6 == 3.3
    assert hal.ttl_input_ready is True


def test_worker_emits_one_frozen_pulse_for_sustained_high(qtbot) -> None:
    hal = MockHAL()
    worker = HardwareWorker(
        telemetry_hz=5,
        breath_hz=100,
        ttl_poll_hz=1000,
        ttl_config={"ttl_debounce_ms": 0},
        hal=hal,
        simulation=True,
    )
    pulses: list[TtlPulse] = []
    breaths: list[tuple[list[float], float]] = []
    worker.ttl_pulse.connect(pulses.append)
    worker.breath_samples.connect(lambda values, timestamp: breaths.append((values, timestamp)))
    worker.arm_ttl(arm_epoch=4)

    for index, level in enumerate([0.0, 3.0, 3.1, 3.2]):
        hal.set_ttl_level(level)
        worker._emit_ai_frame(index / 1000)

    assert pulses == [TtlPulse(timestamp=0.001, arm_epoch=4, sequence=1)]
    assert breaths
    assert all(isinstance(item[0][0], float) for item in breaths)


def test_worker_reports_shared_ai_read_error_without_fabricating_pulse(qtbot) -> None:
    class FailingHAL(MockHAL):
        def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
            raise RuntimeError("USB disconnected")

    worker = HardwareWorker(ttl_poll_hz=1000, hal=FailingHAL(), simulation=True)
    pulses: list[TtlPulse] = []
    errors: list[str] = []
    worker.ttl_pulse.connect(pulses.append)
    worker.ttl_input_error.connect(errors.append)
    worker.arm_ttl(arm_epoch=1)

    worker._emit_ai_frame(1.0)

    assert pulses == []
    assert errors and "TTL/共享 AI 读取失败" in errors[-1]


def test_worker_invalid_poll_rate_uses_safe_ttl_default() -> None:
    worker = HardwareWorker(ttl_poll_hz=math.nan, hal=MockHAL(), simulation=True)

    assert worker.ttl_service.config.poll_hz == 1000
    assert worker.ttl_interval_ms == 1


def test_real_hal_creates_one_ai_task_with_ai0_and_ai6(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def __init__(self) -> None:
            self.names: list[str] = []

        def add_ai_voltage_chan(self, name: str) -> None:
            self.names.append(name)

    class FakeTask:
        def __init__(self) -> None:
            self.ai_channels = FakeChannels()
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)
            self.closed = False
            tasks.append(self)

        def read(self):
            return [0.25, 3.3]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        ai0_channel="Dev1/ai0",
        ttl_input_channel="Dev1/ai6",
        ttl_poll_hz=1000,
        serial_port="COM1",
    )

    frame = hal.read_ai_frame(timestamp=2.0)

    assert len(tasks) == 1
    assert tasks[0].ai_channels.names == ["Dev1/ai0", "Dev1/ai6"]
    assert frame == AnalogInputFrame(timestamp=2.0, ai0=0.25, ai6=3.3)
    assert hal.ttl_input_ready is True


def test_real_hal_ai6_failure_closes_partial_task_then_degrades_to_ai0_only(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def __init__(self, fail_ai6: bool) -> None:
            self.fail_ai6 = fail_ai6
            self.names: list[str] = []

        def add_ai_voltage_chan(self, name: str) -> None:
            if self.fail_ai6 and name.endswith("ai6"):
                raise RuntimeError("AI6 unavailable")
            self.names.append(name)

    class FakeTask:
        def __init__(self) -> None:
            self.ai_channels = FakeChannels(fail_ai6=not tasks)
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)
            self.closed = False
            tasks.append(self)

        def read(self):
            return 0.25

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)

    hal = real_hal_module.RealHAL(serial_port="COM1")
    frame = hal.read_ai_frame(timestamp=2.0)

    assert len(tasks) == 2
    assert tasks[0].closed is True
    assert tasks[1].closed is False
    assert tasks[1].ai_channels.names == ["Dev1/ai0"]
    assert frame == AnalogInputFrame(timestamp=2.0, ai0=0.25, ai6=None)
    assert hal.ttl_input_ready is False
