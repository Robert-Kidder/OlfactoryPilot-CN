from __future__ import annotations

import threading
from pathlib import Path

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    AppState,
    ProtocolDocument,
    ProtocolExecutionReadiness,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolTrial,
    SafetyState,
    TriggerMode,
)
from app.services.actuation_metrics import ActuationMetrics
from app.services.gating_service import GatingService
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.protocol_executor import ProtocolExecutionConfig, ProtocolExecutor
from app.services.safety_manager import SafetyManager
from app.services.valve_service import ValveService
from app.workers.actuation_worker import (
    ActuationInterlockIngress,
    ActuationWorker,
    InterlockSnapshot,
)


class FakeClock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _safe_snapshot(**changes) -> InterlockSnapshot:
    values = {
        "connected": True,
        "hardware_ready": True,
        "flow_setpoints_ready": True,
        "safety_state": "SAFE",
        "ttl_input_ready": True,
        "has_protocol": True,
        "device_lease": "protocol",
    }
    values.update(changes)
    return InterlockSnapshot(**values)


def _command(
    *,
    command_id: str,
    sequence: int,
    expected_ns: int,
    action: ActuationAction = ActuationAction.OPEN,
    execution_epoch: int = 1,
    category: ActuationCategory = ActuationCategory.NORMAL,
    duration_ns: int | None = 100_000_000,
    safety_generation: int = 1,
) -> ActuationCommand:
    return ActuationCommand(
        command_id=command_id,
        execution_epoch=execution_epoch,
        arm_epoch=2,
        sequence=sequence,
        trial_id="trial-1",
        trial_index=0,
        valve=3,
        action=action,
        category=category,
        expected_ns=expected_ns,
        duration_ns=duration_ns,
        wall_timestamp=10.0,
        safety_generation=safety_generation,
    )


def _worker(clock: FakeClock, writer, *, capacity: int = 256):
    state = ProtocolExecutionState(
        status=ProtocolExecutionStatus.WAITING_EXHALE,
        execution_epoch=1,
        arm_epoch=2,
    )
    ingress = ActuationInterlockIngress(_safe_snapshot())
    worker = ActuationWorker(
        protocol_state=state,
        writer=writer,
        interlock=ingress,
        metrics=ActuationMetrics(),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
        normal_queue_capacity=capacity,
    )
    return worker, state, ingress


def test_deadlines_do_not_run_early_and_equal_deadlines_use_sequence_order() -> None:
    clock = FakeClock()
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command.command_id)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, _ = _worker(clock, writer)
    first = _command(
        command_id="first",
        sequence=1,
        expected_ns=clock.value + 10,
        category=ActuationCategory.WARMUP,
    )
    second = _command(
        command_id="second",
        sequence=2,
        expected_ns=clock.value + 10,
        category=ActuationCategory.WARMUP,
    )

    assert worker.submit(first) is True
    assert worker.submit(second) is True
    assert worker.process_ready() == 0
    assert calls == []

    clock.value += 10
    assert worker.process_ready(max_items=2) == 2
    assert calls == ["first", "second"]


def test_due_action_is_not_starved_by_ai_or_ui_message_backlog() -> None:
    clock = FakeClock()
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command.command_id)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, _, _ = _worker(clock, writer)
    due = _command(
        command_id="due",
        sequence=1,
        expected_ns=clock.value,
        category=ActuationCategory.WARMUP,
    )
    assert worker.submit(due)
    for _ in range(100):
        worker.post_snapshot_request()

    assert worker.process_ready(max_items=1) == 1
    assert calls == ["due"]


