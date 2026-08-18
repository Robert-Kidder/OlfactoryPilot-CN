from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import (
    ActuationAction,
    ActuationReceipt,
    ActuationResult,
    AppState,
    DeviceLeaseKind,
    DeviceLeaseToken,
    ManualExperimentIdentity,
    ManualExperimentPlan,
    ManualExperimentStatus,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
)
from app.services import SafetyManager, ValveService
from app.services.flow_service import FlowApplyResult
from app.workers.actuation_worker import (
    ActuationInterlockIngress,
    ActuationWorker,
    InterlockSnapshot,
)
from app.workers.flow_worker import FlowCommandResult


class FakeClock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class DummyHardwareWorker:
    is_connected = True

    def write_digital(self, **_kwargs) -> bool:
        return True


def _config() -> dict:
    return json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))


def _fixture(*, writer_mutator=None, duration_ns: int = 50):
    clock = FakeClock()
    state = AppState.from_config(_config())
    state.simulation_mode = True
    valve_service = ValveService(
        state=state,
        safety_manager=SafetyManager(),
        worker=DummyHardwareWorker(),
        valve_variants=state.valve_variants,
        hardware_variant=state.hardware_variant,
        selector=state.selector,
    )
    protocol_state = ProtocolExecutionState(
        status=ProtocolExecutionStatus.WAITING_EXHALE,
        execution_epoch=1,
        arm_epoch=2,
    )
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=False,
            safety_state="SAFE",
            device_lease="manual",
        )
    )
    written = []

    def writer(command):
        written.append(command)
        actual = clock.value
        if command.step_id == "manual-open:2":
            actual = clock.value + 20
        elif command.step_id == "manual-open:4":
            actual = clock.value + 50
        clock.value = max(clock.value, actual)
        receipt = ActuationReceipt.from_write(
            command=command,
            started_ns=actual,
            actual_ns=actual,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )
        return receipt if writer_mutator is None else writer_mutator(command, receipt)

    flows = []
    worker = ActuationWorker(
        protocol_state=protocol_state,
        valve_service=valve_service,
        writer=writer,
        interlock=ingress,
        monotonic_ns_clock=clock,
        wall_clock=lambda: 10.0,
        flow_submitter=lambda command: flows.append(command) is None,
        manual_receipt_timeout_ms=1,
    )
    identity = ManualExperimentIdentity("manual-1", 1, 1)
    plan = ManualExperimentPlan.from_registry(
        identity=identity,
        flow_setpoints=state.hardware_profile.flow_setpoints(
            total_sccm=1000,
            sample_a_sccm=250,
            vacuum_c_sccm=100,
        ),
        selector=state.selector,
        registry=state.channel_registry,
        external_ports=(2, 4),
        duration_ns=duration_ns,
        allow_mock=True,
    )
    lease = DeviceLeaseToken(DeviceLeaseKind.MANUAL, "manual-1", 1, "lease-1")
    return worker, protocol_state, ingress, clock, plan, lease, flows, written


def _accept_flow(worker, flows) -> None:
    command = flows[0]
    worker.post_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(
                True,
                "ok",
                command.a,
                command.b,
                command.c,
                command.a,
            ),
        )
    )


def _start_to_stimulating(worker, plan, lease, flows) -> None:
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    assert worker.manual_snapshot.status is ManualExperimentStatus.FLOW_PENDING
    _accept_flow(worker, flows)
    assert worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING


def _finish_normal_completion(worker, flows) -> None:
    zero = flows[-1]
    assert zero.mode == "manual_post_close_a_zero"
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )
    restore = flows[-1]
    assert restore.mode == "manual_restore_supply"
    worker.post_flow_result(
        FlowCommandResult(
            command=restore,
            result=FlowApplyResult(
                True, "supply restored", restore.a, restore.b, restore.c, restore.a + restore.c
            ),
        )
    )


def test_manual_plan_requires_mode_scoped_verification() -> None:
    state = AppState.from_config(_config())
    identity = ManualExperimentIdentity("manual", 1, 1)
    kwargs = {
        "identity": identity,
        "flow_setpoints": state.hardware_profile.flow_setpoints(
            total_sccm=1000,
            sample_a_sccm=100,
            vacuum_c_sccm=0,
        ),
        "selector": state.selector,
        "registry": state.channel_registry,
        "external_ports": (2,),
        "duration_ns": 100,
    }
    with pytest.raises(ValueError, match="验证证据"):
        ManualExperimentPlan.from_registry(**kwargs)
    assert ManualExperimentPlan.from_registry(**kwargs, allow_mock=True).targets[0].external_port == 2


