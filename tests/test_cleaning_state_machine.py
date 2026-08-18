from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationReceipt,
    ActuationResult,
    AppState,
    CleaningConfigSnapshot,
    CleaningOperationIdentity,
    CleaningStatus,
    DeviceLeaseKind,
    ExclusiveDeviceLease,
    ProtocolExecutionState,
    SelectorRoute,
)
from app.services.flow_service import FlowApplyResult
from app.services.safety_manager import SafetyManager
from app.services.valve_service import ValvePlanStep, ValveService
from app.workers.actuation_worker import (
    ActuationInterlockIngress,
    ActuationWorker,
    InterlockSnapshot,
)
from app.workers.flow_worker import FlowCommandResult


@dataclass
class _Clock:
    value: int = 1_000_000

    def now(self) -> int:
        return self.value

    def advance(self, nanoseconds: int) -> None:
        self.value += nanoseconds


class _Recorder:
    def __init__(self) -> None:
        self.receipts = []
        self.events = []
        self.fences = []

    def post_receipt(self, receipt, *, producer_sequence):
        self.receipts.append((producer_sequence, receipt))
        return True

    def post_event(self, **values):
        self.events.append(values)
        return True

    def post_fence(self, producer, *, producer_sequence, final_payload=None):
        self.fences.append((producer, producer_sequence, final_payload))
        return True


class _Valves:
    master_valve_line = "Dev2/P1.0"

    def __init__(self) -> None:
        self.committed = []

    def all_configured_close_steps(self):
        return (
            ValvePlanStep(2, "Dev1", "P0.1", False, "odor_safety_close"),
        )

    def selector_route_step(self, route):
        assert route in {SelectorRoute.ODOR, SelectorRoute.COMPENSATION}
        return ValvePlanStep(
            0,
            "Dev2",
            "P1.0",
            route == SelectorRoute.ODOR,
            "selector_odor_route"
            if route == SelectorRoute.ODOR
            else "selector_safe_route",
        )

    def commit_receipt(self, receipt):
        self.committed.append(receipt)


def _plan(duration_s: float = 0.01):
    config = {
        "cleaning": {
            "enabled": True,
            "gas_label": "Air",
            "flow_channel": "A",
            "default_flow_sccm": 1500,
            "max_approved_flow_sccm": 1500,
            "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
            "default_open_duration_s": duration_s,
            "max_open_duration_s": 120,
            "default_cycles": 1,
            "max_cycles": 20,
            "parallel_open_limit": 1,
            "default_channels": [2],
            "external_labels": {"2": "2"},
        }
    }
    snapshot = CleaningConfigSnapshot.from_effective_config(
        config,
        available_channels={2: "Dev1/P0.1"},
    )
    return snapshot.build_plan(CleaningOperationIdentity("clean-1", 1))


def _worker(
    *,
    safety_state: str = "SAFE",
    cleaning_flow_ready_timeout_ms: int = 5000,
):
    clock = _Clock()
    calls = []
    flows = []
    recorder = _Recorder()
    lease = ExclusiveDeviceLease()
    token = lease.acquire(
        DeviceLeaseKind.MAINTENANCE,
        operation_id="clean-1",
        generation=1,
    )
    assert token is not None

    def write(command):
        calls.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=max(clock.now(), command.expected_ns),
            actual_ns=max(clock.now(), command.expected_ns) + 10,
            wall_timestamp=1_700_000_000,
            result=ActuationResult.SUCCESS,
        )

    interlock = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=True,
            safety_state=safety_state,
            device_lease="maintenance",
            recording_ready=True,
        )
    )
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(),
        writer=write,
        interlock=interlock,
        valve_service=_Valves(),
        flow_submitter=lambda command: flows.append(command) or True,
        monotonic_ns_clock=clock.now,
        wall_clock=lambda: 1_700_000_000,
        cleaning_flow_ready_timeout_ms=cleaning_flow_ready_timeout_ms,
    )
    return worker, clock, calls, flows, recorder, token


def test_global_safe_stop_fences_queued_cleaning_start_before_it_can_open() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    results = []
    worker.cleaning_result_ready.connect(results.append)
    plan = _plan()

    assert worker.post_cleaning_start(plan, lease_token=token, recorder=recorder)
    identity = worker.fence_for_safe_stop(
        operation_id="global-stop",
        generation=2,
        reason="shutdown",
        timeout_ms=100,
    )

    assert identity is not None
    assert calls == []
    assert flows == []
    assert results[-1].identity == plan.identity
    assert results[-1].status == CleaningStatus.RECOVERY_REQUIRED
    assert worker.handoff_maintenance_for_safe_stop()
    assert recorder.fences[-1][0:2] == ("actuation", 0)