def test_open_ack_schedules_close_from_actual_open_without_controller_tick() -> None:
    clock = FakeClock()
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value + 5_000_000,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, _ = _worker(clock, writer)
    command = _command(command_id="open", sequence=1, expected_ns=clock.value)

    assert worker.submit(command) is True
    assert worker.process_ready(max_items=1) == 1
    expected_close = clock.value + 5_000_000 + 100_000_000
    assert state.active_valve == 3
    assert state.close_deadline_ns == expected_close
    assert state.pending_close_command_id is not None

    clock.value = expected_close - 1
    assert worker.process_ready() == 0
    clock.value = expected_close
    assert worker.process_ready() == 1
    assert [item.action for item in calls] == [ActuationAction.OPEN, ActuationAction.CLOSE]
    assert state.active_valve is None
    assert state.pending_close_command_id is None


def test_pending_open_identity_prevents_duplicate_submission() -> None:
    clock = FakeClock()
    worker, state, _ = _worker(clock, lambda command: None)

    assert worker.submit(_command(command_id="one", sequence=1, expected_ns=clock.value)) is True
    assert worker.submit(_command(command_id="two", sequence=2, expected_ns=clock.value)) is False
    assert state.pending_open_command_id == "one"


def test_normal_queue_full_fails_closed_but_emergency_close_is_reserved() -> None:
    clock = FakeClock()
    worker, state, _ = _worker(clock, lambda command: None, capacity=1)
    assert worker.submit(_command(command_id="one", sequence=1, expected_ns=clock.value + 10))
    state.pending_open_command_id = None

    assert worker.submit(_command(command_id="two", sequence=2, expected_ns=clock.value + 20)) is False
    assert state.status == ProtocolExecutionStatus.BLOCKED
    emergency = worker.submit_emergency_close(3, reason="queue full")
    assert emergency.category == ActuationCategory.SAFETY
    assert worker.emergency_queue_size == 1


def test_safety_loss_during_blocking_write_marks_possibly_open_and_compensates() -> None:
    clock = FakeClock()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command)
        if command.action == ActuationAction.OPEN:
            entered.set()
            assert release.wait(1)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, ingress = _worker(clock, writer)
    worker.submit(_command(command_id="open", sequence=1, expected_ns=clock.value))
    thread = threading.Thread(target=lambda: worker.process_ready(max_items=1))
    thread.start()
    assert entered.wait(1)
    ingress.publish(_safe_snapshot(safety_state="LOW_FLOW"))
    release.set()
    thread.join(1)

    assert state.status == ProtocolExecutionStatus.BLOCKED
    assert state.possibly_open_valves == {3}
    assert worker.emergency_queue_size == 1
    worker.process_ready(max_items=1)
    assert [item.action for item in calls] == [ActuationAction.OPEN, ActuationAction.CLOSE]
    assert state.possibly_open_valves == set()


def test_stale_successful_open_receipt_never_advances_and_requests_close() -> None:
    clock = FakeClock()
    worker, state, _ = _worker(clock, lambda command: None)
    stale_command = _command(
        command_id="stale",
        sequence=1,
        expected_ns=clock.value,
        execution_epoch=0,
    )
    receipt = ActuationReceipt.from_write(
        command=stale_command,
        started_ns=clock.value,
        actual_ns=clock.value,
        wall_timestamp=10.0,
        result=ActuationResult.SUCCESS,
    )

    worker.consume_receipt(receipt)

    assert state.active_valve is None
    assert state.possibly_open_valves == {3}
    assert worker.emergency_queue_size == 1


def test_severe_open_jitter_latches_block_and_closes_immediately() -> None:
    clock = FakeClock()
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command)
        actual = command.expected_ns + 31_000_000 if command.action == ActuationAction.OPEN else clock.value
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=max(command.expected_ns, actual),
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, _ = _worker(clock, writer)
    worker.submit(_command(command_id="slow-open", sequence=1, expected_ns=clock.value))
    worker.process_ready(max_items=1)

    assert worker.metrics.severe_latched is True
    assert state.status == ProtocolExecutionStatus.BLOCKED
    assert worker.emergency_queue_size == 1
    worker.process_ready(max_items=1)
    assert [item.action for item in calls] == [ActuationAction.OPEN, ActuationAction.CLOSE]


