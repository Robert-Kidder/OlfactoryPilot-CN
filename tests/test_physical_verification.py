from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.controllers import MainController
from app.models import (
    AppState,
    DeviceLeaseKind,
    HardwareVerificationPhase,
    PhysicalVerificationPlan,
    VerificationStatus,
)
from app.services import FlowApplyResult, MockHAL
from app.workers import FlowCommandResult, HardwareWorker


class FakeClock:
    def __init__(self, value: int | None = None) -> None:
        self.value = int(value or time.perf_counter_ns())

    def __call__(self) -> int:
        return self.value


def _controller(
    tmp_path: Path,
    *,
    verification_config: dict[str, float] | None = None,
) -> tuple[MainController, FakeClock, MockHAL]:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    if verification_config is not None:
        config["hardware_profile"]["verification_config"] = verification_config
    config["_local_config_path"] = str(tmp_path / "local_config.json")
    state = AppState.from_config(config)
    state.simulation_mode = False
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.hardware_ready = True
    state.flow_setpoints_ready = False
    clock = FakeClock()
    hal = MockHAL(monotonic_ns_clock=clock)
    controller = MainController(
        state,
        HardwareWorker(hal=hal, simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    controller.actuation_worker._clock_ns = clock
    return controller, clock, hal


def _publish_fresh_safe(controller: MainController) -> None:
    timestamp = max(
        time.time(),
        controller.actuation_interlock.read()[1].airflow_sample_timestamp + 0.001,
    )
    controller.actuation_interlock.publish_airflow(
        airflow=1500.0,
        timestamp=timestamp,
        hardware_state="SAFE",
    )
    controller.actuation_worker.post_interlock_changed(timestamp=timestamp)
    controller._drain_actuation_if_not_running()


def test_physical_verification_uses_exact_start_stop_order_and_complete_evidence(
    tmp_path,
) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    events: list[tuple[str, str]] = []
    controller.actuation_worker.receipt_ready.connect(
        lambda receipt: events.append(("do", str(receipt.step_id)))
        if receipt.operation_id and receipt.operation_id.startswith("physical-verify-")
        else None
    )
    controller.actuation_worker.flow_result_ready.connect(
        lambda wrapped: events.append(("flow", wrapped.command.mode))
        if wrapped.command.source == "verification"
        else None
    )

    controller.handle_hardware_physical_verify_requested(2)
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert controller._hardware_verification_run is not None
    assert controller._hardware_verification_run.phase is HardwareVerificationPhase.PREPARING
    assert events[:20] == [("do", f"initial-close-{index}") for index in range(1, 21)]
    assert events[20] == ("flow", "verification")

    _publish_fresh_safe(controller)
    run = controller._hardware_verification_run
    assert run is not None and run.phase is HardwareVerificationPhase.RUNNING
    assert events[21:23] == [("do", "selector_odor"), ("do", "target_open")]

    assert controller.handle_hardware_verification_result_requested(2, True)
    assert events[23:26] == [
        ("do", "target_close"),
        ("flow", "verification_a_zero"),
        ("do", "selector_safe"),
    ]
    assert [event for event in events[26:] if event[0] == "do"] == [
        ("do", f"other-close-{index}")
        for index in range(1, 21)
        if index != 2
    ]
    assert events[26] == ("flow", "verification_zero")
    evidence = controller.state.hardware_profile.registry.by_external_port(2).verification
    assert evidence.status is VerificationStatus.PHYSICAL_VERIFIED
    assert evidence.run_identity.startswith("physical-verify-2-")
    assert evidence.profile_revision == 0
    assert evidence.ni_target == "Dev1/P0.1"
    assert evidence.flow_setpoint_sccm == 1500.0
    assert evidence.flow_readback_sccm == 1500.0
    assert evidence.opened_at_ns is not None
    assert evidence.closed_at_ns is not None
    assert evidence.action_completed
    assert evidence.safe_closed
    assert evidence.authorized
    assert evidence.user_confirmed
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


@pytest.mark.parametrize(
    ("positive", "expected"),
    ((False, VerificationStatus.FAILED),),
)
def test_early_negative_closes_immediately_without_physical_success(
    tmp_path,
    positive,
    expected,
) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)

    assert controller.handle_hardware_verification_result_requested(2, positive)
    evidence = controller.state.hardware_profile.registry.by_external_port(2).verification
    assert evidence.status is expected
    assert evidence.safe_closed
    assert evidence.status is not VerificationStatus.PHYSICAL_VERIFIED
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_immediate_stop_safe_closes_and_records_incomplete(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)

    controller.handle_hardware_verification_stop_requested(2)

    evidence = controller.state.hardware_profile.registry.by_external_port(2).verification
    assert evidence.status is VerificationStatus.INCOMPLETE
    assert evidence.action_completed
    assert evidence.safe_closed
    assert not evidence.user_confirmed
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_timeout_safe_closes_then_waits_for_confirmation(tmp_path) -> None:
    controller, clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    run = controller._hardware_verification_run
    assert run is not None and run.deadline_ns is not None

    clock.value = run.deadline_ns
    controller._drain_actuation_if_not_running()

    awaiting = controller._hardware_verification_run
    assert awaiting is not None
    assert awaiting.phase is HardwareVerificationPhase.AWAITING_CONFIRMATION
    assert awaiting.physical_contract is not None
    assert awaiting.physical_contract.safe_closed
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert controller.handle_hardware_verification_result_requested(2, True)
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.PHYSICAL_VERIFIED
    )


