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
from app.services.flow_service import FlowApplyResult
from app.services.gating_service import GatingService
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.protocol_executor import ProtocolExecutionConfig, ProtocolExecutor
from app.services.safety_manager import SafetyManager
from app.services.ttl_trigger_service import TtlPulse
from app.services.valve_service import ValveService
from app.workers.actuation_worker import (
    ActuationInterlockIngress,
    ActuationWorker,
    InterlockSnapshot,
)
from app.workers.flow_worker import FlowCommand, FlowCommandResult


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


def test_manual_trigger_bypasses_ai_backlog_without_dropping_samples() -> None:
    clock = FakeClock()
    worker, _, _ = _worker(clock, lambda _command: None)
    readiness = ProtocolExecutionReadiness(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        ttl_input_ready=True,
    )
    first = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(
                timestamp=1.0,
                ai0=0.0,
                monotonic_ns=100,
                ai_epoch=1,
                sample_sequence=1,
            ),
        )
    )
    second = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(
                timestamp=1.001,
                ai0=-1.0,
                monotonic_ns=200,
                ai_epoch=1,
                sample_sequence=2,
            ),
        )
    )

    worker.post_ai_batch(first, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.post_ai_batch(second, readiness=readiness)

    trigger_kind, _ = worker._pop_ready()
    batch_kind, batch_payload = worker._pop_ready()

    assert trigger_kind == "manual_trigger"
    assert batch_kind == "ai_batch"
    assert [sample.monotonic_ns for sample in batch_payload["batch"].samples] == [
        100,
        200,
    ]
    assert worker._pop_ready() is None


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
    calls = []
    worker, state, _ = _worker(clock, calls.append, capacity=1)
    receipts = []
    worker.receipt_ready.connect(receipts.append)
    old_epoch = state.execution_epoch
    assert worker.submit(_command(command_id="one", sequence=1, expected_ns=clock.value + 10))

    assert worker.submit(_command(command_id="two", sequence=2, expected_ns=clock.value + 20)) is False
    assert state.status == ProtocolExecutionStatus.BLOCKED
    assert state.execution_epoch > old_epoch
    assert state.pending_open_command_id is None
    assert worker.normal_queue_size == 0
    assert calls == []
    assert {receipt.command_id for receipt in receipts} == {"one", "two"}
    assert all(receipt.result == ActuationResult.CANCELLED for receipt in receipts)
    emergency = worker.submit_emergency_close(3, reason="queue full")
    assert emergency.category == ActuationCategory.SAFETY
    assert worker.emergency_queue_size == 1


def test_deferred_open_queue_full_cancels_identity_without_ghost_pending_or_write() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("queue-full.csv"),
        source_name="queue-full.csv",
        trials=[ProtocolTrial("one", 0, 10, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )
    calls = []
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=calls.append,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
        normal_queue_capacity=1,
    )
    receipts = []
    worker.receipt_ready.connect(receipts.append)
    blocker = _command(
        command_id="warmup-blocker",
        sequence=1,
        expected_ns=clock.value + 1_000_000,
        category=ActuationCategory.WARMUP,
    )
    assert worker.submit(blocker)
    worker.post_start(document=document, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.process_ready(max_items=2)
    accepted_epoch = executor.state.execution_epoch
    batch = BreathSampleBatch.from_frames(
        (AnalogInputFrame(10.0, -0.6, monotonic_ns=clock.value, ai_epoch=1, sample_sequence=1),)
    )

    worker.post_ai_batch(batch, readiness=readiness)
    worker.process_ready(max_items=1)

    assert executor.state.execution_epoch > accepted_epoch
    assert executor.state.pending_open_command_id is None
    assert executor.state.status == ProtocolExecutionStatus.BLOCKED
    cancelled = [receipt for receipt in receipts if receipt.category == ActuationCategory.NORMAL]
    assert len(cancelled) == 1
    assert cancelled[0].result == ActuationResult.CANCELLED
    assert cancelled[0].stale is True
    assert calls == []


def test_stale_flow_result_is_emitted_without_mutating_current_interlock() -> None:
    clock = FakeClock()
    worker, state, ingress = _worker(clock, lambda command: None)
    state.execution_epoch = 2
    emitted = []
    worker.flow_result_ready.connect(emitted.append)
    stale = FlowCommandResult(
        command=FlowCommand(
            command_id="flow-old",
            execution_epoch=1,
            sequence=1,
            mode="single",
            a=1.0,
            b=0.0,
            c=0.0,
            source="ui",
        ),
        result=FlowApplyResult(False, "cancelled", 1.0, 0.0, 0.0, 1.0, "cancelled"),
    )
    before = ingress.read()

    worker.post_flow_result(stale)

    assert len(emitted) == 1
    assert emitted[0].command == stale.command
    assert emitted[0].result == stale.result
    assert emitted[0].stale is True
    assert ingress.read() == before
    assert state.execution_epoch == 2


def test_interlock_disjoint_updates_are_atomic_under_concurrent_producers() -> None:
    barrier = threading.Barrier(2)

    class CoordinatedIngress(ActuationInterlockIngress):
        def publish(self, snapshot):
            barrier.wait(timeout=1)
            return super().publish(snapshot)

    ingress = CoordinatedIngress(
        _safe_snapshot(connected=False, has_protocol=False)
    )
    threads = [
        threading.Thread(target=ingress.update, kwargs={"connected": True}),
        threading.Thread(target=ingress.update, kwargs={"has_protocol": True}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert ingress.read()[1].connected is True
    assert ingress.read()[1].has_protocol is True


def test_direct_interlock_notification_closes_active_valve_without_ui_dispatch() -> None:
    clock = FakeClock()
    calls = []

    def writer(command):
        calls.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, state, ingress = _worker(clock, writer)
    state.active_valve = 3
    state.status = ProtocolExecutionStatus.TRIGGERED
    ingress.update(safety_state="LOW_FLOW")

    worker.post_interlock_changed(timestamp=10.0)
    worker.process_ready()

    assert [(command.action, command.category) for command in calls] == [
        (ActuationAction.CLOSE, ActuationCategory.SAFETY)
    ]
    assert state.active_valve is None
    assert state.possibly_open_valves == set()


def test_idle_flow_intent_can_recover_low_flow_without_weakening_open_interlock() -> None:
    clock = FakeClock()
    submitted = []
    ingress = ActuationInterlockIngress(
        _safe_snapshot(
            flow_setpoints_ready=False,
            safety_state="LOW_FLOW",
            has_protocol=False,
            device_lease="idle",
        )
    )
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=lambda command: None,
        interlock=ingress,
        monotonic_ns_clock=clock,
        flow_submitter=lambda command: submitted.append(command) or True,
    )

    worker.post_flow_intent(mode="single", a=1.0, b=0.0, c=0.0, source="ui")
    worker.process_ready()

    assert len(submitted) == 1
    open_command = _command(
        command_id="blocked-open",
        sequence=2,
        expected_ns=clock.value,
        category=ActuationCategory.MANUAL,
        safety_generation=ingress.read()[0],
    )
    assert ingress.read()[1].command_rejection_reason(open_command)


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


def test_airflow_publish_preserves_other_producer_owned_interlock_fields() -> None:
    ingress = ActuationInterlockIngress(
        _safe_snapshot(
            flow_setpoints_ready=False,
            has_protocol=False,
            device_lease="idle",
        ),
        safety_manager=SafetyManager(low_flow_threshold=0.2),
    )

    ingress.publish_airflow(airflow=0.0, timestamp=10.0, hardware_state="SAFE")
    snapshot = ingress.read()[1]

    assert snapshot.safety_state == "LOW_FLOW"
    assert snapshot.flow_setpoints_ready is False
    assert snapshot.has_protocol is False
    assert snapshot.device_lease == "idle"


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


def test_next_ttl_trial_rearms_physical_detector_only_after_close_receipt() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("manual-then-ttl.csv"),
        source_name="manual-then-ttl.csv",
        trials=[
            ProtocolTrial("manual", 0, 10, 3, TriggerMode.MANUAL),
            ProtocolTrial("ttl", 0, 10, 4, TriggerMode.TTL),
        ],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )

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
    arm_requests = []
    worker.ttl_arm_requested.connect(arm_requests.append)
    batch = BreathSampleBatch.from_frames(
        (AnalogInputFrame(10.0, -0.6, monotonic_ns=clock.value, ai_epoch=1, sample_sequence=1),)
    )

    worker.post_start(document=document, readiness=readiness)
    worker.post_manual_trigger(readiness=readiness)
    worker.post_ai_batch(batch, readiness=readiness)
    worker.process_ready()
    clock.value = executor.state.close_deadline_ns
    worker.process_ready(max_items=1)

    assert executor.state.current_trial.trial_id == "ttl"
    assert executor.state.ttl_armed is False
    assert arm_requests == [executor.state.arm_epoch]
    worker.consume_ttl_arm_ack(executor.state.arm_epoch, True)
    assert executor.state.ttl_armed is True


def test_accepted_ttl_pulse_physically_disarms_before_next_manual_trial() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("ttl-then-manual.csv"),
        source_name="ttl-then-manual.csv",
        trials=[
            ProtocolTrial("ttl", 0, 10, 3, TriggerMode.TTL),
            ProtocolTrial("manual", 0, 10, 4, TriggerMode.MANUAL),
        ],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(AssertionError("sync writer")),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )

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
    arm_requests = []
    disarm_requests = []
    worker.ttl_arm_requested.connect(arm_requests.append)
    worker.ttl_disarm_requested.connect(lambda: disarm_requests.append(True))
    worker.post_start(document=document, readiness=readiness)
    worker.process_ready()
    armed_epoch = arm_requests[-1]
    worker.consume_ttl_arm_ack(armed_epoch, True)

    worker.post_ttl_pulse(
        TtlPulse(timestamp=10.1, arm_epoch=armed_epoch, sequence=1),
        readiness=readiness,
    )
    worker.process_ready()

    assert executor.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert executor.state.ttl_armed is False
    assert disarm_requests == [True]
    batch = BreathSampleBatch.from_frames(
        (AnalogInputFrame(10.2, -0.6, monotonic_ns=clock.value, ai_epoch=1, sample_sequence=1),)
    )
    worker.post_ai_batch(batch, readiness=readiness)
    worker.process_ready()
    clock.value = executor.state.close_deadline_ns
    worker.process_ready(max_items=1)

    assert executor.state.current_trial.trial_id == "manual"
    assert executor.state.current_mode == TriggerMode.MANUAL
    assert arm_requests == [armed_epoch]
    assert disarm_requests == [True]


def test_negative_ttl_arm_ack_converges_to_blocked_without_rearming_loop() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("ttl.csv"),
        source_name="ttl.csv",
        trials=[ProtocolTrial("ttl", 0, 10, 3, TriggerMode.TTL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda channel, opened: (True, "ok"),
        deferred_actuation=True,
    )
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: None,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        monotonic_ns_clock=clock,
    )
    arm_requests = []
    worker.ttl_arm_requested.connect(arm_requests.append)

    worker.post_start(document=document, readiness=readiness)
    worker.process_ready()
    requested_epoch = arm_requests[-1]
    worker.consume_ttl_arm_ack(requested_epoch, False)
    worker.post_snapshot_request()
    worker.process_ready()

    assert executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert executor.state.ttl_armed is False
    assert arm_requests == [requested_epoch]
    assert executor.state.events[-1].event == "ttl_arm_failed"


def test_retry_timeout_gets_new_owner_deadline_and_duplicate_trigger_does_not() -> None:
    clock = FakeClock()
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("retry.csv"),
        source_name="retry.csv",
        trials=[ProtocolTrial("one", 0, 10, 3, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda channel, opened: (True, "ok"),
        deferred_actuation=True,
        config=ProtocolExecutionConfig(
            breath_gate_timeout_ms=500,
            breath_gate_timeout_action="retry",
            breath_gate_max_retries=1,
        ),
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
    worker.post_manual_trigger(readiness=readiness)
    worker.process_ready(max_items=3)
    assert len(worker._deadline_heap) == 1

    clock.value += 500_000_000
    worker.process_ready(max_items=1)
    assert executor.state.retry_count == 1
    assert len(worker._deadline_heap) == 1
    clock.value += 500_000_000
    worker.process_ready(max_items=1)
    assert executor.state.status == ProtocolExecutionStatus.COMPLETED


def test_failed_normal_close_immediately_queues_and_executes_compensation() -> None:
    clock = FakeClock()
    attempts = []

    def writer(command):
        attempts.append(command)
        result = (
            ActuationResult.FAILED
            if command.action == ActuationAction.CLOSE
            and command.category == ActuationCategory.NORMAL
            else ActuationResult.SUCCESS
        )
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns if result == ActuationResult.SUCCESS else None,
            wall_timestamp=10.0,
            result=result,
            message="close failed" if result == ActuationResult.FAILED else "",
        )

    worker, state, _ = _worker(clock, writer)
    assert worker.submit(_command(command_id="open", sequence=1, expected_ns=clock.value))
    worker.process_ready(max_items=1)
    clock.value = state.close_deadline_ns
    worker.process_ready(max_items=1)

    assert state.status == ProtocolExecutionStatus.BLOCKED
    assert state.possibly_open_valves == {3}
    assert worker.emergency_queue_size == 1
    worker.process_ready(max_items=1)
    assert state.possibly_open_valves == set()
    assert state.pending_close_command_id is None
    assert [command.category for command in attempts[-2:]] == [
        ActuationCategory.NORMAL,
        ActuationCategory.SAFETY,
    ]


def test_same_valve_emergency_intents_each_receive_a_traceable_receipt() -> None:
    clock = FakeClock()
    receipts = []

    def writer(command):
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker, _, _ = _worker(clock, writer)
    worker.receipt_ready.connect(receipts.append)
    first = worker.submit_emergency_close(3, reason="first")
    second = worker.submit_emergency_close(3, reason="second")
    worker.process_ready(max_items=2)

    assert {receipt.command_id for receipt in receipts} == {
        first.command_id,
        second.command_id,
    }


def test_failed_do_release_prevents_false_handoff_and_restart() -> None:
    class FailingReleaseHal:
        def prepare_do_output(self):
            return True

        def release_do_output(self):
            return False

    class Writer:
        def __init__(self):
            self.hal = FailingReleaseHal()

        def __call__(self, command):  # pragma: no cover - no command is queued
            raise AssertionError(command)

    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(),
        writer=Writer(),
        interlock=ActuationInterlockIngress(_safe_snapshot()),
    )

    assert worker.process_ready_with_do_ownership(max_items=0) == 0
    assert worker.shutdown(10) is False
    assert worker.prepare_restart() is False


def test_failed_multistep_plan_rolls_back_every_opened_or_uncertain_target() -> None:
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
        calls.append(command)
        fail_odor_open = (
            command.action == ActuationAction.OPEN and command.target_line == "P0.0"
        )
        result = ActuationResult.FAILED if fail_odor_open else ActuationResult.SUCCESS
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns if result == ActuationResult.SUCCESS else None,
            wall_timestamp=10.0,
            result=result,
            message="odor write failed" if fail_odor_open else "",
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

    worker.post_valve_plan(plan, category=ActuationCategory.MANUAL, request_id="rollback")
    worker.process_ready()

    assert [(command.action, command.target_line) for command in calls] == [
        (ActuationAction.OPEN, "P1.0"),
        (ActuationAction.OPEN, "P0.0"),
        (ActuationAction.CLOSE, "P0.0"),
        (ActuationAction.CLOSE, "P1.0"),
    ]
    assert results[-1]["success"] is False
    assert "补偿关闭" in results[-1]["message"]
    assert valve_service.master_is_open() is False
    assert valve_service.is_open(1) is False


def test_stop_supersedes_pending_load_and_reports_document_failure() -> None:
    old_document = ProtocolDocument(
        Path("old.csv"),
        "old.csv",
        [ProtocolTrial("old", 0, 100, 1, TriggerMode.MANUAL)],
    )
    new_document = ProtocolDocument(
        Path("new.csv"),
        "new.csv",
        [ProtocolTrial("new", 0, 100, 2, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(0.5, -0.5),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
    )
    executor.reset(old_document)
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        ),
        interlock=ActuationInterlockIngress(_safe_snapshot()),
    )
    document_results = []
    worker.document_result_ready.connect(document_results.append)
    worker._pending_safe_transition = ("load", {"document": new_document})

    worker.post_stop(message="operator stop wins")
    worker.process_ready()

    assert document_results[-1]["success"] is False
    assert executor.state.document is old_document
    assert executor.state.status == ProtocolExecutionStatus.STOPPED


def test_start_ack_is_frozen_before_later_state_mutation() -> None:
    document = ProtocolDocument(
        source_path=Path("start.csv"),
        source_name="start.csv",
        trials=[ProtocolTrial("one", 0, 100, 1, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(0.5, -0.5),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
    )
    executor.reset(document)
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        ),
        interlock=ActuationInterlockIngress(_safe_snapshot()),
    )
    acknowledgements = []
    worker.start_result_ready.connect(acknowledgements.append)

    worker.post_start(document=None, readiness=ProtocolExecutionReadiness(True, True, True, "SAFE", True))
    worker.process_ready()
    ack = acknowledgements[-1]
    accepted_epoch = ack.execution_epoch

    executor.state.execution_epoch += 10
    executor.state.status = ProtocolExecutionStatus.BLOCKED

    assert ack.accepted is True
    assert ack.execution_epoch == accepted_epoch
    assert ack.status == ProtocolExecutionStatus.WAITING_TRIGGER


def _configured_valve_service() -> ValveService:
    app_state = AppState(
        hardware_variant="test",
        valve_variants={"test": {1: "Dev1/P0.0", 2: "Dev1/P0.1"}},
        master_valve_line="Dev2/P1.0",
        hardware_ready=True,
        flow_setpoints_ready=True,
    )
    app_state.telemetry.connected = True
    return ValveService(
        state=app_state,
        safety_manager=SafetyManager(),
        worker=None,
        valve_variants=app_state.valve_variants,
        hardware_variant=app_state.hardware_variant,
        master_valve_line=app_state.master_valve_line,
    )


def test_readiness_loss_closes_every_configured_target_while_executor_is_idle() -> None:
    clock = FakeClock()
    valve_service = _configured_valve_service()
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
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

    ingress = ActuationInterlockIngress(
        _safe_snapshot(has_protocol=False, device_lease="idle")
    )
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ingress,
        valve_service=valve_service,
        monotonic_ns_clock=clock,
    )
    ingress.update(safety_state="LOW_FLOW")
    worker.post_readiness_update(
        readiness=ProtocolExecutionReadiness(True, True, True, "LOW_FLOW", True)
    )
    worker.process_ready()

    assert {(command.valve, command.target_line) for command in calls} == {
        (0, "P1.0"),
        (1, "P0.0"),
        (2, "P0.1"),
    }
    assert all(command.category == ActuationCategory.SAFETY for command in calls)
    assert executor.state.status == ProtocolExecutionStatus.BLOCKED