def test_failed_emergency_close_keeps_conservative_open_fact() -> None:
    clock = FakeClock()

    def writer(command: ActuationCommand) -> ActuationReceipt:
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=None,
            wall_timestamp=10.0,
            result=ActuationResult.FAILED,
            message="DAQ failure",
        )

    worker, state, _ = _worker(clock, writer)
    state.possibly_open_valves.add(3)
    worker.submit_emergency_close(3, reason="retry")
    worker.process_ready(max_items=1)

    assert state.status == ProtocolExecutionStatus.BLOCKED
    assert state.possibly_open_valves == {3}


def test_ttl_armed_only_after_matching_ack() -> None:
    clock = FakeClock()
    worker, state, _ = _worker(clock, lambda command: None)

    worker.request_ttl_arm(arm_epoch=2)
    assert state.ttl_armed is False
    worker.consume_ttl_arm_ack(arm_epoch=1, armed=True)
    assert state.ttl_armed is False
    worker.consume_ttl_arm_ack(arm_epoch=2, armed=True)
    assert state.ttl_armed is True
    worker.request_ttl_disarm()
    assert state.ttl_armed is False


def test_interlock_raw_publish_uses_shared_safety_manager_and_safe_does_not_auto_clear() -> None:
    ingress = ActuationInterlockIngress(
        _safe_snapshot(),
        safety_manager=SafetyManager(low_flow_threshold=0.2),
    )
    generation = ingress.read()[0]

    ingress.publish_raw_telemetry(
        airflow=100.0,
        timestamp=10.0,
        hardware_state="LOW_FLOW",
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        ttl_input_ready=True,
        has_protocol=True,
        device_lease="protocol",
    )
    unsafe_generation, snapshot, latched = ingress.read()
    assert unsafe_generation > generation
    assert snapshot.safety_state == "LOW_FLOW"
    assert latched is True

    ingress.publish_raw_telemetry(
        airflow=100.0,
        timestamp=10.1,
        hardware_state="SAFE",
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        ttl_input_ready=True,
        has_protocol=True,
        device_lease="protocol",
    )
    assert ingress.read()[2] is True
    assert ingress.clear_unsafe_latch() is True
    assert ingress.read()[2] is False