def test_cleaning_owner_enforces_flow_zero_before_selector_safe_route() -> None:
    worker, clock, calls, flows, recorder, token = _worker()
    snapshots = []
    results = []
    worker.cleaning_snapshot_ready.connect(snapshots.append)
    worker.cleaning_result_ready.connect(results.append)

    assert worker.post_cleaning_start(
        _plan(),
        lease_token=token,
        recorder=recorder,
    )
    worker.process_ready()

    assert [(item.valve, item.category, item.action) for item in calls] == [
        (2, ActuationCategory.SAFETY, ActuationAction.CLOSE),
    ]
    assert len(flows) == 1
    assert (flows[0].a, flows[0].b, flows[0].c) == (1500, 0, 0)
    assert not any(
        command.category == ActuationCategory.CLEANING
        and command.valve == 2
        and command.action == ActuationAction.OPEN
        for command in calls
    )

    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()

    assert [(item.valve, item.action) for item in calls[-2:]] == [
        (0, ActuationAction.OPEN),
        (2, ActuationAction.OPEN),
    ]
    assert calls[-2].category == ActuationCategory.CLEANING
    assert calls[-1].category == ActuationCategory.CLEANING
    assert snapshots[-1].status == CleaningStatus.RUNNING
    assert worker.process_ready() == 0

    clock.advance(_plan().open_duration_ns + 100)
    worker.process_ready()

    assert calls[-1].valve == 2 and calls[-1].action == ActuationAction.CLOSE
    assert len(flows) == 1
    assert flows[0].mode == "zero"
    assert (flows[0].a, flows[0].b, flows[0].c) == (0, 0, 0)

    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert [(item.valve, item.action) for item in calls[-2:]] == [
        (0, ActuationAction.CLOSE),
        (2, ActuationAction.CLOSE),
    ]
    assert results[-1].status == CleaningStatus.COMPLETED
    assert results[-1].safe_terminal is True
    assert worker.cleaning_snapshot.status == CleaningStatus.COMPLETED
    assert all(
        receipt.operation_id == "clean-1"
        for _, receipt in recorder.receipts
    )


def test_cleaning_can_recover_from_idle_low_flow_before_any_open() -> None:
    worker, _clock, calls, flows, recorder, token = _worker(
        safety_state="LOW_FLOW"
    )
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()

    assert len(flows) == 1
    assert all(command.action == ActuationAction.CLOSE for command in calls)

    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()

    assert all(command.action == ActuationAction.CLOSE for command in calls)
    assert worker.cleaning_snapshot.status == CleaningStatus.PREPARING

    worker.interlock.update(safety_state="SAFE")
    worker.post_interlock_changed()
    worker.process_ready()

    assert [(item.valve, item.action) for item in calls[-2:]] == [
        (0, ActuationAction.OPEN),
        (2, ActuationAction.OPEN),
    ]
    assert worker.cleaning_snapshot.status == CleaningStatus.RUNNING


def test_cleaning_waits_through_transient_data_stale_during_setpoint_write() -> None:
    worker, _clock, calls, flows, recorder, token = _worker(
        safety_state="LOW_FLOW"
    )
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()

    worker.interlock.update(safety_state="DATA_STALE")
    worker.post_interlock_changed()
    worker.process_ready()
    assert worker.cleaning_snapshot.status == CleaningStatus.PREPARING
    assert all(command.action == ActuationAction.CLOSE for command in calls)

    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    assert worker.cleaning_snapshot.status == CleaningStatus.PREPARING
    assert all(command.action == ActuationAction.CLOSE for command in calls)

    worker.interlock.update(safety_state="SAFE")
    worker.post_interlock_changed()
    worker.process_ready()

    assert [(item.valve, item.action) for item in calls[-2:]] == [
        (0, ActuationAction.OPEN),
        (2, ActuationAction.OPEN),
    ]
    assert worker.cleaning_snapshot.status == CleaningStatus.RUNNING


