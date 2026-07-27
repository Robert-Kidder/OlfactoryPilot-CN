from __future__ import annotations

import math
import threading
from types import SimpleNamespace

import pytest

from app.services.hal import AnalogInputFrame, HalInterface
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlPulse
from app.workers.actuation_worker import ActuationInterlockIngress, InterlockSnapshot
from app.workers.hardware_worker import HardwareWorker


def test_ttl_control_ack_reaches_actuation_sink_without_ui_event_loop() -> None:
    class Sink:
        def __init__(self) -> None:
            self.ack = None
            self.ack_thread = None
            self.ready = threading.Event()

        def consume_ttl_arm_ack(self, *, arm_epoch, armed) -> None:
            self.ack = (arm_epoch, armed)
            self.ack_thread = threading.get_ident()
            self.ready.set()

        def post_readiness_update(self, **_kwargs) -> None:
            return None

        def post_ai_batch(self, _batch) -> None:
            return None

        def post_ttl_pulse(self, _pulse) -> None:
            return None

    sink = Sink()
    worker = HardwareWorker(ttl_poll_hz=1000, hal=MockHAL(), simulation=True)
    worker.set_actuation_sink(sink)
    caller_thread = threading.get_ident()
    worker.start()
    try:
        worker.post_ttl_arm(arm_epoch=17)
        assert sink.ready.wait(1.0)
        assert sink.ack == (17, True)
        assert sink.ack_thread != caller_thread
        assert worker.ttl_service.arm_epoch == 17
        worker.post_ttl_disarm()
        for _ in range(1000):
            if not worker.ttl_service.is_armed:
                break
            threading.Event().wait(0.001)
        assert worker.ttl_service.is_armed is False
    finally:
        assert worker.stop()


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


def test_worker_uses_python_high_resolution_sleep_for_ai_polling(monkeypatch) -> None:
    import app.workers.hardware_worker as hardware_worker_module

    worker = HardwareWorker(ttl_poll_hz=1000, hal=MockHAL(), simulation=True)
    sleeps = []
    worker._run_self_check = lambda: None
    worker._emit_ai_frame = lambda _timestamp: setattr(worker, "_running", False)
    worker._emit_telemetry = lambda _timestamp: None
    monkeypatch.setattr(
        hardware_worker_module.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    worker.run()

    assert sleeps == [0.001]
    assert worker._ai_release_attempted is True
    assert worker._ai_release_success is True


def test_worker_syncs_ttl_readiness_after_lazy_ai_task_start(qtbot) -> None:
    class LazyAIHal(MockHAL):
        def __init__(self) -> None:
            super().__init__()
            self._lazy_ttl_ready = False

        @property
        def ttl_input_ready(self) -> bool:
            return self._lazy_ttl_ready

        def read_ai_frames(self, timestamp=None):
            self._lazy_ttl_ready = True
            return [
                AnalogInputFrame(
                    timestamp=1.0,
                    ai0=0.0,
                    ai6=0.0,
                    monotonic_ns=1_000_000,
                    ai_epoch=1,
                    sample_sequence=0,
                )
            ]

    hal = LazyAIHal()
    worker = HardwareWorker(hal=hal, simulation=True)
    readiness = []
    worker.ttl_readiness_changed.connect(readiness.append)
    assert worker.ttl_input_ready is False

    worker._emit_ai_frame(1.0)

    assert worker.ttl_input_ready is True
    assert readiness == [True]


def test_airflow_publication_cannot_restore_hardware_ready_during_ai_fault() -> None:
    hal = MockHAL()
    worker = HardwareWorker(hal=hal, simulation=True)
    worker._connected = True
    worker._ai_error_latched = True
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=False,
            flow_setpoints_ready=True,
            safety_state="SAFE",
            ttl_input_ready=False,
            has_protocol=True,
            device_lease="protocol",
        )
    )
    worker.set_actuation_sink(None, interlock_ingress=ingress)

    worker._publish_interlock(1.0, 1.0)

    _, snapshot, unsafe_latched = ingress.read()
    assert snapshot.connected is True
    assert snapshot.hardware_ready is False
    assert unsafe_latched is True


