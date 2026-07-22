from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

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
    breaths = []
    worker.ttl_pulse.connect(pulses.append)
    worker.breath_samples.connect(breaths.append)
    worker.arm_ttl(arm_epoch=4)

    for index, level in enumerate([0.0, 3.0, 3.1, 3.2]):
        hal.set_ttl_level(level)
        worker._emit_ai_frame(index / 1000)

    assert len(pulses) == 1
    assert (pulses[0].timestamp, pulses[0].arm_epoch, pulses[0].sequence) == (0.001, 4, 1)
    assert pulses[0].monotonic_ns > 0
    assert breaths
    assert all(isinstance(item.samples[0].value, float) for item in breaths)


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


def test_worker_latches_continuous_read_errors_and_marks_ttl_unready(monkeypatch, qtbot) -> None:
    import app.workers.hardware_worker as hardware_worker_module

    class FailingHAL(MockHAL):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0
            self.reset_count = 0

        def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
            self.read_count += 1
            raise RuntimeError("USB disconnected")

        def reset_ai_input(self) -> None:
            self.reset_count += 1

    monotonic = {"value": 0.0}
    monkeypatch.setattr(hardware_worker_module.time, "monotonic", lambda: monotonic["value"])
    hal = FailingHAL()
    worker = HardwareWorker(ttl_poll_hz=1000, hal=hal, simulation=True)
    errors: list[str] = []
    readiness: list[bool] = []
    worker.ttl_input_error.connect(errors.append)
    worker.ttl_readiness_changed.connect(readiness.append)
    worker.arm_ttl(arm_epoch=1)

    for index in range(1000):
        worker._emit_ai_frame(index / 1000)

    assert hal.read_count == 1
    assert hal.reset_count == 1
    assert len(errors) == 1
    assert readiness == [False]
    assert worker.ttl_input_ready is False


def test_worker_retries_after_backoff_and_restores_ttl_readiness(monkeypatch, qtbot) -> None:
    import app.workers.hardware_worker as hardware_worker_module

    class RecoveringHAL(MockHAL):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
            self.read_count += 1
            if self.read_count == 1:
                raise RuntimeError("temporary read failure")
            return super().read_ai_frame(timestamp)

    monotonic = {"value": 0.0}
    monkeypatch.setattr(hardware_worker_module.time, "monotonic", lambda: monotonic["value"])
    hal = RecoveringHAL()
    worker = HardwareWorker(ttl_poll_hz=1000, hal=hal, simulation=True)
    errors: list[str] = []
    readiness: list[bool] = []
    worker.ttl_input_error.connect(errors.append)
    worker.ttl_readiness_changed.connect(readiness.append)

    worker._emit_ai_frame(0.0)
    monotonic["value"] = 0.5
    worker._emit_ai_frame(0.5)
    monotonic["value"] = 1.0
    worker._emit_ai_frame(1.0)

    assert hal.read_count == 2
    assert len(errors) == 1
    assert readiness == [False, True]
    assert worker.ttl_input_ready is True


def test_worker_does_not_publish_recovery_until_entire_frame_is_valid(monkeypatch, qtbot) -> None:
    import app.workers.hardware_worker as hardware_worker_module

    class InvalidThenRecoveringHAL(MockHAL):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
            self.read_count += 1
            if self.read_count == 1:
                raise RuntimeError("temporary read failure")
            if self.read_count == 2:
                return AnalogInputFrame(timestamp=float(timestamp or 0.0), ai0=math.nan, ai6=0.0)
            return super().read_ai_frame(timestamp)

    monotonic = {"value": 0.0}
    monkeypatch.setattr(hardware_worker_module.time, "monotonic", lambda: monotonic["value"])
    hal = InvalidThenRecoveringHAL()
    worker = HardwareWorker(ttl_poll_hz=1000, hal=hal, simulation=True)
    readiness: list[bool] = []
    worker.ttl_readiness_changed.connect(readiness.append)

    worker._emit_ai_frame(0.0)
    monotonic["value"] = 1.0
    worker._emit_ai_frame(1.0)

    assert readiness == [False]
    assert worker.ttl_input_ready is False

    monotonic["value"] = 2.0
    worker._emit_ai_frame(2.0)

    assert readiness == [False, True]
    assert worker.ttl_input_ready is True


def test_worker_drains_ai_batch_and_emits_downsampled_breath_values(qtbot) -> None:
    class BatchHAL(MockHAL):
        def read_ai_frames(self, timestamp: float | None = None) -> list[AnalogInputFrame]:
            return [
                AnalogInputFrame(
                    timestamp=index / 1000,
                    ai0=float(index),
                    ai6=0.0,
                    monotonic_ns=(index + 1) * 1_000_000,
                    ai_epoch=1,
                    sample_sequence=index,
                )
                for index in range(25)
            ]

    worker = HardwareWorker(breath_hz=100, ttl_poll_hz=1000, hal=BatchHAL(), simulation=True)
    breaths = []
    worker.breath_samples.connect(breaths.append)

    worker._emit_ai_frame(0.024)

    assert [[sample.value for sample in batch.samples] for batch in breaths] == [[0.0, 10.0, 20.0]]
    assert [sample.monotonic_ns for sample in breaths[0].samples] == [1_000_000, 11_000_000, 21_000_000]
    assert worker._ai_sample_count == 25


def test_worker_invalid_poll_rate_uses_safe_ttl_default() -> None:
    worker = HardwareWorker(ttl_poll_hz=math.nan, hal=MockHAL(), simulation=True)

    assert worker.ttl_service.config.poll_hz == 1000
    assert worker.ttl_interval_ms == 1