def test_invalid_flow_is_rejected_before_lease_or_hardware_intent(tmp_path) -> None:
    controller, _clock, hal = _controller(tmp_path)
    store = controller._hardware_profile_store
    assert store is not None
    object.__setattr__(
        store.profile.verification_config,
        "flow_sccm",
        store.profile.max_sample_a_sccm + 1,
    )

    controller.handle_hardware_physical_verify_requested(2)

    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert not hal.flow_commands
    assert controller._hardware_verification_run is None


def test_competing_owner_rejects_before_any_hardware_intent(tmp_path) -> None:
    controller, _clock, hal = _controller(tmp_path)
    token = controller.flow_worker.acquire_lease(
        DeviceLeaseKind.MANUAL,
        operation_id="competing-manual",
        generation=0,
    )
    assert token is not None

    controller.handle_hardware_physical_verify_requested(2)

    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    assert not hal.flow_commands
    assert not hal._digital_state
    assert controller._hardware_verification_run is None
    assert controller.flow_worker.release_lease(token)


def test_duplicate_exact_receipt_fails_closed_and_requires_recovery(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    receipts = []
    controller.actuation_worker.receipt_ready.connect(receipts.append)
    controller.handle_hardware_physical_verify_requested(2)
    assert receipts

    controller.actuation_worker.consume_receipt(receipts[0])
    controller._drain_actuation_if_not_running()

    evidence = controller.state.hardware_profile.registry.by_external_port(2).verification
    assert evidence.status is not VerificationStatus.PHYSICAL_VERIFIED
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


def test_late_flow_receipt_does_not_advance_and_starts_fail_closed(tmp_path) -> None:
    controller, clock, _hal = _controller(tmp_path)
    submitted = []
    controller.actuation_worker._flow_submitter = (
        lambda command: submitted.append(command) or True
    )
    controller.handle_hardware_physical_verify_requested(2)
    assert len(submitted) == 1
    command = submitted[0]
    deadline_ns = controller.actuation_worker._verification_receipt_deadlines[
        command.command_id
    ]
    clock.value = deadline_ns + 1

    controller.actuation_worker.post_flow_result(
        FlowCommandResult(
            command=command,
            result=FlowApplyResult(True, "ok", 1500, 0, 0, 1500),
        )
    )
    controller._drain_actuation_if_not_running()

    assert controller.actuation_worker._verification_opened_at_ns is None
    assert controller.actuation_worker._verification_phase == "idle"
    assert [item.mode for item in submitted] == [
        "verification",
        "verification_a_zero",
    ]
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is not VerificationStatus.PHYSICAL_VERIFIED
    )
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