def test_manual_cohort_uses_latest_open_receipt_and_owner_deadline() -> None:
    worker, _, _, clock, plan, lease, flows, written = _fixture()
    initial_ns = clock.value

    _start_to_stimulating(worker, plan, lease, flows)

    assert [command.step_id for command in written[:3]] == [
        "selector_odor",
        "manual-open:2",
        "manual-open:4",
    ]
    assert worker.manual_snapshot.ready_ns == initial_ns + 70
    assert worker.manual_snapshot.deadline_ns == initial_ns + 120
    assert worker.manual_snapshot.open_confirmed == (2, 4)
    clock.value = worker.manual_snapshot.deadline_ns - 1
    assert worker.process_ready() == 0
    clock.value += 1
    worker.process_ready()
    _finish_normal_completion(worker, flows)

    assert worker.manual_snapshot.status is ManualExperimentStatus.COMPLETED
    assert worker.manual_snapshot.flow_zero_confirmed
    assert worker.manual_snapshot.selector_compensation_confirmed
    assert worker.manual_snapshot.supply_restored
    assert worker.manual_snapshot.close_confirmed == (2, 4)
    assert [command.action for command in written[-2:]] == [
        ActuationAction.CLOSE,
        ActuationAction.CLOSE,
    ]


def test_manual_open_receipts_may_arrive_in_reverse_order() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    worker._consume_manual_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(True, "ok", command.a, command.b, command.c, command.a),
        )
    )
    worker.process_ready(max_items=1)  # selector receipt creates the open cohort
    open_commands = [item[3] for item in worker._normal_heap]
    worker._normal_heap.clear()
    cohort_started_ns = clock.value

    for offset, command in zip((80, 20), reversed(open_commands), strict=True):
        clock.value = max(clock.value, clock.value + offset)
        worker.consume_receipt(
            ActuationReceipt.from_write(
                command=command,
                started_ns=clock.value,
                actual_ns=clock.value,
                wall_timestamp=10.0,
                result=ActuationResult.SUCCESS,
            )
        )

    assert worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING
    assert worker.manual_snapshot.ready_ns == cohort_started_ns + 100
    assert worker.manual_snapshot.deadline_ns == cohort_started_ns + 150


def test_partial_open_cohort_timeout_fails_closed_without_sleep() -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    worker._consume_manual_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(True, "ok", command.a, command.b, command.c, command.a),
        )
    )
    worker.process_ready(max_items=1)  # selector
    worker.process_ready(max_items=1)  # first open only
    command_ids = tuple(
        command_id
        for command_id, expected in worker._manual_expected.items()
        if expected["role"] == "open"
    )

    worker._handle_manual_receipt_timeout(
        identity=plan.identity,
        phase="open",
        command_ids=command_ids,
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "cohort 不完整" in worker.manual_snapshot.recovery_reason


@pytest.mark.parametrize("failure", ["stale", "wrong_identity", "missing_actual", "failed"])
def test_manual_invalid_or_failed_open_receipt_enters_recovery(failure) -> None:
    def mutate(command, receipt):
        if command.step_id != "manual-open:4":
            return receipt
        if failure == "stale":
            return replace(receipt, stale=True)
        if failure == "wrong_identity":
            return replace(receipt, generation=99)
        if failure == "missing_actual":
            return replace(receipt, actual_ns=None)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=receipt.started_ns,
            actual_ns=receipt.actual_ns,
            wall_timestamp=10.0,
            result=ActuationResult.FAILED,
            message="fault",
        )

    worker, _, _, _, plan, lease, flows, _ = _fixture(writer_mutator=mutate)
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    _accept_flow(worker, flows)

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "RECOVERY_REQUIRED" in worker.manual_snapshot.recovery_reason
    assert worker._background_safe_stop_plan is not None


def test_manual_duplicate_conflict_invalidates_completed_open_evidence() -> None:
    worker, _, _, _, plan, lease, flows, written = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    open_command = next(command for command in written if command.step_id == "manual-open:2")
    prior = worker._seen_receipts[open_command.command_id]

    worker.consume_receipt(replace(prior, message="conflict"))

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "receipt 内容冲突" in worker.manual_snapshot.recovery_reason