def test_stop_waits_for_close_receipts_from_every_configured_target() -> None:
    clock = FakeClock()
    document = ProtocolDocument(
        source_path=Path("stop.csv"),
        source_name="stop.csv",
        trials=[ProtocolTrial("one", 0, 100, 1, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
    )
    executor.reset(document)
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

    worker = ActuationWorker(
        protocol_executor=executor,
        writer=writer,
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        valve_service=_configured_valve_service(),
        monotonic_ns_clock=clock,
    )
    worker.post_stop(message="stop")
    worker.process_ready()

    assert {(command.valve, command.target_line) for command in calls} == {
        (0, "P1.0"),
        (1, "P0.0"),
        (2, "P0.1"),
    }
    assert executor.state.status == ProtocolExecutionStatus.STOPPED
    assert worker._safe_transition_close_pending == set()


def test_stopped_worker_rejects_correlated_intents_instead_of_replaying_them() -> None:
    clock = FakeClock()
    valve_service = _configured_valve_service()
    plan = valve_service.plan_valve(
        1,
        True,
        safety_state=SafetyState("SAFE", 1.0, 0.2, 1.0, ""),
    )[1]
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=lambda command: (_ for _ in ()).throw(AssertionError(command)),
        interlock=ActuationInterlockIngress(
            _safe_snapshot(has_protocol=False, device_lease="idle")
        ),
        valve_service=valve_service,
        monotonic_ns_clock=clock,
    )
    plan_results = []
    flow_results = []
    worker.plan_result_ready.connect(plan_results.append)
    worker.flow_result_ready.connect(flow_results.append)
    assert worker.shutdown(10)

    worker.post_valve_plan(plan, category=ActuationCategory.MANUAL, request_id="late-plan")
    worker.post_flow_intent(mode="idle", a=1.0, b=2.0, c=3.0, source="late-flow")

    assert worker.process_ready() == 0
    assert plan_results == [
        {
            "request_id": "late-plan",
            "success": False,
            "message": "动作线程已停止接单，请在硬件恢复后重新发起请求。",
        }
    ]
    assert flow_results[-1].result.success is False
    assert worker.prepare_restart()
    assert worker.process_ready() == 0


def test_invalidation_emits_terminal_receipt_for_every_removed_normal_command() -> None:
    clock = FakeClock()
    worker, _, _ = _worker(clock, lambda command: None)
    receipts = []
    worker.receipt_ready.connect(receipts.append)
    commands = [
        _command(command_id="queued-open", sequence=1, expected_ns=clock.value + 100),
        _command(
            command_id="queued-close",
            sequence=2,
            expected_ns=clock.value + 100,
            action=ActuationAction.CLOSE,
        ),
    ]
    assert all(worker.submit(command) for command in commands)

    worker.invalidate_execution(reason="readiness lost")

    assert {receipt.command_id for receipt in receipts} == {
        command.command_id for command in commands
    }
    assert all(
        receipt.result == ActuationResult.CANCELLED and receipt.stale
        for receipt in receipts
    )
    assert worker.normal_queue_size == 0
    assert not worker._commands_by_id


def test_generation_change_finishes_manual_plan_with_correlated_rollback() -> None:
    clock = FakeClock()
    valve_service = _configured_valve_service()
    plan = valve_service.plan_valve(
        1,
        True,
        safety_state=SafetyState("SAFE", 1.0, 0.2, 1.0, ""),
    )[1]
    ingress = ActuationInterlockIngress(
        _safe_snapshot(has_protocol=False, device_lease="idle")
    )
    calls = []

    def writer(command):
        calls.append(command)
        if command.action == ActuationAction.OPEN:
            ingress.update(safety_state="LOW_FLOW")
        return ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=writer,
        interlock=ingress,
        valve_service=valve_service,
        monotonic_ns_clock=clock,
    )
    results = []
    worker.plan_result_ready.connect(results.append)
    worker.post_valve_plan(plan, category=ActuationCategory.MANUAL, request_id="race")
    worker.process_ready()

    assert [(command.action, command.target_line) for command in calls] == [
        (ActuationAction.OPEN, "P1.0"),
        (ActuationAction.CLOSE, "P1.0"),
    ]
    assert results[-1]["request_id"] == "race"
    assert results[-1]["success"] is False
    assert worker._plan_contexts == {}
    assert worker._plan_by_command == {}