def test_worker_publishes_low_flow_to_actuation_before_ui_signal(qtbot) -> None:
    hal = MockHAL(base_flow_sccm=0.1)
    worker = HardwareWorker(hal=hal, simulation=True)
    worker._connected = True
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=True,
            safety_state="SAFE",
            ttl_input_ready=True,
            has_protocol=True,
            device_lease="protocol",
        )
    )
    events = []

    class Sink:
        def post_readiness_update(self, *, readiness, timestamp=None):
            events.append(("actuation", readiness.safety_state, timestamp))

    worker.set_actuation_sink(Sink(), interlock_ingress=ingress)
    worker.telemetry_ready.connect(
        lambda payload: events.append(("ui", payload["safety_state"], payload["timestamp"]))
    )

    worker.consume_airflow_sample(0.1, 10.0)
    events.clear()
    worker._emit_telemetry(10.0)

    assert events == [
        ("actuation", "LOW_FLOW", 10.0),
        ("ui", "LOW_FLOW", 10.0),
    ]
    assert ingress.read()[1].safety_state == "LOW_FLOW"


def test_worker_marks_missing_stale_and_error_flow_samples_data_stale(qtbot) -> None:
    hal = MockHAL(base_flow_sccm=100.0)
    worker = HardwareWorker(hal=hal, simulation=True)
    worker._connected = True
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(connected=True, hardware_ready=True, safety_state="SAFE")
    )
    states = []

    class Sink:
        def post_readiness_update(self, *, readiness, timestamp=None):
            states.append(readiness.safety_state)

    worker.set_actuation_sink(Sink(), interlock_ingress=ingress)

    worker._emit_telemetry(10.0)
    assert states[-1] == "DATA_STALE"

    worker.consume_airflow_sample(100.0, 10.0)
    assert states[-1] == "SAFE"
    worker._emit_telemetry(11.1)
    assert states[-1] == "DATA_STALE"

    worker.consume_airflow_sample(100.0, 12.0, error="serial disconnected")
    assert states[-1] == "DATA_STALE"
    assert math.isnan(worker._read_flow(12.0))


def test_hardware_worker_rejects_legacy_do_paths() -> None:
    hal = MockHAL()
    worker = HardwareWorker(hal=hal, simulation=True)

    assert worker.write_digital(device="Dev1", line="P0.0", state=True) is False
    assert worker.close_all_channels() is False
    assert hal._digital_state == {}


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


def test_real_hal_retains_ai_task_when_driver_close_fails(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    class FailingTask:
        def __init__(self) -> None:
            self.fail_close = True

        def close(self) -> None:
            if self.fail_close:
                raise RuntimeError("AI reservation remains")

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1")
    task = FailingTask()
    hal._ai_task = task
    hal._ttl_input_ready = True

    assert hal.reset_ai_input() is False
    assert hal._ai_task is task
    assert hal._ai_release_failed is True
    with pytest.raises(RuntimeError, match="release failed"):
        hal.read_ai_frames()

    task.fail_close = False
    assert hal.reset_ai_input() is True
    assert hal._ai_task is None
    assert hal._ai_release_failed is False


def test_hardware_worker_reports_real_ai_close_failure(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    class FailingTask:
        def close(self) -> None:
            raise RuntimeError("AI close failed")

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1")
    hal._ai_task = FailingTask()
    worker = HardwareWorker(hal=hal)

    assert worker.release_ai_resources() is False
    assert hal._ai_task is not None
    assert worker._ai_release_attempted is True
    assert worker._ai_release_success is False


def test_hardware_worker_release_is_successful_when_no_ai_task_exists() -> None:
    worker = HardwareWorker(hal=MockHAL())

    assert worker.release_ai_resources() is True


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
