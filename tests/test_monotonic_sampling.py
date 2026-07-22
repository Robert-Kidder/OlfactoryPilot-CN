from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ProtocolDocument, ProtocolExecutionReadiness, ProtocolTrial, TriggerMode
from app.services.gating_service import GatingService, GatingState
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.protocol_executor import ProtocolExecutor
from app.services.ttl_trigger_service import TtlTriggerConfig, TtlTriggerService
from app.workers.hardware_worker import HardwareWorker


def test_gating_transition_keeps_middle_sample_monotonic_identity() -> None:
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    batch = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(10.0, 0.0, None, monotonic_ns=1_000, ai_epoch=2, sample_sequence=4),
            AnalogInputFrame(10.01, -0.6, None, monotonic_ns=2_000, ai_epoch=2, sample_sequence=5),
            AnalogInputFrame(10.02, -0.7, None, monotonic_ns=3_000, ai_epoch=2, sample_sequence=6),
        )
    )

    transitions = service.process_sample_batch(batch, safety_state="SAFE")

    assert len(transitions) == 1
    assert transitions[0].state == GatingState.EXHALE
    assert transitions[0].timestamp == 10.01
    assert transitions[0].monotonic_ns == 2_000
    assert transitions[0].ai_epoch == 2
    assert transitions[0].sample_sequence == 5
    with pytest.raises(FrozenInstanceError):
        transitions[0].monotonic_ns = 99  # type: ignore[misc]


def test_ttl_pulse_preserves_source_sample_monotonic_time() -> None:
    service = TtlTriggerService(TtlTriggerConfig(debounce_ms=0))
    service.arm(arm_epoch=8)
    service.process_sample(0.0, timestamp=10.0, monotonic_ns=1_000)

    pulse = service.process_sample(3.0, timestamp=10.1, monotonic_ns=2_000)

    assert pulse is not None
    assert pulse.timestamp == 10.1
    assert pulse.monotonic_ns == 2_000
    assert pulse.arm_epoch == 8


def test_real_hal_ai_origin_sequence_and_epoch_are_not_reanchored(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []
    monotonic_values = iter((1_000_000, 1_004_000, 2_000_000, 2_006_000))

    class FakeChannels:
        def add_ai_voltage_chan(self, name: str, *, terminal_config=None) -> None:
            return None

    class FakeTask:
        def __init__(self) -> None:
            self.ai_channels = FakeChannels()
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)
            self.started = False
            self.closed = False
            self.read_count = 0
            tasks.append(self)

        def start(self) -> None:
            self.started = True

        def read(self, number_of_samples_per_channel=None):
            self.read_count += 1
            if self.read_count == 1:
                return [[0.1, 0.2, 0.3], [0.0, 0.0, 0.0]]
            return [[0.4], [0.0]]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "AcquisitionType", SimpleNamespace(CONTINUOUS="CONTINUOUS"))
    monkeypatch.setattr(real_hal_module, "READ_ALL_AVAILABLE", "ALL")
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)

    hal = real_hal_module.RealHAL(
        ttl_poll_hz=1000,
        serial_port="COM1",
        monotonic_ns_clock=lambda: next(monotonic_values),
        wall_clock=lambda: 100.0,
    )
    first = hal.read_ai_frames(timestamp=999.0)
    second = hal.read_ai_frames(timestamp=2000.0)

    assert tasks[0].started is True
    assert [frame.monotonic_ns for frame in first] == [1_002_000, 2_002_000, 3_002_000]
    assert [frame.sample_sequence for frame in first] == [0, 1, 2]
    assert second[0].monotonic_ns == 4_002_000
    assert second[0].sample_sequence == 3
    assert all(frame.ai_epoch == 1 for frame in [*first, *second])
    assert all(frame.origin_uncertainty_ns == 2_000 for frame in first)
    assert [frame.timestamp for frame in first] == pytest.approx([100.0, 100.001, 100.002])

    hal.reset_ai_input()
    recreated = hal.read_ai_frames()
    assert recreated[0].ai_epoch == 2
    assert recreated[0].sample_sequence == 0


def test_hardware_worker_emits_frozen_breath_batch_with_sample_identity() -> None:
    class BatchHAL:
        ttl_input_ready = True

        def read_ai_frames(self, timestamp=None):
            return [
                AnalogInputFrame(10.0, 0.1, 0.0, monotonic_ns=1_000, ai_epoch=1, sample_sequence=0),
                AnalogInputFrame(10.001, 0.2, 0.0, monotonic_ns=2_000, ai_epoch=1, sample_sequence=1),
            ]

        def reset_ai_input(self):
            return None

    worker = HardwareWorker(hal=BatchHAL(), breath_hz=1000, ttl_poll_hz=1000)
    batches = []
    worker.breath_samples.connect(batches.append)

    worker._emit_ai_frame(100.0)

    assert len(batches) == 1
    assert isinstance(batches[0], BreathSampleBatch)
    assert [sample.monotonic_ns for sample in batches[0].samples] == [1_000, 2_000]
    with pytest.raises(FrozenInstanceError):
        batches[0].samples = ()  # type: ignore[misc]


def _waiting_executor() -> ProtocolExecutor:
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (True, "ok"),
    )
    document = ProtocolDocument(
        source_path=Path("mono.csv"),
        source_name="mono.csv",
        trials=[ProtocolTrial("t1", 0, 100, 1, TriggerMode.MANUAL)],
    )
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", False)
    executor.start(document, readiness=readiness, timestamp=10.0)
    executor.accept_trigger(TriggerMode.MANUAL, readiness=readiness, timestamp=10.01)
    return executor


def test_executor_expected_open_uses_first_exhale_sample_in_middle_of_batch() -> None:
    executor = _waiting_executor()
    batch = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(10.02, 0.0, monotonic_ns=1_000, ai_epoch=1, sample_sequence=1),
            AnalogInputFrame(10.03, -0.6, monotonic_ns=2_000, ai_epoch=1, sample_sequence=2),
            AnalogInputFrame(10.04, -0.7, monotonic_ns=3_000, ai_epoch=1, sample_sequence=3),
        )
    )

    result = executor.process_breath_samples(batch, safety_state="SAFE")

    assert result.state.expected_open_ns == 2_000
    assert result.events[-1].trigger_reason == "exhale_transition"


def test_executor_exhale_fallback_uses_last_valid_sample_with_distinct_reason() -> None:
    executor = _waiting_executor()
    executor.gating_service.current_state = GatingState.EXHALE
    batch = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(10.02, -0.6, monotonic_ns=2_000, ai_epoch=1, sample_sequence=2),
            AnalogInputFrame(10.03, -0.7, monotonic_ns=3_000, ai_epoch=1, sample_sequence=3),
        )
    )

    result = executor.process_breath_samples(batch, safety_state="SAFE")

    assert result.state.expected_open_ns == 3_000
    assert result.events[-1].trigger_reason == "exhale_state_fallback"
