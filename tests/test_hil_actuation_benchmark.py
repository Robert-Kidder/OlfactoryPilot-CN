from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
)
from app.services import AnalogInputFrame, MockHAL
from scripts.hil_actuation_benchmark import (
    AIOnlyHal,
    LatencyTrace,
    Runtime,
    evaluate_full_close,
    production_safety_paths_succeeded,
    run_authorized_close_check,
    run_benchmark,
)


def test_hil_starts_ai_and_do_owners_at_high_priority(monkeypatch) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    starts = {}

    class FakeThread:
        def __init__(self, name: str, *, connected: bool = False) -> None:
            self.name = name
            self._connected = connected
            self._do_handed_off = False

        def start(self, priority=None) -> None:
            starts[self.name] = priority

        def isRunning(self) -> bool:
            return True

    runtime = Runtime.__new__(Runtime)
    runtime.actuation = FakeThread("actuation")
    runtime.flow = FakeThread("flow")
    runtime.hardware = FakeThread("hardware", connected=True)
    runtime.protocol_mode = False
    runtime._flow_restore_confirmed = True
    runtime.hal = SimpleNamespace(_ai_epoch=1)
    runtime.ingress = SimpleNamespace(
        read=lambda: (
            0,
            SimpleNamespace(unsafe_reason=lambda: None),
        ),
        clear_unsafe_latch=lambda: True,
    )
    runtime.pump = lambda: None
    now = 0.0

    def monotonic() -> float:
        nonlocal now
        now += 0.2
        return now

    monkeypatch.setattr(benchmark_module.time, "monotonic", monotonic)
    monkeypatch.setattr(benchmark_module.time, "sleep", lambda _seconds: None)

    runtime.start()

    assert starts == {
        "actuation": QThread.Priority.HighPriority,
        "flow": None,
        "hardware": QThread.Priority.HighPriority,
    }


def test_live_preflight_rejects_finite_but_unsafe_low_flow(monkeypatch) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    class FakeHal:
        def read_flow(self):
            return 0.0

        def release_serial_resources(self):
            return True

    monkeypatch.setattr(
        benchmark_module.HardwareCheckService,
        "from_config",
        lambda _config: SimpleNamespace(run_checks=lambda: ([], True)),
    )
    monkeypatch.setattr(
        benchmark_module,
        "enumerate_devices",
        lambda: [{"name": "Dev1"}, {"name": "Dev2"}],
    )
    monkeypatch.setattr(
        benchmark_module.RealHAL,
        "from_config",
        lambda _config: FakeHal(),
    )

    with pytest.raises(RuntimeError, match="MFC"):
        benchmark_module.preflight(
            {"low_flow_threshold": 0.2}, live=True, require_flow=True
        )


def test_close_only_live_preflight_does_not_require_mfc_flow(monkeypatch) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    class FakeHal:
        def read_flow(self):
            raise AssertionError("close-only preflight must not read MFC flow")

        def release_serial_resources(self):
            return True

    monkeypatch.setattr(
        benchmark_module.HardwareCheckService,
        "from_config",
        lambda _config: SimpleNamespace(run_checks=lambda: ([], True)),
    )
    monkeypatch.setattr(
        benchmark_module,
        "enumerate_devices",
        lambda: [{"name": "Dev1"}, {"name": "Dev2"}],
    )
    monkeypatch.setattr(
        benchmark_module.RealHAL,
        "from_config",
        lambda _config: FakeHal(),
    )

    _, _, airflow, _ = benchmark_module.preflight(
        {"low_flow_threshold": 0.2}, live=True, require_flow=False
    )

    assert airflow == 0.0