def test_cleaning_low_flow_recovery_timeout_never_opens_master_or_odor() -> None:
    worker, clock, calls, flows, recorder, token = _worker(
        safety_state="LOW_FLOW",
        cleaning_flow_ready_timeout_ms=10,
    )
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    clock.advance(11_000_000)
    worker.process_ready()

    assert not any(command.action == ActuationAction.OPEN for command in calls)
    assert worker.cleaning_snapshot.status in {
        CleaningStatus.STOPPING,
        CleaningStatus.FAILED,
    }


def test_cleaning_start_rejects_missing_recorder_readiness_without_any_open() -> None:
    worker, _clock, calls, _flows, recorder, token = _worker()
    worker.interlock.update(recording_ready=False)

    assert worker.post_cleaning_start(
        _plan(),
        lease_token=token,
        recorder=recorder,
    )
    worker.process_ready()

    assert not any(command.action == ActuationAction.OPEN for command in calls)
    assert worker.cleaning_snapshot.status == CleaningStatus.FAILED


def test_conflicting_receipt_fails_closed_and_does_not_advance() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready(max_items=2)
    open_command = calls[-1]
    conflict = ActuationReceipt.from_write(
        command=open_command,
        started_ns=open_command.expected_ns,
        actual_ns=open_command.expected_ns + 1,
        wall_timestamp=1_700_000_000,
        result=ActuationResult.SUCCESS,
    )
    conflict = conflict.__class__(
        **{
            name: getattr(conflict, name)
            for name in conflict.__dataclass_fields__
            if name != "target_line"
        },
        target_line="P0.7",
    )

    worker.consume_receipt(conflict)
    worker.process_ready()

    assert worker.cleaning_snapshot.status in {
        CleaningStatus.FAILED,
        CleaningStatus.RECOVERY_REQUIRED,
        CleaningStatus.STOPPING,
    }
    assert not any(
        command.action == ActuationAction.CLOSE
        and command.category == ActuationCategory.CLEANING
        for command in calls
    )


def test_cc01_stop_preempts_step_and_old_deadline_cannot_advance() -> None:
    worker, clock, calls, flows, recorder, token = _worker()
    results = []
    worker.cleaning_result_ready.connect(results.append)
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    count_before_stop = len(calls)

    assert worker.post_cleaning_stop(reason="用户中止", aborted=True)
    worker.process_ready()

    assert calls[count_before_stop:] == []
    assert len(flows) == 1 and flows[0].mode == "zero"
    clock.advance(_plan().open_duration_ns * 2)
    assert worker.process_ready() == 0

    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    stop_calls = calls[count_before_stop:]
    assert [(command.target_device, command.target_line) for command in stop_calls] == [
        ("Dev2", "P1.0"),
        ("Dev1", "P0.1"),
    ]
    assert results[-1].status == CleaningStatus.COMPLETED
    assert results[-1].outcome.value == "aborted"
    assert worker.cleaning_owner_handoff_ready is True


def test_cleaning_stop_a_zero_failure_never_routes_selector() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    before = len(calls)
    worker.post_cleaning_stop(reason="fault", aborted=False)
    worker.process_ready()
    zero = flows.pop()

    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(False, "A zero failed", 0, 0, 0, 0, "timeout"),
        )
    )
    worker.process_ready()

    assert calls[before:]
    assert all(command.valve != 0 for command in calls[before:])
    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert "A zero failed" in worker.cleaning_snapshot.recovery_reason