def test_worker_owns_deferred_executor_and_processes_ai_to_close_deadline() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("worker.csv"),
        source_name="worker.csv",
        trials=[ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )
    calls = []

    def writer(command: ActuationCommand) -> ActuationReceipt:
        calls.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=max(clock.value, command.expected_ns),
            actual_ns=max(clock.value, command.expected_ns),
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    ingress = ActuationInterlockIngress(_safe_snapshot())
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ingress,
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )
    receipts = []
    worker.receipt_ready.connect(receipts.append)
    batch = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(10.01, -0.6, monotonic_ns=clock.value, ai_epoch=1, sample_sequence=1),
        )
    )

    worker.post_start(document=document, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.post_ai_batch(batch, readiness=readiness)
    assert worker.process_ready(max_items=3) == 3
    assert worker.normal_queue_size == 1
    assert worker.process_ready(max_items=1) == 1

    assert worker.protocol_executor is executor
    assert worker.gating_service is executor.gating_service
    assert calls, receipts
    assert executor.state.active_valve == 3, (
        calls,
        [(event.event, event.result, event.message) for event in executor.state.events],
        executor.state.pending_open_command_id,
        executor.state.possibly_open_valves,
        receipts,
        ingress.read(),
    )
    assert executor.state.pending_close_command_id is not None
    assert [item.action for item in calls] == [ActuationAction.OPEN]

    clock.value = executor.state.close_deadline_ns
    worker.process_ready(max_items=1)
    assert [item.action for item in calls] == [ActuationAction.OPEN, ActuationAction.CLOSE]
    assert executor.state.status == ProtocolExecutionStatus.COMPLETED


def test_breath_timeout_is_a_worker_monotonic_deadline_not_ui_tick() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("timeout.csv"),
        source_name="timeout.csv",
        trials=[ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda channel, opened: (True, "ok"),
        deferred_actuation=True,
        config=ProtocolExecutionConfig(breath_gate_timeout_ms=500),
        clock=lambda: 10.0,
    )
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: None,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )

    worker.post_start(document=document, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.process_ready(max_items=2)
    assert executor.state.status == ProtocolExecutionStatus.WAITING_EXHALE

    clock.value += 499_999_999
    assert worker.process_ready() == 0
    clock.value += 1
    assert worker.process_ready(max_items=1) == 1
    assert executor.state.status == ProtocolExecutionStatus.COMPLETED


def test_manual_valve_plan_runs_master_then_odor_and_commits_only_receipts() -> None:
    clock = FakeClock()
    app_state = AppState(
        hardware_variant="20-channel",
        valve_variants={"20-channel": {1: "Dev1/P0.0"}},
        master_valve_line="Dev2/P1.0",
        hardware_ready=True,
        flow_setpoints_ready=True,
    )
    app_state.telemetry.connected = True
    valve_service = ValveService(
        state=app_state,
        safety_manager=SafetyManager(),
        worker=None,
        valve_variants=app_state.valve_variants,
        hardware_variant=app_state.hardware_variant,
        master_valve_line=app_state.master_valve_line,
    )
    plan = valve_service.plan_valve(
        1,
        True,
        safety_state=SafetyState("SAFE", 1.0, 0.2, 1.0, ""),
    )[1]
    calls = []

    def writer(command):
        calls.append((command.valve, command.target_device, command.target_line))
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=writer,
        interlock=ActuationInterlockIngress(
            _safe_snapshot(has_protocol=False, device_lease="idle")
        ),
        valve_service=valve_service,
        monotonic_ns_clock=clock,
    )
    results = []
    worker.plan_result_ready.connect(results.append)

    worker.post_valve_plan(plan, category=ActuationCategory.MANUAL, request_id="manual-1")
    worker.process_ready()

    assert calls == [(0, "Dev2", "P1.0"), (1, "Dev1", "P0.0")]
    assert valve_service.master_is_open() is True
    assert valve_service.is_open(1) is True
    assert results[-1]["success"] is True


def test_protocol_lease_rejects_new_manual_open_plan() -> None:
    clock = FakeClock()
    app_state = AppState(
        hardware_variant="20-channel",
        valve_variants={"20-channel": {1: "Dev1/P0.0"}},
        hardware_ready=True,
        flow_setpoints_ready=True,
    )
    app_state.telemetry.connected = True
    valve_service = ValveService(
        state=app_state,
        safety_manager=SafetyManager(),
        worker=None,
        valve_variants=app_state.valve_variants,
        hardware_variant=app_state.hardware_variant,
    )
    plan = valve_service.plan_valve(
        1,
        True,
        safety_state=SafetyState("SAFE", 1.0, 0.2, 1.0, ""),
    )[1]
    calls = []
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=calls.append,
        interlock=ActuationInterlockIngress(_safe_snapshot(device_lease="protocol")),
        valve_service=valve_service,
        monotonic_ns_clock=clock,
    )
    results = []
    worker.plan_result_ready.connect(results.append)

    worker.post_valve_plan(plan, category=ActuationCategory.MANUAL, request_id="manual-2")
    worker.process_ready()

    assert calls == []
    assert results[-1]["success"] is False
    assert "租约" in results[-1]["message"]