def test_manual_close_failure_is_not_reported_complete() -> None:
    def mutate(command, receipt):
        if command.step_id == "manual-close:4":
            return ActuationReceipt.from_write(
                command=command,
                started_ns=receipt.started_ns,
                actual_ns=receipt.actual_ns,
                wall_timestamp=10.0,
                result=ActuationResult.FAILED,
                message="close fault",
            )
        return receipt

    worker, _, _, clock, plan, lease, flows, _ = _fixture(writer_mutator=mutate)
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "close fault" in worker.manual_snapshot.recovery_reason


@pytest.mark.parametrize("preemption", ["lease", "epoch", "stop"])
def test_manual_preemption_fails_closed(preemption) -> None:
    worker, state, ingress, _, plan, lease, flows, _ = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)

    if preemption == "lease":
        ingress.update(device_lease="protocol")
        worker.post_interlock_changed()
    elif preemption == "epoch":
        state.execution_epoch += 1
        worker.post_interlock_changed()
    else:
        worker.post_stop(message="stop")
    worker.process_ready()

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker._background_safe_stop_plan is not None


def test_manual_cannot_restart_directly_from_recovery_required() -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    worker.post_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(False, "flow fault", command.a, command.b, command.c, 0),
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert not worker.post_manual_start(plan, lease_token=lease)
    worker._begin_manual(plan=plan, lease_token=lease)
    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert sum(command.source == "manual:experiment" for command in flows) == 1


def test_owner_restart_after_explicit_shutdown_rebuilds_manual_admission() -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    worker.post_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(False, "flow fault", command.a, command.b, command.c, 0),
        )
    )
    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED

    assert worker.shutdown(10)
    assert worker.prepare_restart()

    assert worker.manual_snapshot.status is ManualExperimentStatus.IDLE


@pytest.mark.parametrize("failure", ["stale", "values", "identity"])
def test_manual_invalid_flow_result_fails_closed(failure) -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    result_command = replace(command, generation=99) if failure == "identity" else command
    result = FlowApplyResult(
        True,
        "ok",
        command.a + (1 if failure == "values" else 0),
        command.b,
        command.c,
        command.a,
    )
    worker.post_flow_result(
        FlowCommandResult(
            command=result_command,
            result=result,
            stale=failure == "stale",
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker._background_safe_stop_plan is not None
    assert not worker._background_safe_stop_plan.selector_allowed


@pytest.mark.parametrize("failure", ["stale", "failed", "wrong_identity"])
def test_manual_invalid_selector_receipt_fails_closed_before_open(failure) -> None:
    def mutate(command, receipt):
        if command.step_id != "selector_odor":
            return receipt
        if failure == "stale":
            return replace(receipt, stale=True)
        if failure == "wrong_identity":
            return replace(receipt, generation=99)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=receipt.started_ns,
            actual_ns=receipt.actual_ns,
            wall_timestamp=10.0,
            result=ActuationResult.FAILED,
            message="selector fault",
        )

    worker, _, _, _, plan, lease, flows, written = _fixture(writer_mutator=mutate)
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    _accept_flow(worker, flows)

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker._background_safe_stop_plan is not None
    assert not worker._background_safe_stop_plan.selector_allowed
    assert all(not command.step_id.startswith("manual-open:") for command in written)


def test_late_success_after_open_timeout_keeps_recovery_without_deadline() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    command = flows[0]
    worker._consume_manual_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(True, "ok", command.a, command.b, command.c, command.a),
        )
    )
    worker.process_ready(max_items=1)
    open_commands = [item[3] for item in worker._normal_heap]
    worker._normal_heap.clear()
    command_ids = tuple(command.command_id for command in open_commands)
    worker._handle_manual_receipt_timeout(
        identity=plan.identity,
        phase="open",
        command_ids=command_ids,
    )
    late = open_commands[0]
    worker.consume_receipt(
        ActuationReceipt.from_write(
            command=late,
            started_ns=clock.value,
            actual_ns=clock.value,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker.manual_snapshot.deadline_ns is None
    assert worker._background_safe_stop_plan is not None
    assert all(item[3] != "manual_deadline" for item in worker._deadline_heap)


def test_conflicting_duplicate_after_completion_relocks_recovery() -> None:
    worker, _, _, clock, plan, lease, flows, written = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    _finish_normal_completion(worker, flows)
    assert worker.manual_snapshot.status is ManualExperimentStatus.COMPLETED
    close_command = next(command for command in written if command.step_id == "manual-close:2")
    prior = worker._seen_receipts[close_command.command_id]

    worker.consume_receipt(replace(prior, message="terminal conflict"))

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker._background_safe_stop_plan is not None


def test_conflicting_duplicate_while_recovery_required_is_not_ignored() -> None:
    def mutate(command, receipt):
        if command.step_id == "manual-open:4":
            return replace(receipt, stale=True, message="open stale")
        return receipt

    worker, _, _, _, plan, lease, flows, written = _fixture(writer_mutator=mutate)
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    _accept_flow(worker, flows)
    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    open_command = next(command for command in written if command.step_id == "manual-open:4")
    prior = worker._seen_receipts[open_command.command_id]

    worker.consume_receipt(replace(prior, message="recovery conflict"))

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "receipt 内容冲突" in worker.manual_snapshot.recovery_reason
    assert worker._background_safe_stop_plan is not None


@pytest.mark.parametrize("failure", ["stale", "failed"])
def test_manual_post_close_a_zero_failure_never_authorizes_compensation(failure) -> None:
    worker, _, _, clock, plan, lease, flows, written = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(
                failure != "failed", "zero fault", zero.a, zero.b, zero.c, zero.c
            ),
            stale=failure == "stale",
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert not worker.manual_snapshot.flow_zero_confirmed
    assert all(command.step_id != "selector_compensation" for command in written)
    assert worker._background_safe_stop_plan is not None


def test_manual_compensation_failure_never_restores_nonzero_a() -> None:
    def mutate(command, receipt):
        if command.step_id == "selector_compensation":
            return ActuationReceipt.from_write(
                command=command,
                started_ns=receipt.started_ns,
                actual_ns=receipt.actual_ns,
                wall_timestamp=10.0,
                result=ActuationResult.FAILED,
                message="compensation fault",
            )
        return receipt

    worker, _, _, clock, plan, lease, flows, _ = _fixture(writer_mutator=mutate)
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker.manual_snapshot.flow_zero_confirmed
    assert not worker.manual_snapshot.selector_compensation_confirmed
    assert all(command.mode != "manual_restore_supply" for command in flows)
    assert worker._background_safe_stop_plan is not None


def test_manual_restore_supply_failure_is_not_completed() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )
    restore = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=restore,
            result=FlowApplyResult(
                False, "restore fault", restore.a, restore.b, restore.c, restore.c
            ),
        )
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker.manual_snapshot.selector_compensation_confirmed
    assert not worker.manual_snapshot.supply_restored
    assert worker._background_safe_stop_plan is not None