def test_latency_trace_is_bounded_and_flushes_only_after_collection(tmp_path) -> None:
    trace = LatencyTrace(enabled=True, run_id="diag", max_events=2)
    trace.begin_trial("trial-1")
    trace.record("first", at_ns=10)
    trace.record("second", at_ns=20)
    trace.end_trial()

    assert [event["event"] for event in trace.events] == [
        "trial_trace_begin",
        "first",
    ]
    path = tmp_path / "latency-trace.jsonl"
    trace.write_jsonl(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert rows[0]["schema"] == "story-3.4.hil-latency.v1"
    assert rows[-1] == {
        "schema": "story-3.4.hil-latency.v1",
        "run_id": "diag",
        "event": "trace_complete",
        "event_count": 2,
        "dropped_events": 2,
    }


def test_ai0_latency_trace_preserves_modeled_frame_time_and_marks_software_source() -> None:
    frame = AnalogInputFrame(
        timestamp=time.time(),
        ai0=0.25,
        ai6=0.0,
        monotonic_ns=300,
        ai_epoch=7,
        sample_sequence=42,
        origin_uncertainty_ns=11,
    )
    trace = LatencyTrace(enabled=True, run_id="diag")
    trace.begin_trial("trial-1")
    ai_hal = AIOnlyHal(
        SimpleNamespace(read_ai_frames=lambda _timestamp=None: [frame]),
        1000.0,
        monotonic_ns_clock=lambda: 200,
        latency_trace=trace,
    )

    ai_hal.set_ai0_software_stimulus(-0.9)
    stimulated = ai_hal.read_ai_frames()[0]

    assert stimulated.monotonic_ns == 300
    assert stimulated.ai_epoch == 7
    assert stimulated.sample_sequence == 42
    assert stimulated.ai0 == -0.9
    set_event = next(event for event in trace.events if event["event"] == "stimulus_set")
    read_event = next(
        event for event in trace.events if event["event"] == "ai_read_return"
    )
    assert set_event["external_signal"] is False
    assert set_event["at_ns"] == 200
    assert read_event["frame_monotonic_ns"] == [300]
    assert read_event["frame_origin_uncertainty_ns"] == [11]
    assert read_event["overridden_sequences"] == [42]


def _receipt(
    *,
    valve: int,
    device: str,
    line: str,
    action: ActuationAction = ActuationAction.CLOSE,
    category: ActuationCategory = ActuationCategory.SAFETY,
    result: ActuationResult = ActuationResult.SUCCESS,
    trial_id: str = "safety",
) -> ActuationReceipt:
    expected_ns = time.perf_counter_ns()
    command = ActuationCommand(
        command_id=f"{trial_id}-{valve}-{action.value}",
        execution_epoch=1,
        arm_epoch=1,
        sequence=valve + 1,
        trial_id=trial_id,
        trial_index=valve,
        valve=valve,
        action=action,
        category=category,
        expected_ns=expected_ns,
        duration_ns=None,
        wall_timestamp=time.time(),
        safety_generation=0,
        target_device=device,
        target_line=line,
    )
    actual_ns = expected_ns + 1_000_000
    return ActuationReceipt.from_write(
        command=command,
        started_ns=expected_ns,
        actual_ns=actual_ns,
        wall_timestamp=time.time(),
        result=result,
    )


def _close_runtime(receipts: list[ActuationReceipt]):
    steps = (
        SimpleNamespace(logical_valve=0, device="Dev1", line="port1/line0"),
        SimpleNamespace(logical_valve=1, device="Dev1", line="port0/line0"),
        SimpleNamespace(logical_valve=2, device="Dev1", line="port0/line1"),
    )
    hal = MockHAL()
    for step in steps:
        hal.write_digital(device=step.device, line=step.line, state=False)
    return SimpleNamespace(
        valves=SimpleNamespace(emergency_close_steps=lambda: steps),
        collector=SimpleNamespace(receipts=receipts),
        hal=hal,
        actuation=SimpleNamespace(
            protocol_state=SimpleNamespace(active_valve=None, possibly_open_valves=set())
        ),
    )


def test_full_close_evidence_requires_master_and_every_configured_odor_target() -> None:
    receipts = [
        _receipt(valve=0, device="Dev1", line="port1/line0"),
        _receipt(valve=1, device="Dev1", line="port0/line0"),
    ]
    runtime = _close_runtime(receipts)

    incomplete = evaluate_full_close(runtime, after_index=0, scenario="stop")
    assert incomplete["all_configured_targets_closed"] is False
    assert incomplete["missing_targets"] == [
        {"valve": 2, "device": "Dev1", "line": "port0/line1"}
    ]

    runtime.collector.receipts.append(
        _receipt(valve=2, device="Dev1", line="port0/line1")
    )
    complete = evaluate_full_close(runtime, after_index=0, scenario="stop")
    assert complete["expected_target_count"] == 3
    assert complete["successful_close_receipt_count"] == 3
    assert complete["all_configured_targets_closed"] is True


def test_production_safety_gate_rejects_any_partial_close_evidence() -> None:
    complete = {"all_configured_targets_closed": True}
    safety = {
        "stop": dict(complete),
        "low_flow": dict(complete),
        "severe": {**complete, "latched": True},
        "shutdown": {
            **complete,
            "result": "success",
            "valves_closed": True,
            "heaters_off": True,
        },
    }
    assert production_safety_paths_succeeded(safety) is True

    safety["low_flow"]["all_configured_targets_closed"] = False
    assert production_safety_paths_succeeded(safety) is False


def test_authorized_close_check_does_not_require_flow_or_ai_workers() -> None:
    receipts = [
        _receipt(valve=0, device="Dev1", line="port1/line0"),
        _receipt(valve=1, device="Dev1", line="port0/line0"),
        _receipt(valve=2, device="Dev1", line="port0/line1"),
    ]
    runtime = _close_runtime([])

    def emergency_close_all(timeout_ms):
        assert timeout_ms == 2000
        runtime.collector.receipts.extend(receipts)
        return True

    runtime.actuation.emergency_close_all = emergency_close_all

    result = run_authorized_close_check(runtime, timeout_ms=2000)

    assert result["success"] is True
    assert result["count"] == 3
    assert result["all_configured_targets_closed"] is True


def test_ai0_software_stimulus_preserves_hal_frame_identity() -> None:
    historical_frame = AnalogInputFrame(
        timestamp=time.time(),
        ai0=0.25,
        ai6=0.0,
        monotonic_ns=100,
        ai_epoch=7,
        sample_sequence=42,
    )
    current_frame = AnalogInputFrame(
        timestamp=time.time(),
        ai0=0.5,
        ai6=0.0,
        monotonic_ns=300,
        ai_epoch=7,
        sample_sequence=43,
    )
    hal = SimpleNamespace(
        read_ai_frames=lambda _timestamp=None: [historical_frame, current_frame]
    )
    ai_hal = AIOnlyHal(hal, 1000.0, monotonic_ns_clock=lambda: 200)

    ai_hal.set_ai0_software_stimulus(-0.9)
    historical, stimulated = ai_hal.read_ai_frames()

    assert historical is historical_frame
    assert historical.ai0 == 0.25
    assert stimulated.ai0 == -0.9
    assert stimulated.monotonic_ns == current_frame.monotonic_ns
    assert stimulated.ai_epoch == current_frame.ai_epoch
    assert stimulated.sample_sequence == current_frame.sample_sequence

    ai_hal.set_ai0_software_stimulus(None)
    assert ai_hal.read_ai_frames() == [historical_frame, current_frame]


def test_ai0_stimulus_clear_during_inflight_read_does_not_cover_returned_frames() -> None:
    entered = threading.Event()
    release = threading.Event()
    frame = AnalogInputFrame(
        timestamp=time.time(),
        ai0=0.25,
        ai6=0.0,
        monotonic_ns=300,
        ai_epoch=7,
        sample_sequence=42,
    )

    def read(_timestamp=None):
        entered.set()
        assert release.wait(1.0)
        return [frame]

    ai_hal = AIOnlyHal(
        SimpleNamespace(read_ai_frames=read),
        1000.0,
        monotonic_ns_clock=lambda: 200,
    )
    ai_hal.set_ai0_software_stimulus(-0.9)
    result = []
    thread = threading.Thread(target=lambda: result.extend(ai_hal.read_ai_frames()))
    thread.start()
    assert entered.wait(1.0)
    ai_hal.set_ai0_software_stimulus(None)
    release.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert result == [frame]


def test_ai0_stimulus_started_during_inflight_read_does_not_rewrite_old_frame() -> None:
    entered = threading.Event()
    release = threading.Event()
    frame = AnalogInputFrame(
        timestamp=time.time(),
        ai0=0.25,
        ai6=0.0,
        monotonic_ns=100,
        ai_epoch=7,
        sample_sequence=42,
    )

    def read(_timestamp=None):
        entered.set()
        assert release.wait(1.0)
        return [frame]

    ai_hal = AIOnlyHal(
        SimpleNamespace(read_ai_frames=read),
        1000.0,
        monotonic_ns_clock=lambda: 200,
    )
    result = []
    thread = threading.Thread(target=lambda: result.extend(ai_hal.read_ai_frames()))
    thread.start()
    assert entered.wait(1.0)
    ai_hal.set_ai0_software_stimulus(-0.9)
    release.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert result == [frame]


class _FakeCollector:
    def __init__(self) -> None:
        self.receipts: list[ActuationReceipt] = []

    def wait_for(self, predicate, _timeout_s, _pump):
        return next(item for item in self.receipts if predicate(item))


def test_benchmark_normal_actions_come_from_protocol_document() -> None:
    collector = _FakeCollector()
    captured_documents = []
    direct_categories = []

    def command(**kwargs):
        direct_categories.append(kwargs["category"])
        return SimpleNamespace(**kwargs)

    def trigger(*, label):
        valve = int(label.rsplit("v", 1)[1])
        opened = _receipt(
            valve=valve,
            device="Dev1",
            line=f"port0/line{valve}",
            action=ActuationAction.OPEN,
            category=ActuationCategory.NORMAL,
            trial_id=label,
        )
        closed = _receipt(
            valve=valve,
            device="Dev1",
            line=f"port0/line{valve}",
            action=ActuationAction.CLOSE,
            category=ActuationCategory.NORMAL,
            trial_id=label,
        )
        collector.receipts.extend((opened, closed))
        return opened

    runtime = SimpleNamespace(
        close_everything=lambda _label: [],
        valves=SimpleNamespace(resolve_target=lambda _valve: ("Dev1", "port1/line0")),
        command=command,
        submit_and_wait=lambda _command: None,
        wait_ms=lambda _milliseconds: None,
        start_protocol_document=captured_documents.append,
        trigger_current_trial_via_ai0=trigger,
        wait_status=lambda _statuses: None,
        collector=collector,
        pump=lambda: None,
        metrics=SimpleNamespace(config=SimpleNamespace(window_size=50, min_samples=1)),
    )
    args = SimpleNamespace(
        cycles=3,
        valves=[1, 9, 13],
        duration_ms=100.0,
        inter_trial_ms=250.0,
    )

    result = run_benchmark(runtime, args)

    assert direct_categories == [ActuationCategory.WARMUP]
    assert len(captured_documents) == 1
    assert len(captured_documents[0].trials) == 3
    assert all(trial.trigger.value == "manual" for trial in captured_documents[0].trials)
    assert result["open"]["count"] == 3
    assert result["close"]["count"] == 3
