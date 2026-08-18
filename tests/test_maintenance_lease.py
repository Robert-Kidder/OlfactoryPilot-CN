from __future__ import annotations

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    DeviceLeaseKind,
    ExclusiveDeviceLease,
    MaintenanceLeaseReleaseEvidence,
)
from app.services.flow_service import FlowApplyResult
from app.workers.actuation_worker import InterlockSnapshot
from app.workers.flow_worker import FlowCommand, FlowWorker


class _FlowService:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float, float]] = []

    def apply_flows(self, **kwargs) -> FlowApplyResult:
        values = (
            kwargs["a_target"],
            kwargs["b_target"],
            kwargs["c_target"],
        )
        self.calls.append(values)
        return FlowApplyResult(True, "ok", *values, values[0] + values[2])

    def apply_zero(self) -> FlowApplyResult:
        self.calls.append((0, 0, 0))
        return FlowApplyResult(True, "ok", 0, 0, 0, 0)


def test_exclusive_lease_rejects_every_conflicting_kind_and_stale_release() -> None:
    lease = ExclusiveDeviceLease()
    maintenance = lease.acquire(
        DeviceLeaseKind.MAINTENANCE,
        operation_id="clean-1",
        generation=2,
    )
    assert maintenance is not None

    for kind in (
        DeviceLeaseKind.PROTOCOL,
        DeviceLeaseKind.MANUAL,
        DeviceLeaseKind.PRETEST,
        DeviceLeaseKind.COMPENSATION,
        DeviceLeaseKind.CONFIG_CHANGE,
        DeviceLeaseKind.MAINTENANCE,
    ):
        assert lease.acquire(kind, operation_id=f"other-{kind}", generation=3) is None

    assert lease.release(maintenance.replaced(token="stale")) is False
    assert lease.release(maintenance) is True
    assert lease.release(maintenance) is False
    assert lease.snapshot.kind == DeviceLeaseKind.IDLE


def test_flow_worker_maintenance_lease_allows_only_exact_token_commands() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    token = worker.acquire_maintenance_lease("clean-1", 7)
    assert token is not None

    ordinary = FlowCommand("manual", 0, 1, "rest", 1, 2, 3, "manual")
    wrong_generation = FlowCommand(
        "wrong",
        0,
        2,
        "cleaning",
        1500,
        0,
        0,
        "cleaning",
        operation_id="clean-1",
        generation=6,
        lease_token=token.token,
    )
    cleaning = FlowCommand(
        "cleaning",
        0,
        3,
        "cleaning",
        1500,
        0,
        0,
        "cleaning",
        operation_id="clean-1",
        generation=7,
        lease_token=token.token,
    )

    assert worker.submit(ordinary) is False
    assert worker.submit(wrong_generation) is False
    assert worker.submit(cleaning) is True
    assert worker.process_ready() == 1
    assert service.calls == [(1500, 0, 0)]
    assert worker.release_lease(token) is False
    assert worker.release_maintenance_lease(
        token,
        MaintenanceLeaseReleaseEvidence(
            operation_terminal=True,
            all_targets_closed=True,
            owner_handoff=True,
        ),
    ) is True


def test_maintenance_release_fails_closed_until_every_gate_is_proven() -> None:
    worker = FlowWorker(_FlowService())
    token = worker.acquire_maintenance_lease("clean-2", 8)
    assert token is not None

    for evidence in (
        MaintenanceLeaseReleaseEvidence(False, True, True),
        MaintenanceLeaseReleaseEvidence(True, False, True),
        MaintenanceLeaseReleaseEvidence(True, True, False),
    ):
        assert worker.release_maintenance_lease(token, evidence) is False
        assert worker.lease_snapshot == token


def test_protocol_compatibility_wrapper_uses_explicit_token_and_rejects_old_epoch() -> None:
    worker = FlowWorker(_FlowService())

    assert worker.acquire_protocol_lease(8) is True
    held = worker.lease_snapshot
    assert held.kind == DeviceLeaseKind.PROTOCOL
    assert held.generation == 8
    assert worker.release_protocol_lease(7) is False
    assert worker.release_protocol_lease(8) is True
    assert worker.release_protocol_lease(8) is False


def test_maintenance_interlock_is_bidirectional_and_safety_close_remains_available() -> None:
    snapshot = InterlockSnapshot(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        device_lease="maintenance",
        recording_ready=True,
    )

    cleaning = _command(ActuationCategory.CLEANING, ActuationAction.OPEN)
    manual = _command(ActuationCategory.MANUAL, ActuationAction.OPEN)
    safety = _command(ActuationCategory.SAFETY, ActuationAction.CLOSE)

    assert snapshot.command_rejection_reason(cleaning) == ""
    assert "maintenance" in snapshot.command_rejection_reason(manual)
    assert snapshot.command_rejection_reason(safety) == ""


def _command(
    category: ActuationCategory,
    action: ActuationAction,
) -> ActuationCommand:
    cleaning = category == ActuationCategory.CLEANING
    return ActuationCommand(
        command_id=f"{category}-{action}",
        execution_epoch=0,
        arm_epoch=0,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=2,
        action=action,
        category=category,
        expected_ns=1,
        duration_ns=None,
        wall_timestamp=1,
        safety_generation=0,
        target_device="Dev1",
        target_line="P0.1",
        operation_id="clean-1" if cleaning else None,
        generation=1 if cleaning else None,
        step_id="step-1" if cleaning else None,
        action_kind=action if cleaning else None,
    )