def test_load_preserves_quality_and_successful_start_clears_snapshot_atomically() -> None:
    clock = FakeClock()
    first = ProtocolDocument(
        source_path=Path("first.csv"),
        source_name="first.csv",
        trials=[ProtocolTrial("one", 0, 100, 1, TriggerMode.MANUAL)],
    )
    second = ProtocolDocument(
        source_path=Path("second.csv"),
        source_name="second.csv",
        trials=[ProtocolTrial("two", 0, 100, 2, TriggerMode.MANUAL)],
    )
    metrics = ActuationMetrics()
    sample_command = _command(
        command_id="quality",
        sequence=1,
        expected_ns=clock.value,
    )
    metrics.record(
        ActuationReceipt.from_write(
            command=sample_command,
            started_ns=clock.value,
            actual_ns=clock.value + 5_000_000,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
    )
    executor.reset(first)
    executor.state.quality = metrics.snapshot()
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        ),
        interlock=ActuationInterlockIngress(_safe_snapshot()),
        metrics=metrics,
        monotonic_ns_clock=clock,
    )

    worker.post_load(second)
    worker.process_ready()
    assert executor.state.quality.open.sample_count == 1

    worker.post_start(
        document=None,
        readiness=ProtocolExecutionReadiness(True, True, True, "SAFE", True),
    )
    worker.process_ready()
    assert executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert executor.state.quality.open.sample_count == 0