def test_real_hal_constructor_defers_ai_task_to_hardware_owner(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    created = []

    class FakeTask:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)

    hal = real_hal_module.RealHAL(serial_port="COM1")

    assert created == []
    assert hal._ai_task is None


def test_real_hal_creates_one_ai_task_with_ai0_and_ai6(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def __init__(self) -> None:
            self.names: list[str] = []
            self.terminal_configs: list[str | None] = []

        def add_ai_voltage_chan(self, name: str, *, terminal_config=None) -> None:
            self.names.append(name)
            self.terminal_configs.append(terminal_config)

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
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        ai0_channel="Dev1/ai0",
        ttl_input_channel="Dev1/ai6",
        ttl_poll_hz=1000,
        serial_port="COM1",
        monotonic_ns_clock=iter((1_000, 3_000)).__next__,
        wall_clock=lambda: 2.0,
    )

    frame = hal.read_ai_frame(timestamp=2.0)

    assert len(tasks) == 1
    assert tasks[0].ai_channels.names == ["Dev1/ai0", "Dev1/ai6"]
    assert tasks[0].ai_channels.terminal_configs == ["RSE", "RSE"]
    assert frame == AnalogInputFrame(
        timestamp=2.0,
        ai0=0.25,
        ai6=3.3,
        monotonic_ns=2_000,
        ai_epoch=1,
        sample_sequence=0,
        origin_uncertainty_ns=1_000,
    )
    assert hal.ttl_input_ready is True


def test_real_hal_shared_ai_task_uses_continuous_sampling_for_long_runs(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def add_ai_voltage_chan(self, name: str, *, terminal_config=None) -> None:
            return None

    class FakeTiming:
        def __init__(self) -> None:
            self.config: dict = {}

        def cfg_samp_clk_timing(self, **kwargs) -> None:
            self.config = kwargs

    class FakeTask:
        def __init__(self) -> None:
            self.ai_channels = FakeChannels()
            self.timing = FakeTiming()
            self.read_count = 0
            tasks.append(self)

        def read(self):
            self.read_count += 1
            if self.read_count > 1000 and self.timing.config.get("sample_mode") != "CONTINUOUS":
                raise RuntimeError("finite acquisition exhausted")
            return [0.25, 3.3]

        def close(self) -> None:
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "AcquisitionType", SimpleNamespace(CONTINUOUS="CONTINUOUS"))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        ttl_poll_hz=1000,
        serial_port="COM1",
        monotonic_ns_clock=iter((1_000, 3_000)).__next__,
        wall_clock=lambda: 1.998,
    )

    for index in range(1500):
        hal.read_ai_frame(timestamp=index / 1000)

    assert len(tasks) == 1
    assert tasks[0].timing.config == {
        "rate": 1000,
        "sample_mode": "CONTINUOUS",
        "samps_per_chan": 1000,
    }


def test_real_hal_drains_all_buffered_ai_samples_with_sample_clock_timestamps(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def add_ai_voltage_chan(self, name: str, *, terminal_config=None) -> None:
            return None

    class FakeTask:
        def __init__(self) -> None:
            self.ai_channels = FakeChannels()
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)
            self.requested_samples = None
            tasks.append(self)

        def read(self, number_of_samples_per_channel=None):
            self.requested_samples = number_of_samples_per_channel
            return [[0.1, 0.2, 0.3], [0.0, 3.0, 3.1]]

        def close(self) -> None:
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "AcquisitionType", SimpleNamespace(CONTINUOUS="CONTINUOUS"))
    monkeypatch.setattr(real_hal_module, "READ_ALL_AVAILABLE", "ALL")
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        ttl_poll_hz=1000,
        serial_port="COM1",
        monotonic_ns_clock=iter((1_000, 3_000)).__next__,
        wall_clock=lambda: 1.998,
    )

    frames = hal.read_ai_frames(timestamp=2.0)

    assert tasks[0].requested_samples == "ALL"
    assert [frame.timestamp for frame in frames] == pytest.approx([1.998, 1.999, 2.0])
    assert [(frame.ai0, frame.ai6) for frame in frames] == [
        (0.1, 0.0),
        (0.2, 3.0),
        (0.3, 3.1),
    ]


def test_real_hal_ai6_failure_closes_partial_task_then_degrades_to_ai0_only(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def __init__(self, fail_ai6: bool) -> None:
            self.fail_ai6 = fail_ai6
            self.names: list[str] = []
            self.terminal_configs: list[str | None] = []

        def add_ai_voltage_chan(self, name: str, *, terminal_config=None) -> None:
            self.terminal_configs.append(terminal_config)
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
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)

    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        monotonic_ns_clock=iter((1_000, 3_000)).__next__,
        wall_clock=lambda: 2.0,
    )
    frame = hal.read_ai_frame(timestamp=2.0)

    assert len(tasks) == 2
    assert tasks[0].closed is True
    assert tasks[1].closed is False
    assert tasks[1].ai_channels.names == ["Dev1/ai0"]
    assert tasks[0].ai_channels.terminal_configs == ["RSE", "RSE"]
    assert tasks[1].ai_channels.terminal_configs == ["RSE"]
    assert frame == AnalogInputFrame(
        timestamp=2.0,
        ai0=0.25,
        ai6=None,
        monotonic_ns=2_000,
        ai_epoch=1,
        sample_sequence=0,
        origin_uncertainty_ns=1_000,
    )
    assert hal.ttl_input_ready is False
