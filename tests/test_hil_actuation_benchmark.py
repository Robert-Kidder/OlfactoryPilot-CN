from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationQualitySnapshot,
    ActuationReceipt,
    ActuationResult,
    ActuationStreamSnapshot,
    ProtocolDocument,
    ProtocolTrial,
    TriggerMode,
)
from app.models.protocol_execution import ProtocolExecutionStatus
from app.services import AnalogInputFrame, MockHAL
from scripts.hil_actuation_benchmark import (
    AIOnlyHal,
    LatencyTrace,
    ReceiptCollector,
    Runtime,
    evaluate_full_close,
    production_safety_paths_succeeded,
    run_authorized_close_check,
    run_benchmark,
)

FAILED_STORY35_HIL_RUN = (
    Path(__file__).resolve().parents[1]
    / "logs"
    / "benchmarks"
    / "story-3-5-20260730-183616-live"
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


def test_latency_trace_keeps_scheduled_close_visible_after_trial_scope_ends() -> None:
    trace = LatencyTrace(enabled=True, run_id="diag")
    trace.begin_trial("bench-0068-v9")
    trace.record(
        "actuation_submit_return",
        command_id="protocol-9-close-1000283",
        expected_ns=200,
        accepted=True,
    )
    trace.end_trial()

    assert trace.should_trace_command("protocol-9-close-1000283")
    trace.record(
        "actuation_execute_enter",
        at_ns=230,
        command_id="protocol-9-close-1000283",
        expected_ns=200,
    )
    trace.record(
        "writer_return",
        at_ns=231,
        command_id="protocol-9-close-1000283",
        expected_ns=200,
        hal_started_ns=230,
        hal_actual_ns=231,
        result="success",
    )

    close_events = [
        event
        for event in trace.events
        if event.get("command_id") == "protocol-9-close-1000283"
    ]
    assert [event["event"] for event in close_events] == [
        "actuation_submit_return",
        "actuation_execute_enter",
        "writer_return",
    ]
    assert all(event["trial_label"] == "bench-0068-v9" for event in close_events)
    assert not trace.should_trace_command("protocol-9-close-1000283")


@pytest.mark.skipif(
    not FAILED_STORY35_HIL_RUN.is_dir(),
    reason="本机未保留 2026-07-30 第二次 Story 3.5 HIL 失败证据",
)
def test_failed_story35_close_trace_and_abort_bundle_are_audited_read_only() -> None:
    evidence_files = tuple(
        path
        for path in FAILED_STORY35_HIL_RUN.rglob("*")
        if path.is_file()
    )
    before = {
        path: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in evidence_files
    }
    receipts = [
        json.loads(line)
        for line in (FAILED_STORY35_HIL_RUN / "receipts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    trigger = next(
        item
        for item in receipts
        if item["command_id"] == "protocol-9-close-1000283"
    )
    trace = [
        json.loads(line)
        for line in (FAILED_STORY35_HIL_RUN / "latency-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    target_trace = [
        item
        for item in trace
        if item.get("command_id") == "protocol-9-close-1000283"
    ]
    bundle = next(
        path
        for path in (FAILED_STORY35_HIL_RUN / "session-output").iterdir()
        if path.is_dir()
    )
    session_log = next(bundle.glob("*.log"))
    records = [
        json.loads(line)
        for line in session_log.read_text(encoding="utf-8").splitlines()
    ]
    trigger_sequence = next(
        item["session_sequence"]
        for item in records
        if item.get("command_id") == "protocol-9-close-1000283"
        and item.get("record_type") == "receipt"
    )
    abort_closes = [
        item
        for item in records
        if item.get("record_type") == "receipt"
        and item["session_sequence"] > trigger_sequence
        and str(item.get("command_id", "")).startswith("shutdown-close-")
    ]
    closed = records[-1]

    assert (trigger["started_ns"] - trigger["expected_ns"]) / 1_000_000 == pytest.approx(
        29.6321
    )
    assert (trigger["actual_ns"] - trigger["started_ns"]) / 1_000_000 == pytest.approx(
        0.6845
    )
    assert [item["event"] for item in target_trace] == [
        "actuation_submit_return"
    ]
    assert {item["valve"] for item in abort_closes} == set(range(21))
    assert all(
        item["result"] == "success"
        and item["action"] == "close"
        and item["category"] == "safety"
        for item in abort_closes
    )
    assert max(item["session_sequence"] for item in abort_closes) < closed[
        "session_sequence"
    ]
    assert closed["event"] == "session_closed"
    assert closed["producer_fences"] == {
        "actuation": 746,
        "hardware": 2553,
        "controller": 2,
    }

    after = {
        path: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in evidence_files
    }
    assert after == before


def test_receipt_collector_defers_diagnostic_jsonl_until_owner_has_stopped(
    tmp_path,
) -> None:
    path = tmp_path / "not-yet-created" / "receipts.jsonl"
    collector = ReceiptCollector(path)
    receipt = _receipt(
        valve=9,
        device="Dev1",
        line="P1.0",
        command_id="protocol-9-close-1000283",
    )

    collector.record(receipt)

    assert collector.receipts == [receipt]
    assert not path.exists()

    path.parent.mkdir()
    collector.write_jsonl()
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["command_id"] == "protocol-9-close-1000283"
    assert payload["result"] == "success"


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
    command_id: str | None = None,
    action: ActuationAction = ActuationAction.CLOSE,
    category: ActuationCategory = ActuationCategory.SAFETY,
    result: ActuationResult = ActuationResult.SUCCESS,
    trial_id: str = "safety",
) -> ActuationReceipt:
    expected_ns = time.perf_counter_ns()
    command = ActuationCommand(
        command_id=command_id or f"{trial_id}-{valve}-{action.value}",
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


def test_hil_waits_for_manual_trigger_owner_ack_before_applying_ai0_stimulus() -> None:
    order: list[object] = []
    receipt = _receipt(
        valve=1,
        device="Dev1",
        line="P0.0",
        action=ActuationAction.OPEN,
        category=ActuationCategory.NORMAL,
        trial_id="bench-0001-v1",
    )

    class Collector:
        receipts = []

        def wait_for(self, predicate, _timeout_s, _pump, *, after_index=0):
            order.append("wait_receipt")
            assert after_index == 0
            assert predicate(receipt)
            return receipt

    waiting = ("status", frozenset({ProtocolExecutionStatus.WAITING_EXHALE}))
    runtime = SimpleNamespace(
        config={"exhale_threshold": -0.44},
        collector=Collector(),
        latency_trace=SimpleNamespace(
            begin_trial=lambda label: order.append(("begin", label)),
            end_trial=lambda: order.append("end"),
        ),
        ai_hal=SimpleNamespace(
            set_ai0_software_stimulus=lambda value: order.append(("stimulus", value))
        ),
        actuation=SimpleNamespace(
            post_manual_trigger=lambda **_kwargs: order.append("manual_trigger")
        ),
        readiness=lambda: object(),
        wait_status=lambda statuses: order.append(
            ("status", frozenset(statuses))
        ),
        pump=lambda: None,
    )

    result = Runtime.trigger_current_trial_via_ai0(
        runtime,
        label="bench-0001-v1",
    )

    assert result is receipt
    assert order.index("manual_trigger") < order.index(waiting)
    assert order.index(waiting) < order.index(("stimulus", -0.94))


def test_story35_cli_exposes_native_recording_gate(monkeypatch) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    monkeypatch.setattr(
        "sys.argv",
        [
            "hil_actuation_benchmark.py",
            "--story-3-5-recording",
            "--candidate-commit",
            "a" * 40,
            "--cycles",
            "1",
        ],
    )

    args = benchmark_module.parse_args()

    assert args.story_3_5_recording is True
    assert args.candidate_commit == "a" * 40


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _initialize_candidate_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "candidate-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "hil-test@example.invalid")
    _git(repo, "config", "user.name", "HIL Test")
    tracked = repo / "candidate.txt"
    tracked.write_text("first\n", encoding="utf-8")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    tracked.write_text("second\n", encoding="utf-8")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "second")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_live_story35_candidate_must_exist_equal_head_and_have_clean_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    repo, prior_commit, head = _initialize_candidate_repo(tmp_path)
    monkeypatch.setattr(benchmark_module, "REPO_ROOT", repo)

    def parse(candidate: str):
        monkeypatch.setattr(
            "sys.argv",
            [
                "hil_actuation_benchmark.py",
                "--live",
                "--confirm",
                benchmark_module.LIVE_CONFIRMATION,
                "--story-3-5-recording",
                "--candidate-commit",
                candidate,
            ],
        )
        return benchmark_module.parse_args()

    with pytest.raises(SystemExit):
        parse("f" * 40)
    with pytest.raises(SystemExit):
        parse(prior_commit)

    (repo / "candidate.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        parse(head)

    _git(repo, "restore", "candidate.txt")
    args = parse(head)
    assert args.candidate_commit == head
    assert args.live is True


def test_aborted_benchmark_never_continues_to_safety_scenarios(monkeypatch) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    safety_started = False

    def fail_benchmark(_runtime, _args):
        raise RuntimeError("formal benchmark aborted")

    def safety(_runtime, _args):
        nonlocal safety_started
        safety_started = True
        return {}

    monkeypatch.setattr(benchmark_module, "run_benchmark", fail_benchmark)
    monkeypatch.setattr(benchmark_module, "run_safety_scenarios", safety)

    with pytest.raises(RuntimeError, match="aborted"):
        benchmark_module.run_acceptance_scenarios(object(), object())

    assert safety_started is False


def test_severe_benchmark_waits_for_all_abort_closes_before_raising(
    monkeypatch,
) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    open_receipt = SimpleNamespace(jitter_ms=1.0)
    close_receipt = SimpleNamespace(jitter_ms=30.3166)
    close_checks: list[tuple[int, str]] = []

    class RuntimeDouble:
        def __init__(self) -> None:
            self.collector = SimpleNamespace(receipts=[])
            self.output_dir = Path("story-3-5-test-run")
            self.valves = SimpleNamespace(
                resolve_target=lambda _valve: ("Dev2", "P1.0")
            )
            self.metrics = SimpleNamespace(
                config=SimpleNamespace(window_size=100, min_samples=20)
            )
            self._abort_close_confirmed = False

        def close_everything(self, _label):
            return []

        def begin_story_35_recording(self, *_args, **_kwargs):
            return None

        def command(self, **_kwargs):
            return object()

        def submit_and_wait(self, _command):
            return None

        def wait_ms(self, _milliseconds):
            return None

        def start_protocol_document(self, _document):
            return None

        def trigger_current_trial_via_ai0(self, *, label):
            assert label == "bench-0001-v1"
            return open_receipt

    runtime = RuntimeDouble()
    monkeypatch.setattr(
        benchmark_module,
        "wait_protocol_trial",
        lambda *_args, **_kwargs: (open_receipt, close_receipt),
    )

    def full_close(_runtime, *, after_index, scenario, timeout_s=3.0):
        close_checks.append((after_index, scenario))
        return {"all_configured_targets_closed": True}

    monkeypatch.setattr(benchmark_module, "wait_for_full_close", full_close)
    args = SimpleNamespace(
        valves=[1, 9, 13],
        cycles=1,
        duration_ms=100.0,
        inter_trial_ms=250.0,
        story_3_5_recording=True,
        candidate_commit="a" * 40,
        live=False,
    )

    with pytest.raises(RuntimeError, match="严重超限"):
        run_benchmark(runtime, args)

    assert close_checks == [(0, "severe-abort")]
    assert runtime._abort_close_confirmed is True


def test_confirmed_abort_close_is_not_replaced_by_a_second_shutdown_close_set() -> None:
    order: list[str] = []

    class Actuation:
        def isRunning(self):
            return True

        def emergency_close_all(self, _timeout_ms):
            raise AssertionError("已确认 severe 全关后不得再生成第二组 shutdown-close")

        def shutdown(self, _timeout_ms):
            order.append("actuation_fence_and_shutdown")
            return True

    runtime = Runtime.__new__(Runtime)
    runtime.config = {
        "actuation_emergency_close_timeout_ms": 500,
        "actuation_shutdown_timeout_ms": 2000,
    }
    runtime.actuation = Actuation()
    runtime.hardware = SimpleNamespace(stop=lambda: order.append("hardware_fence"))
    runtime.flow = SimpleNamespace(
        shutdown=lambda _timeout_ms: order.append("flow_shutdown")
    )
    runtime._shutdown_completed = False
    runtime._abort_close_confirmed = True

    runtime.stop()

    assert order == [
        "actuation_fence_and_shutdown",
        "hardware_fence",
        "flow_shutdown",
    ]


def test_incomplete_shutdown_close_latches_story35_bundle_failure_before_finalize() -> None:
    failures: list[tuple[str, str]] = []

    class Actuation:
        def isRunning(self):
            return True

        def emergency_close_all(self, _timeout_ms):
            return False

        def shutdown(self, _timeout_ms):
            return True

    runtime = Runtime.__new__(Runtime)
    runtime.config = {
        "actuation_emergency_close_timeout_ms": 500,
        "actuation_shutdown_timeout_ms": 2000,
    }
    runtime.actuation = Actuation()
    runtime.hardware = SimpleNamespace(stop=lambda: None)
    runtime.flow = SimpleNamespace(shutdown=lambda _timeout_ms: None)
    runtime._shutdown_completed = False
    runtime._abort_close_confirmed = False
    runtime._story_35_writer = SimpleNamespace(
        fail_from_producer=lambda *, stage, message: failures.append(
            (stage, message)
        )
    )

    with pytest.raises(RuntimeError, match="未获得全部成功回执"):
        runtime.stop()

    assert failures == [
        (
            "shutdown_emergency_close",
            "中止后未取得全部配置目标关闭回执，Story 3.5 bundle 禁止标记完整。",
        )
    ]


@pytest.mark.parametrize(
    ("failed_field", "failed_value"),
    [
        ("p95_ms", 20.0),
        ("rolling_p95_ms", [20.0]),
        ("final_window_p95_ms", 20.0),
        ("count", 199),
    ],
)
def test_failed_performance_gate_closes_and_never_starts_safety_scenarios(
    monkeypatch,
    failed_field,
    failed_value,
) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    stream = {
        "count": 200,
        "p95_ms": 10.0,
        "rolling_p95_ms": [10.0],
        "final_window_p95_ms": 10.0,
    }
    benchmark = {
        "open": {**stream, failed_field: failed_value},
        "close": dict(stream),
        "combined": dict(stream),
    }
    safety_started = False
    closes: list[str] = []

    monkeypatch.setattr(
        benchmark_module,
        "run_benchmark",
        lambda _runtime, _args: benchmark,
    )

    def safety(_runtime, _args):
        nonlocal safety_started
        safety_started = True
        return {}

    monkeypatch.setattr(benchmark_module, "run_safety_scenarios", safety)
    runtime = SimpleNamespace(
        metrics=SimpleNamespace(config=SimpleNamespace(target_ms=20.0)),
        close_everything=lambda label: closes.append(label),
    )
    args = SimpleNamespace(live=True, cycles=200, story_3_5_recording=False)

    with pytest.raises(RuntimeError, match="性能 Gate"):
        benchmark_module.run_acceptance_scenarios(runtime, args)

    assert closes == ["performance-gate-abort"]
    assert safety_started is False


def test_story35_safety_scenarios_use_one_recording_session_per_protocol(
    monkeypatch,
) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    started: list[str] = []
    finalized: list[str] = []

    class RuntimeDouble:
        def __init__(self) -> None:
            self._story_35_writer = None
            self.collector = SimpleNamespace(
                receipts=[],
                inject_delay_ms=0.0,
            )
            self.metrics = SimpleNamespace(
                config=SimpleNamespace(single_limit_ms=30.0),
                severe_latched=True,
                snapshot=lambda: SimpleNamespace(label=started[-1]),
            )
            self.actuation = SimpleNamespace(
                post_stop=lambda **_kwargs: None,
            )
            self.hardware = SimpleNamespace(
                consume_airflow_sample=lambda *_args: None,
            )

        def start_protocol_trial(self, *, label, valve, duration_ms):
            assert self._story_35_writer is None
            assert valve in {1, 9, 13}
            assert duration_ms == 500
            self._story_35_writer = object()
            started.append(label)
            return SimpleNamespace(jitter_ms=35.0 if label == "safety-severe" else 1.0)

        def finalize_story_35_recording(self, **kwargs):
            assert self._story_35_writer is not None
            finalized.append(kwargs["reason"])
            self._story_35_writer = None
            return (
                SimpleNamespace(complete=True),
                SimpleNamespace(complete=True),
            )

        def wait_status(self, _statuses):
            return None

        def close_everything(self, _label):
            return []

        def recover_low_flow_via_owner(self):
            return None

        def shutdown_via_service(self):
            return {
                "result": "success",
                "valves_closed": True,
                "heaters_off": True,
                "error": "",
            }

    monkeypatch.setattr(
        benchmark_module,
        "wait_for_full_close",
        lambda *_args, **kwargs: {
            "scenario": kwargs["scenario"],
            "all_configured_targets_closed": True,
        },
    )
    runtime = RuntimeDouble()
    args = SimpleNamespace(
        story_3_5_recording=True,
        valves=[1, 9, 13],
    )

    result = benchmark_module.run_safety_scenarios(runtime, args)

    assert set(result) == {"stop", "low_flow", "severe", "shutdown"}
    assert started == [
        "safety-stop",
        "safety-low-flow",
        "safety-severe",
        "safety-shutdown",
    ]
    assert finalized == [
        "safety_stop_completed",
        "safety_low_flow_completed",
        "safety_severe_completed",
        "safety_shutdown_completed",
    ]
    assert runtime._story_35_writer is None


def test_story35_writer_failure_invalidates_interlock_before_waking_actuation() -> None:
    order: list[tuple[str, object]] = []

    class Interlock:
        def update(self, **values):
            order.append(("interlock", values))

    class Actuation:
        def post_recorder_failed(self, message):
            order.append(("recorder_failed", message))

        def post_stop(self, *, message):
            order.append(("stop", message))

    runtime = object.__new__(Runtime)
    runtime.ingress = Interlock()
    runtime.actuation = Actuation()
    failure = SimpleNamespace(
        session_generation=7,
        message="disk failed",
    )

    Runtime._handle_story_35_writer_failure(runtime, failure)

    assert order[0] == (
        "interlock",
        {
            "recording_ready": False,
            "recorder_failed": True,
            "recorder_generation": 7,
        },
    )
    assert order[1] == ("recorder_failed", "disk failed")
    assert order[2][0] == "stop"


def test_story35_active_session_rejects_loading_a_different_protocol_document(
    tmp_path: Path,
) -> None:
    runtime = object.__new__(Runtime)
    bound = ProtocolDocument(
        source_path=tmp_path / "bound.csv",
        source_name="bound.csv",
        trials=[],
    )
    different = ProtocolDocument(
        source_path=tmp_path / "different.csv",
        source_name="different.csv",
        trials=[],
    )
    runtime._story_35_writer = object()
    runtime._story_35_bound_document = bound
    runtime.executor = object()

    with pytest.raises(RuntimeError, match="单 session|绑定协议"):
        Runtime.start_protocol_document(runtime, different)


def test_story35_benchmark_session_is_finalized_with_preserved_quality_before_safety(
    monkeypatch,
) -> None:
    import scripts.hil_actuation_benchmark as benchmark_module

    quality = ActuationQualitySnapshot(
        open=ActuationStreamSnapshot(sample_count=20, p95_ms=10.0),
        close=ActuationStreamSnapshot(sample_count=20, p95_ms=11.0),
        combined=ActuationStreamSnapshot(sample_count=40, p95_ms=11.0),
    )
    stream = {
        "count": 20,
        "p95_ms": 10.0,
        "rolling_p95_ms": [10.0],
        "final_window_p95_ms": 10.0,
    }
    benchmark = {
        "open": dict(stream),
        "close": dict(stream),
        "combined": dict(stream),
    }
    order: list[tuple[str, object]] = []
    runtime = SimpleNamespace(
        metrics=SimpleNamespace(
            config=SimpleNamespace(target_ms=20.0),
            snapshot=lambda: quality,
        ),
        _story_35_writer=object(),
        close_everything=lambda _label: None,
    )

    def finalize(**kwargs):
        order.append(("finalize", kwargs["final_quality"]))
        runtime._story_35_writer = None
        return (
            SimpleNamespace(complete=True),
            SimpleNamespace(complete=True),
        )

    runtime.finalize_story_35_recording = finalize
    monkeypatch.setattr(
        benchmark_module,
        "run_benchmark",
        lambda _runtime, _args: benchmark,
    )

    def safety(_runtime, _args):
        assert runtime._story_35_writer is None
        order.append(("safety", None))
        return {"passed": True}

    monkeypatch.setattr(benchmark_module, "run_safety_scenarios", safety)
    args = SimpleNamespace(
        live=False,
        cycles=20,
        story_3_5_recording=True,
    )

    returned_benchmark, returned_safety = benchmark_module.run_acceptance_scenarios(
        runtime,
        args,
    )

    assert returned_benchmark is benchmark
    assert returned_safety == {"passed": True}
    assert order == [("finalize", quality), ("safety", None)]


def test_story35_native_recording_binds_both_owners_collects_fences_and_validates(
    tmp_path,
) -> None:
    class FakeActuation:
        def __init__(self) -> None:
            self.recorder = None
            self.ready_generation = None

        def bind_session_recorder(self, recorder, *, generation, timeout_ms):
            assert timeout_ms > 0
            self.recorder = recorder
            self.generation = generation
            return True

        def post_recorder_ready(self, generation, *, wait=False, timeout_ms=1000):
            assert wait
            assert timeout_ms > 0
            self.ready_generation = generation
            return True

        def post_recorder_fence(self, *, wait=False, timeout_ms=1000):
            assert wait
            return self.recorder.post_fence(
                "actuation",
                producer_sequence=0,
            )

    class FakeHardware:
        def __init__(self) -> None:
            self.recorder = None

        def bind_session_recorder(self, recorder, *, generation, timeout_ms):
            assert generation > 0
            assert timeout_ms > 0
            self.recorder = recorder
            return True

        def post_session_fence(self):
            assert self.recorder.post_fence(
                "hardware",
                producer_sequence=0,
            )

    class FakeInterlock:
        def __init__(self) -> None:
            self.cleared = False
            self.snapshot = SimpleNamespace(unsafe_reason=lambda: "")

        def update(self, **_kwargs):
            return 2

        def read(self):
            return 2, self.snapshot, True

        def clear_unsafe_latch(self):
            self.cleared = True
            return True

    runtime = object.__new__(Runtime)
    runtime.config = {
        "inhale_threshold": 0.47,
        "exhale_threshold": -0.44,
        "low_flow_threshold": 0.2,
        "actuation_jitter_target_ms": 20.0,
        "actuation_jitter_single_limit_ms": 30.0,
        "actuation_jitter_window_size": 100,
        "actuation_jitter_min_samples": 20,
        "session_writer_close_timeout_ms": 2000,
    }
    runtime.output_dir = tmp_path
    runtime.state = SimpleNamespace(
        master_valve_line="Dev2/P1.0",
        hardware_variant="20-channel",
    )
    runtime.ingress = FakeInterlock()
    runtime.metrics = SimpleNamespace(
        config=SimpleNamespace(
            target_ms=20.0,
            single_limit_ms=30.0,
            window_size=100,
            min_samples=20,
        ),
        snapshot=lambda: None,
    )
    runtime.actuation = FakeActuation()
    runtime.hardware = FakeHardware()
    runtime._story_35_writer = None
    runtime._story_35_ingress = None
    runtime._story_35_descriptor = None
    runtime._story_35_controller_sequence = 0
    document = ProtocolDocument(
        source_path=tmp_path / "story-3-5.csv",
        source_name="story-3-5.csv",
        trials=[
            ProtocolTrial(
                trial_id="bench-0001-v1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=TriggerMode.MANUAL,
            )
        ],
        metadata={"story": "3.5"},
    )

    descriptor = runtime.begin_story_35_recording(
        document,
        candidate_commit="a" * 40,
        run_id="story-3-5-test",
        live=False,
    )

    assert runtime.actuation.recorder is runtime._story_35_ingress
    assert runtime.hardware.recorder is runtime._story_35_ingress
    assert runtime.actuation.ready_generation == descriptor.generation
    assert runtime.ingress.cleared
    assert runtime._story_35_writer._failure_callback is not None
    runtime.hardware.post_session_fence()
    assert runtime.actuation.post_recorder_fence(wait=True)

    result, validation = runtime.finalize_story_35_recording(
        reason="test_completed",
        aborted=False,
    )

    assert result.complete
    assert validation.complete, validation.reason
    assert validation.path == descriptor.paths.final_dir