def test_receipt_bookkeeping_retires_payloads_and_bounds_duplicate_window() -> None:
    clock = FakeClock()
    receipts = []

    def writer(command):
        receipt = ActuationReceipt.from_write(
            command=command,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )
        receipts.append(receipt)
        return receipt

    worker, _, _ = _worker(clock, writer)
    worker._receipt_history_limit = 4
    emitted = []
    worker.receipt_ready.connect(emitted.append)
    for index in range(10):
        command = _command(
            command_id=f"warmup-{index}",
            sequence=index,
            expected_ns=clock.value,
            category=ActuationCategory.WARMUP,
        )
        assert worker.submit(command)
        worker.process_ready(max_items=1)

    assert worker._commands_by_id == {}
    assert len(worker._seen_receipts) == 4
    before = len(emitted)
    worker.consume_receipt(receipts[-1])
    assert len(emitted) == before


def test_emergency_close_cancels_every_queued_non_safety_open_before_replay() -> None:
    clock = FakeClock()
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

    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=1),
        writer=writer,
        interlock=ActuationInterlockIngress(
            _safe_snapshot(has_protocol=False, device_lease="idle")
        ),
        valve_service=_configured_valve_service(),
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
    )
    emitted = []
    worker.receipt_ready.connect(emitted.append)
    queued = [
        _command(
            command_id=f"queued-{category.value}",
            sequence=index,
            expected_ns=clock.value + 1_000,
            category=category,
        )
        for index, category in enumerate(
            (
                ActuationCategory.MANUAL,
                ActuationCategory.PRETEST,
                ActuationCategory.WARMUP,
            ),
            start=1,
        )
    ]
    assert all(worker.submit(command) for command in queued)

    worker._begin_emergency_close_all()
    worker.process_ready()
    clock.value += 1_000
    worker.process_ready()

    assert all(command.category == ActuationCategory.SAFETY for command in calls)
    assert worker.normal_queue_size == 0
    cancelled_ids = {
        receipt.command_id
        for receipt in emitted
        if receipt.result == ActuationResult.CANCELLED
    }
    assert cancelled_ids == {command.command_id for command in queued}