def test_manual_receipt_actual_time_after_phase_deadline_is_rejected() -> None:
    def mutate(command, receipt):
        if command.step_id == "selector_odor":
            late = command.expected_ns + 2_000_000
            return ActuationReceipt.from_write(
                command=command,
                started_ns=late,
                actual_ns=late,
                wall_timestamp=10.0,
                result=ActuationResult.SUCCESS,
            )
        return receipt

    worker, _, _, _, plan, lease, flows, _ = _fixture(writer_mutator=mutate)
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    _accept_flow(worker, flows)

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert "deadline" in worker.manual_snapshot.recovery_reason
    assert worker._background_safe_stop_plan is not None


def test_successful_a_zero_makes_its_old_timeout_harmless() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )
    assert worker.manual_snapshot.status is ManualExperimentStatus.RESTORING_SUPPLY

    worker._handle_manual_receipt_timeout(
        identity=plan.identity,
        phase="flow_zero",
        command_ids=(zero.command_id,),
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RESTORING_SUPPLY


def test_restore_supply_timeout_fails_closed() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )
    restore = flows[-1]

    worker._handle_manual_receipt_timeout(
        identity=plan.identity,
        phase="restore_supply",
        command_ids=(restore.command_id,),
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
    assert worker._background_safe_stop_plan is not None


def test_successful_restore_supply_makes_queued_timeout_harmless() -> None:
    worker, _, _, clock, plan, lease, flows, _ = _fixture()
    _start_to_stimulating(worker, plan, lease, flows)
    clock.value = worker.manual_snapshot.deadline_ns
    worker.process_ready()
    zero = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=zero,
            result=FlowApplyResult(True, "A=0", zero.a, zero.b, zero.c, zero.c),
        )
    )
    restore = flows[-1]
    worker.post_flow_result(
        FlowCommandResult(
            command=restore,
            result=FlowApplyResult(
                True,
                "restored",
                restore.a,
                restore.b,
                restore.c,
                restore.a,
            ),
        )
    )
    assert worker.manual_snapshot.status is ManualExperimentStatus.COMPLETED

    worker._handle_manual_receipt_timeout(
        identity=plan.identity,
        phase="restore_supply",
        command_ids=(restore.command_id,),
    )

    assert worker.manual_snapshot.status is ManualExperimentStatus.COMPLETED