def test_runtime_disconnect_fails_closed_without_physical_success(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    controller.state.telemetry.connected = False

    controller._handle_hardware_verification_tick()

    evidence = controller.state.hardware_profile.registry.by_external_port(2).verification
    assert evidence.status is not VerificationStatus.PHYSICAL_VERIFIED
    assert controller._hardware_verification_run is None


def test_positive_after_deadline_becomes_timeout_confirmation_not_success(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    run = controller._hardware_verification_run
    assert run is not None
    controller._hardware_verification_run = replace(
        run,
        deadline_ns=time.monotonic_ns() - 1,
    )

    assert not controller.handle_hardware_verification_result_requested(2, True)

    awaiting = controller._hardware_verification_run
    assert awaiting is not None
    assert awaiting.phase is HardwareVerificationPhase.AWAITING_CONFIRMATION
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is not VerificationStatus.PHYSICAL_VERIFIED
    )


def test_stop_while_physical_start_is_queued_wins_without_hardware_action(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _clock, hal = _controller(tmp_path)
    drain = controller._drain_actuation_if_not_running
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)

    controller.handle_hardware_physical_verify_requested(2)
    controller.handle_hardware_verification_stop_requested(2)
    assert controller.actuation_worker._verification_start_pending

    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", drain)
    drain()

    assert not hal.flow_commands
    assert not hal._digital_state
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_shutdown_rejects_queued_physical_start_and_releases_lease(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _clock, hal = _controller(tmp_path)
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)
    controller.handle_hardware_physical_verify_requested(2)

    assert controller.actuation_worker.shutdown()

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert not hal.flow_commands
    assert not hal._digital_state


def test_flow_wait_safe_has_bounded_timeout_and_converges_fail_closed(tmp_path) -> None:
    controller, clock, hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    assert controller.actuation_worker._verification_phase == "flow_wait_safe"
    deadline = next(
        item[0]
        for item in controller.actuation_worker._deadline_heap
        if item[3] == "verification_flow_ready_timeout"
    )

    clock.value = deadline
    controller._drain_actuation_if_not_running()

    assert ("A", 0.0, False) in hal.flow_commands
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


def test_stop_during_flow_wait_safe_converges_and_releases_lease(tmp_path) -> None:
    controller, _clock, hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    assert controller.actuation_worker._verification_phase == "flow_wait_safe"

    controller.handle_hardware_verification_stop_requested(2)

    assert ("A", 0.0, False) in hal.flow_commands
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_lost_initial_close_receipt_times_out_without_stranding_owner(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    drain = controller._drain_actuation_if_not_running
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)
    controller.handle_hardware_physical_verify_requested(2)
    controller.actuation_worker.process_ready(max_items=1)
    plan = controller.actuation_worker._verification_plan
    assert plan is not None
    command_id = next(iter(controller.actuation_worker._verification_expected))

    controller.actuation_worker._handle_physical_verification_receipt_timeout(
        run_identity=plan.run_identity,
        command_id=command_id,
    )
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", drain)
    drain()

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


def test_recurring_safe_readiness_does_not_abort_running_verification(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)

    _publish_fresh_safe(controller)
    _publish_fresh_safe(controller)

    run = controller._hardware_verification_run
    assert run is not None
    assert run.phase is HardwareVerificationPhase.RUNNING


def test_recurring_safe_readiness_during_closing_keeps_requested_outcome(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    drain = controller._drain_actuation_if_not_running
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)
    assert controller.handle_hardware_verification_result_requested(2, True)
    controller.actuation_worker.process_ready(max_items=1)
    assert controller.actuation_worker._verification_phase == "target_close"

    controller.actuation_worker._handle_message("readiness", {})
    assert not controller.actuation_worker._verification_recovery_required
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", drain)
    drain()

    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.PHYSICAL_VERIFIED
    )