def test_cleaning_stop_late_a_zero_receipt_requires_recovery() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    before = len(calls)
    worker.post_cleaning_stop(reason="fault", aborted=False)
    worker.process_ready()
    expected = flows.pop()
    late = expected.__class__(
        command_id="late-zero",
        execution_epoch=expected.execution_epoch,
        sequence=expected.sequence,
        mode=expected.mode,
        a=expected.a,
        b=expected.b,
        c=expected.c,
        source=expected.source,
        operation_id=expected.operation_id,
        generation=expected.generation,
        lease_token=expected.lease_token,
    )

    worker.post_flow_result(
        FlowCommandResult(
            command=late,
            result=FlowApplyResult(True, "late", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert calls[before:]
    assert all(command.valve != 0 for command in calls[before:])
    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert "迟到或身份冲突" in worker.cleaning_snapshot.recovery_reason


def test_cleaning_same_command_id_with_conflicting_generation_never_routes_selector() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    before = len(calls)
    worker.post_cleaning_stop(reason="fault", aborted=False)
    worker.process_ready()
    expected = flows.pop()
    conflicting = replace(expected, generation=expected.generation + 1)

    worker.post_flow_result(
        FlowCommandResult(
            command=conflicting,
            result=FlowApplyResult(True, "conflict", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert calls[before:]
    assert all(command.valve != 0 for command in calls[before:])
    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert worker.cleaning_snapshot.flow_zero_confirmed is False
    assert worker.cleaning_snapshot.selector_safe_confirmed is False


def test_cc02_low_flow_then_late_open_is_latched_and_safely_closed() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    worker.interlock.update(safety_state="LOW_FLOW")
    worker.post_interlock_changed()
    worker.process_ready(max_items=1)
    late_command = calls[-1].__class__(
        command_id="late-open",
        execution_epoch=0,
        arm_epoch=0,
        sequence=999,
        trial_id=None,
        trial_index=None,
        valve=2,
        action=ActuationAction.OPEN,
        category=ActuationCategory.CLEANING,
        expected_ns=1,
        duration_ns=None,
        wall_timestamp=1_700_000_000,
        safety_generation=0,
        target_device="Dev1",
        target_line="P0.1",
        operation_id="clean-1",
        generation=1,
        step_id="late-step",
        action_kind=ActuationAction.OPEN,
    )
    late_receipt = ActuationReceipt.from_write(
        command=late_command,
        started_ns=1,
        actual_ns=2,
        wall_timestamp=1_700_000_000,
        result=ActuationResult.SUCCESS,
    )

    worker.consume_receipt(late_receipt)
    worker.process_ready()

    assert any(
        command.category == ActuationCategory.SAFETY
        and command.action == ActuationAction.CLOSE
        and command.target == "Dev1/P0.1"
        for command in calls
    )
    assert "Dev1/P0.1" not in worker.cleaning_snapshot.possibly_open


def test_cc03_recorder_queue_failure_is_latched_before_odor_open() -> None:
    worker, _clock, calls, flows, _recorder, token = _worker()
    queue_failed = threading.Event()

    class Recorder(_Recorder):
        def post_event(self, **values):
            if values.get("event") == "step_started":
                queue_failed.set()
                return False
            return super().post_event(**values)

    recorder = Recorder()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()

    assert queue_failed.is_set()
    assert not any(
        command.category == ActuationCategory.CLEANING
        and command.valve == 2
        and command.action == ActuationAction.OPEN
        for command in calls
    )
    assert any(
        command.category == ActuationCategory.SAFETY
        and command.action == ActuationAction.CLOSE
        for command in calls
    )
    assert worker.cleaning_snapshot.status in {
        CleaningStatus.STOPPING,
        CleaningStatus.FAILED,
        CleaningStatus.RECOVERY_REQUIRED,
    }


def test_all_configured_odor_union_is_20_and_selector_is_separate() -> None:
    variants = {
        "20-channel": {
            channel: (
                f"Dev1/P0.{channel - 1}"
                if channel <= 8
                else f"Dev1/P1.{channel - 9}"
                if channel <= 12
                else f"Dev2/P0.{channel - 13}"
            )
            for channel in range(1, 21)
        },
        "10-channel": {
            channel: (
                f"Dev1/P0.{channel - 1}"
                if channel <= 8
                else f"Dev1/P1.{channel - 9}"
            )
            for channel in range(1, 11)
        },
    }
    state = AppState(
        hardware_variant="10-channel",
        valve_variants=variants,
        master_valve_line="Dev2/P1.0",
    )
    service = ValveService(
        state=state,
        safety_manager=SafetyManager(),
        worker=object(),
        valve_variants=variants,
        hardware_variant="10-channel",
        master_valve_line="Dev2/P1.0",
    )

    targets = service.all_configured_close_steps()

    assert len(targets) == 20
    assert len({(step.device, step.line) for step in targets}) == 20
    assert all(step.logical_valve != 0 for step in targets)
    selector = service.selector_route_step(SelectorRoute.COMPENSATION)
    assert (selector.device, selector.line, selector.state) == ("Dev2", "P1.0", False)
    assert len(service.emergency_close_steps()) == 10


def test_low_flow_preempts_cleaning_and_only_reports_failed_after_close_and_zero() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    results = []
    worker.cleaning_result_ready.connect(results.append)
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    before = len(calls)

    worker.interlock.update(safety_state="LOW_FLOW")
    worker.post_interlock_changed()
    worker.process_ready()
    assert len(flows) == 1 and flows[0].mode == "zero"
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert all(
        command.category == ActuationCategory.SAFETY
        for command in calls[before:]
    )
    assert results[-1].status == CleaningStatus.FAILED
    assert worker.cleaning_owner_handoff_ready is True


def test_repeated_unsafe_updates_do_not_restart_cleaning_stop_close_set() -> None:
    worker, _clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()

    worker.interlock.update(safety_state="LOW_FLOW")
    worker.post_interlock_changed()
    worker.process_ready()
    assert len(flows) == 1 and flows[0].mode == "zero"
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()
    close_count = len(
        [
            command
            for command in calls
            if command.category == ActuationCategory.SAFETY
            and command.step_id.startswith("stop-close-")
        ]
    )
    assert close_count == 1
    assert worker.cleaning_snapshot.close_confirmed == 1

    for _ in range(5):
        worker.post_interlock_changed()
    worker.process_ready()

    repeated_close_count = len(
        [
            command
            for command in calls
            if command.category == ActuationCategory.SAFETY
            and command.step_id.startswith("stop-close-")
        ]
    )
    assert repeated_close_count == close_count
    assert worker.cleaning_snapshot.close_confirmed == 1


def test_cleaning_a_zero_receipt_timeout_never_routes_selector_safe() -> None:
    worker, clock, calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    selector_count_before_stop = len([call for call in calls if call.valve == 0])

    worker.post_cleaning_stop(reason="stop", aborted=True)
    worker.process_ready()
    assert len(flows) == 1 and flows[0].mode == "zero"

    clock.advance(2_000_000_001)
    worker.process_ready()

    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert "超时" in worker.cleaning_snapshot.recovery_reason
    assert len([call for call in calls if call.valve == 0]) == selector_count_before_stop

def test_uncertain_safety_close_keeps_possibly_open_and_requires_explicit_recovery() -> None:
    worker, _clock, _calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()
    original_writer = worker.writer

    def uncertain_close(command):
        if command.category == ActuationCategory.SAFETY:
            return ActuationReceipt.from_write(
                command=command,
                started_ns=command.expected_ns,
                actual_ns=None,
                wall_timestamp=1_700_000_000,
                result=ActuationResult.UNCERTAIN,
                message="ack 不确定",
            )
        return original_writer(command)

    worker.writer = uncertain_close
    worker.post_cleaning_stop(reason="stop", aborted=True)
    worker.process_ready()
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert worker.cleaning_snapshot.possibly_open
    assert worker.cleaning_owner_handoff_ready is False
    worker.interlock.update(safety_state="SAFE")
    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED

    worker.writer = original_writer
    assert worker.post_cleaning_recover()
    worker.process_ready()
    assert len(flows) == 1 and flows[0].mode == "zero"
    worker.post_flow_result(
        FlowCommandResult(
            command=flows.pop(),
            result=FlowApplyResult(True, "ok", 0, 0, 0, 0),
        )
    )
    worker.process_ready()

    assert worker.cleaning_snapshot.status == CleaningStatus.RECOVERY_REQUIRED
    assert worker.cleaning_snapshot.possibly_open == ()
    assert worker.cleaning_owner_handoff_ready is True


def test_identical_cleaning_receipt_replay_requires_recovery() -> None:
    worker, _clock, _calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    first_receipt = recorder.receipts[0][1]

    worker.consume_receipt(first_receipt)

    assert worker.cleaning_snapshot.status == CleaningStatus.STOPPING
    assert "重复投递" in worker.cleaning_snapshot.recovery_reason
    assert flows[-1].mode == "zero"


def test_conflicting_flow_start_receipt_submits_zero_before_recovery() -> None:
    worker, _clock, _calls, flows, recorder, token = _worker()
    worker.post_cleaning_start(_plan(), lease_token=token, recorder=recorder)
    worker.process_ready()
    flow_start = flows.pop()
    conflicting = replace(flow_start, generation=flow_start.generation + 1)

    worker.post_flow_result(
        FlowCommandResult(
            command=conflicting,
            result=FlowApplyResult(True, "conflict", 1500, 0, 0, 1500),
        )
    )
    worker.process_ready()

    assert worker.cleaning_snapshot.status == CleaningStatus.STOPPING
    assert "身份冲突" in worker.cleaning_snapshot.recovery_reason
    assert len(flows) == 1
    assert flows[0].mode == "zero"
    assert (flows[0].a, flows[0].b, flows[0].c) == (0, 0, 0)