def test_blocked_writer_late_success_receipt_is_rejected_by_current_clock() -> None:
    entered = threading.Event()
    release = threading.Event()
    terminal = threading.Event()

    def block_selector(command, receipt):
        if command.step_id == "selector_odor":
            entered.set()
            assert release.wait(1.0)
            return replace(receipt, actual_ns=clock.value)
        return receipt

    worker, _, _, clock, plan, lease, flows, _ = _fixture(
        writer_mutator=block_selector
    )
    fail_manual = worker._fail_manual

    def observe_failure(*args, **kwargs):
        fail_manual(*args, **kwargs)
        terminal.set()

    worker._fail_manual = observe_failure
    assert worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready(max_items=1)
    worker.start()
    try:
        command = flows[0]
        worker.post_flow_result(
            FlowCommandResult(
                command=command,
                result=FlowApplyResult(
                    True,
                    "ok",
                    command.a,
                    command.b,
                    command.c,
                    command.a,
                ),
            )
        )
        assert entered.wait(1.0)
        deadline = next(iter(worker._manual_expected.values()))["deadline_ns"]
        clock.value = deadline + 1
        release.set()
        assert terminal.wait(1.0)
        assert worker.manual_snapshot.status is ManualExperimentStatus.RECOVERY_REQUIRED
        assert worker.manual_snapshot.ready_ns is None
    finally:
        release.set()
        worker.shutdown()


def test_manual_begin_resets_all_completion_evidence_and_double_post_is_guarded() -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    assert worker.post_manual_start(plan, lease_token=lease)
    assert not worker.post_manual_start(plan, lease_token=lease)
    worker.process_ready()
    assert sum(command.source == "manual:experiment" for command in flows) == 1
    worker._manual_snapshot = replace(
        worker.manual_snapshot,
        status=ManualExperimentStatus.COMPLETED,
        flow_zero_confirmed=True,
        selector_compensation_confirmed=True,
        supply_restored=True,
        supply_enabled=True,
    )
    next_plan = replace(
        plan,
        identity=replace(plan.identity, operation_id="manual-2", generation=2),
    )
    next_lease = replace(lease, operation_id="manual-2", generation=2)

    assert worker.post_manual_start(next_plan, lease_token=next_lease)
    worker.process_ready(max_items=1)

    snapshot = worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.FLOW_PENDING
    assert not snapshot.flow_zero_confirmed
    assert not snapshot.selector_compensation_confirmed
    assert not snapshot.supply_restored
    assert snapshot.supply_enabled is True
    assert not snapshot.supply_transitioning


def test_pending_manual_start_is_cancelled_by_stop_before_owner_consumes_it() -> None:
    worker, _, _, _, plan, lease, flows, _ = _fixture()
    results = []
    worker.manual_result_ready.connect(results.append)

    assert worker.post_manual_start(plan, lease_token=lease)
    assert worker.post_manual_stop(reason="取消尚未开始的手动实验")
    worker.process_ready()

    assert worker.manual_snapshot.status is ManualExperimentStatus.IDLE
    assert flows == []
    assert not worker._manual_start_pending
    assert results[-1].identity == plan.identity
    assert results[-1].outcome.value == "aborted"
    assert worker.post_manual_start(plan, lease_token=lease)


def test_current_registry_close_failure_is_not_cleared_by_legacy_alias_success() -> None:
    failed_target = "Dev2/P1.1"

    def fail_current_target(command, receipt):
        if command.valve == 2 and command.target == failed_target:
            return replace(
                receipt,
                result=ActuationResult.FAILED,
                actual_ns=None,
                message="current target failed",
            )
        return receipt

    worker, protocol_state, _, _, _, _, _, written = _fixture(
        writer_mutator=fail_current_target
    )
    profile = worker.valve_service.state.hardware_profile
    channels = list(profile.channels)
    channels[1] = replace(channels[1], target=failed_target)
    worker.valve_service.rebind_profile(replace(profile, channels=tuple(channels)))
    protocol_state.possibly_open_valves.add(2)

    worker._submit_all_configured_closes(reason="test remap safety close")
    worker.process_ready()

    valve_two_targets = [command.target for command in written if command.valve == 2]
    assert valve_two_targets[:2] == [failed_target, "Dev1/P0.1"]
    assert 2 in protocol_state.possibly_open_valves
    assert "RECOVERY_REQUIRED" in protocol_state.quality_block_reason