def test_unsafe_readiness_while_waiting_for_fresh_flow_fails_closed(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    assert controller.actuation_worker._verification_phase == "flow_wait_safe"
    controller.actuation_interlock.update(safety_state="LOW_FLOW")

    controller.actuation_worker.post_interlock_changed(timestamp=time.time())
    controller._drain_actuation_if_not_running()

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


def test_first_rapid_finish_request_wins(tmp_path, monkeypatch) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    drain = controller._drain_actuation_if_not_running
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)

    assert controller.handle_hardware_verification_result_requested(2, True)
    assert not controller.handle_hardware_verification_result_requested(2, False)
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", drain)
    drain()

    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.PHYSICAL_VERIFIED
    )


def test_close_before_open_is_recovery_required_without_worker_exception(tmp_path) -> None:
    controller, clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)
    opened_at = controller.actuation_worker._verification_opened_at_ns
    assert opened_at is not None
    clock.value = opened_at - 1

    assert controller.handle_hardware_verification_result_requested(2, True)

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


def test_close_step_construction_exception_releases_lease_without_action(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _clock, hal = _controller(tmp_path)
    monkeypatch.setattr(
        controller.valve_service,
        "all_configured_close_steps",
        lambda: (_ for _ in ()).throw(RuntimeError("close-plan-fault")),
    )

    controller.handle_hardware_physical_verify_requested(2)

    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert controller._hardware_verification_run is None
    assert not hal.flow_commands
    assert not hal._digital_state


def test_partial_verification_flow_failure_converges_and_holds_lease(tmp_path) -> None:
    controller, _clock, hal = _controller(tmp_path)
    hal.fail_on.add("C")

    controller.handle_hardware_physical_verify_requested(2)

    assert ("B", 0.0, False) in hal.flow_commands
    assert ("C", 0.0, False) in hal.flow_commands
    assert ("A", 0.0, False) in hal.flow_commands
    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


@pytest.mark.parametrize("readback", (None, 1499.0))
def test_missing_or_mismatched_alicat_setpoint_readback_fails_closed(
    tmp_path,
    monkeypatch,
    readback,
) -> None:
    controller, _clock, hal = _controller(tmp_path)
    original = hal.last_setpoint_readback_sccm
    monkeypatch.setattr(
        hal,
        "last_setpoint_readback_sccm",
        lambda channel: readback if channel == "A" else original(channel),
    )

    controller.handle_hardware_physical_verify_requested(2)

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION


def test_nondefault_persisted_flow_and_duration_drive_physical_plan(tmp_path) -> None:
    controller, _clock, hal = _controller(
        tmp_path,
        verification_config={
            "flow_sccm": 1400.0,
            "duration_s": 30.0,
            "max_approved_flow_sccm": 1500.0,
        },
    )
    controller.handle_hardware_physical_verify_requested(2)
    _publish_fresh_safe(controller)

    run = controller._hardware_verification_run
    assert run is not None
    assert ("A", 1400.0, False) in hal.flow_commands
    assert run.duration_s == 30.0
    assert run.started_ns is not None
    assert run.deadline_ns == run.started_ns + 30_000_000_000


def test_plan_requires_exact_unique_valves_and_matching_selected_entry(tmp_path) -> None:
    controller, _clock, _hal = _controller(tmp_path)
    controller.handle_hardware_physical_verify_requested(2)
    plan = controller.actuation_worker._verification_plan
    assert isinstance(plan, PhysicalVerificationPlan)

    duplicate_valve = list(plan.close_targets)
    duplicate_valve[0] = (2, duplicate_valve[0][1], duplicate_valve[0][2])
    with pytest.raises(ValueError):
        replace(plan, close_targets=tuple(duplicate_valve))
    wrong_selected = tuple(
        (valve, target, not level) if valve == plan.internal_valve else (valve, target, level)
        for valve, target, level in plan.close_targets
    )
    with pytest.raises(ValueError):
        replace(plan, close_targets=wrong_selected)