def test_emergency_close_all_confirms_every_configured_target() -> None:
    clock = FakeClock()
    app_state = AppState(
        hardware_variant="test",
        valve_variants={"test": {1: "Dev1/P0.0", 2: "Dev1/P0.1"}},
        master_valve_line="Dev2/P1.0",
    )
    valve_service = ValveService(
        state=app_state,
        safety_manager=SafetyManager(),
        worker=None,
        valve_variants=app_state.valve_variants,
        hardware_variant="test",
        master_valve_line=app_state.master_valve_line,
    )
    calls = []

    def writer(command):
        calls.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, _ = _worker(clock, writer)
    worker.valve_service = valve_service

    assert worker.emergency_close_all(100)
    assert {(c.valve, c.target_line) for c in calls} == {
        (0, "P1.0"),
        (1, "P0.0"),
        (2, "P0.1"),
    }
    assert all(c.category == ActuationCategory.SAFETY for c in calls)
    assert state.possibly_open_valves == set()


def test_pause_closes_active_valve_then_resume_retries_same_trial_with_new_epoch() -> None:
    clock = FakeClock()
    document = ProtocolDocument(
        source_path=Path("pause.csv"),
        source_name="pause.csv",
        trials=[ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )
    executor.reset(document)
    executor.state.status = ProtocolExecutionStatus.TRIGGERED
    executor.state.active_valve = 3
    executor.state.execution_epoch = 5

    def writer(command):
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )
    worker.post_pause()
    worker.process_ready()

    paused_epoch = executor.state.execution_epoch
    assert executor.state.status == ProtocolExecutionStatus.PAUSED
    assert executor.state.active_valve is None
    assert executor.state.trial_index == 0

    worker.post_resume()
    worker.process_ready()
    assert executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert executor.state.trial_index == 0
    assert executor.state.execution_epoch > paused_epoch


def test_failed_pause_close_keeps_state_and_allows_explicit_pause_retry() -> None:
    clock = FakeClock()
    document = ProtocolDocument(
        source_path=Path("pause-retry.csv"),
        source_name="pause-retry.csv",
        trials=[ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )
    executor.reset(document)
    executor.state.status = ProtocolExecutionStatus.TRIGGERED
    executor.state.active_valve = 3
    executor.state.execution_epoch = 5
    attempts = 0

    def writer(command):
        nonlocal attempts
        attempts += 1
        result = ActuationResult.FAILED if attempts == 1 else ActuationResult.SUCCESS
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns if result == ActuationResult.SUCCESS else None,
            wall_timestamp=10.0,
            result=result,
            message="DAQ failure" if result == ActuationResult.FAILED else "",
        )

    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )

    worker.post_pause()
    worker.process_ready()
    assert executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert executor.state.active_valve == 3
    assert executor.state.possibly_open_valves == {3}

    worker.post_pause()
    worker.process_ready()
    assert attempts == 2
    assert executor.state.status == ProtocolExecutionStatus.PAUSED
    assert executor.state.active_valve is None
    assert executor.state.possibly_open_valves == set()


def test_severe_close_advances_final_trial_then_explicit_ack_completes_without_replay() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("severe-close.csv"),
        source_name="severe-close.csv",
        trials=[ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )

    def writer(command):
        delay = 31_000_000 if command.action == ActuationAction.CLOSE else 0
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns + delay,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
            actual_duration_ms=131.0 if command.action == ActuationAction.CLOSE else None,
        )

    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )
    batch = BreathSampleBatch.from_frames(
        (AnalogInputFrame(10.0, -0.6, monotonic_ns=clock.value, ai_epoch=1, sample_sequence=1),)
    )
    worker.post_start(document=document, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.post_ai_batch(batch, readiness=readiness)
    worker.process_ready()
    clock.value = executor.state.close_deadline_ns
    worker.process_ready()

    assert executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert executor.state.trial_index == 1
    assert executor.state.executed_quality_failed_trials == {"one"}

    worker.post_rearm()
    worker.process_ready()
    assert executor.state.status == ProtocolExecutionStatus.COMPLETED
    assert executor.state.trial_index == 1