def test_protocol_lease_rejects_manual_close_without_writing_do() -> None:
    clock = FakeClock()
    calls = []
    worker, _, _ = _worker(clock, lambda command: calls.append(command))
    emitted = []
    worker.receipt_ready.connect(emitted.append)
    command = _command(
        command_id="manual-close-during-protocol",
        sequence=1,
        expected_ns=clock.value,
        action=ActuationAction.CLOSE,
        category=ActuationCategory.MANUAL,
        duration_ns=None,
    )

    assert worker.submit(command)
    worker.process_ready()

    assert calls == []
    assert emitted[-1].command_id == command.command_id
    assert emitted[-1].result == ActuationResult.CANCELLED
    assert "设备租约" in emitted[-1].message


def test_stopped_worker_returns_terminal_results_for_start_and_load() -> None:
    document = ProtocolDocument(
        source_path=Path("late.csv"),
        source_name="late.csv",
        trials=[ProtocolTrial("late", 0, 100, 1, TriggerMode.MANUAL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda *_: (True, "ok"),
        deferred_actuation=True,
    )
    worker = ActuationWorker(
        protocol_executor=executor,
        writer=lambda command: (_ for _ in ()).throw(AssertionError(command)),
        interlock=ActuationInterlockIngress(_safe_snapshot()),
    )
    start_results = []
    document_results = []
    worker.start_result_ready.connect(start_results.append)
    worker.document_result_ready.connect(document_results.append)
    assert worker.shutdown(10)

    worker.post_start(
        document=document,
        readiness=ProtocolExecutionReadiness(True, True, True, "SAFE", True),
        lease_epoch=executor.state.execution_epoch,
    )
    worker.post_load(document)

    assert len(start_results) == 1
    assert start_results[0].accepted is False
    assert start_results[0].lease_epoch == executor.state.execution_epoch
    assert document_results == [
        {
            "document": document,
            "success": False,
            "message": "动作线程已停止接单，请在硬件恢复后重新发起请求。",
        }
    ]


def test_ttl_breath_timeout_uses_pulse_capture_monotonic_time() -> None:
    clock = FakeClock()
    captured_ns = clock.value
    readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    document = ProtocolDocument(
        source_path=Path("ttl-timeout.csv"),
        source_name="ttl-timeout.csv",
        trials=[ProtocolTrial("ttl", 0, 100, 1, TriggerMode.TTL)],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(),
        valve_writer=lambda *_: (True, "ok"),
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
    arm_requests = []
    worker.ttl_arm_requested.connect(arm_requests.append)
    worker.post_start(document=document, readiness=readiness)
    worker.process_ready()
    arm_epoch = arm_requests[-1]
    worker.consume_ttl_arm_ack(arm_epoch, True)

    clock.value = captured_ns + 400_000_000
    worker.post_ttl_pulse(
        TtlPulse(
            timestamp=9.6,
            arm_epoch=arm_epoch,
            sequence=1,
            monotonic_ns=captured_ns,
        ),
        readiness=readiness,
    )
    worker.process_ready()

    assert worker._deadline_heap[0][0] == captured_ns + 500_000_000
    clock.value = captured_ns + 499_999_999
    assert worker.process_ready() == 0
    clock.value += 1
    assert worker.process_ready(max_items=1) == 1
    assert executor.state.status == ProtocolExecutionStatus.COMPLETED
