from __future__ import annotations

import threading

import pytest

from app.models import (
    DeviceLeaseKind,
    MaintenanceLeaseReleaseEvidence,
    ProtocolExecutionState,
    SafeStopIdentity,
)
from app.services.flow_service import FlowApplyResult
from app.workers.actuation_worker import (
    ActuationInterlockIngress,
    ActuationWorker,
    InterlockSnapshot,
)
from app.workers.flow_worker import FlowCommand, FlowCommandResult, FlowWorker


class _FlowService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_flows(self, **kwargs) -> FlowApplyResult:
        self.calls.append(str(kwargs["mode"]))
        return FlowApplyResult(True, "ok", kwargs["a_target"], kwargs["b_target"], kwargs["c_target"], kwargs["a_target"])

    def apply_zero(self) -> FlowApplyResult:
        self.calls.append("zero")
        return FlowApplyResult(True, "ok", 0, 0, 0, 0)

    def apply_a_zero(self) -> FlowApplyResult:
        self.calls.append("safe_stop_a_zero")
        return FlowApplyResult(True, "ok", 0, 0, 0, 0)


def test_flow_worker_preserves_command_identity() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    received = []
    worker.result_ready.connect(received.append)
    command = FlowCommand("flow-1", 7, 3, "rest", 1.0, 2.0, 3.0, "manual")

    assert worker.submit(command)
    assert worker.process_ready() == 1

    assert service.calls == ["rest"]
    assert received == [FlowCommandResult(command=command, result=received[0].result)]
    assert received[0].result.success


def test_protocol_lease_rejects_while_queued_flow_still_owns_order() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    received = []
    worker.result_ready.connect(received.append)
    command = FlowCommand("flow-old", 7, 3, "rest", 1.0, 2.0, 3.0, "manual")

    assert worker.submit(command)
    assert worker.acquire_protocol_lease(8) is False

    assert service.calls == []
    assert worker.process_ready() == 1
    assert len(received) == 1
    assert received[0].command is command
    assert received[0].result.success is True
    assert worker.acquire_protocol_lease(8) is True


def test_flow_worker_rejects_stale_epoch_after_protocol_lease_release() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    worker.acquire_protocol_lease(8)
    worker.release_protocol_lease(8)

    stale = FlowCommand("stale", 7, 1, "rest", 1.0, 2.0, 3.0, "manual")
    current = FlowCommand("current", 8, 2, "rest", 4.0, 5.0, 6.0, "manual")

    assert worker.submit(stale) is False
    assert worker.submit(current) is True
    assert worker.process_ready() == 1
    assert service.calls == ["rest"]


def test_stale_lease_calls_cannot_rollback_or_release_new_protocol_epoch() -> None:
    worker = FlowWorker(_FlowService())

    assert worker.acquire_protocol_lease(9) is True
    assert worker.acquire_protocol_lease(8) is False
    assert worker.release_protocol_lease(8) is False
    assert worker.release_protocol_lease(10) is False
    assert worker.execution_context[:2] == (9, "protocol")
    assert worker.release_protocol_lease(9) is True
    assert worker.execution_context[:2] == (9, "idle")


def test_protocol_lease_rejects_even_current_epoch_safety_flow_recovery() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    received = []
    worker.result_ready.connect(received.append)
    assert worker.acquire_protocol_lease(8)
    recovery = FlowCommand(
        "safety-recovery",
        8,
        2,
        "rest",
        4.0,
        0.0,
        0.0,
        "safety:low-flow-recovery",
    )

    assert worker.submit(recovery) is False
    assert worker.process_ready() == 0

    assert service.calls == []
    assert received == []


def test_shutdown_zero_preempts_business_lease_but_stays_on_flow_owner() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    assert worker.acquire_protocol_lease(8)

    assert worker.zero_for_shutdown(1000) is True

    assert service.calls == ["safe_stop_a_zero", "zero"]
    assert worker.lease_snapshot.kind.value == "protocol"


def test_safe_stop_a_zero_receipt_is_correlated_and_fences_business_queue() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    cancelled = []
    worker.result_ready.connect(cancelled.append)
    assert worker.submit(FlowCommand("old", 7, 1, "rest", 1, 2, 3, "manual"))
    identity = SafeStopIdentity("safe-1", 2, 8)

    receipt = worker.zero_a_for_safe_stop(identity, 1000)

    assert receipt is not None
    assert receipt.identity == identity
    assert receipt.success and receipt.confirmed_a == 0
    assert service.calls == ["safe_stop_a_zero"]
    assert cancelled[0].command.command_id == "old"
    assert cancelled[0].result.success is False
    assert worker.submit(
        FlowCommand("new", 8, 2, "rest", 1, 2, 3, "manual")
    ) is False


def test_blocking_safe_stop_a_zero_returning_after_deadline_is_rejected(
    monkeypatch,
) -> None:
    now = [10.0]
    monkeypatch.setattr(
        "app.workers.flow_worker.time.monotonic",
        lambda: now[0],
    )

    class _SlowFlowService(_FlowService):
        def apply_a_zero(self) -> FlowApplyResult:
            now[0] += 0.01
            return super().apply_a_zero()

    worker = FlowWorker(_SlowFlowService())

    assert worker.zero_a_for_safe_stop(
        SafeStopIdentity("safe-slow", 1, 1),
        timeout_ms=1,
    ) is None


def test_safe_stop_rejects_conflicting_identity() -> None:
    worker = FlowWorker(_FlowService())
    first = SafeStopIdentity("safe-1", 2, 8)
    conflict = SafeStopIdentity("safe-2", 2, 8)

    assert worker.zero_a_for_safe_stop(first, 1000) is not None
    assert worker.zero_a_for_safe_stop(conflict, 1000) is None


def test_newer_safe_stop_epoch_supersedes_old_identity_but_never_rolls_back() -> None:
    worker = FlowWorker(_FlowService())
    first = SafeStopIdentity("safe-1", 2, 8)
    newer = SafeStopIdentity("safe-2", 3, 9)
    older = SafeStopIdentity("safe-old", 4, 7)

    assert worker.zero_a_for_safe_stop(first, 1000) is not None
    assert worker.zero_a_for_safe_stop(newer, 1000) is not None
    assert worker.zero_a_for_safe_stop(older, 1000) is None


def test_shutdown_cancels_pending_flow_and_restart_does_not_replay_it() -> None:
    service = _FlowService()
    worker = FlowWorker(service)
    received = []
    worker.result_ready.connect(received.append)
    command = FlowCommand("flow-pending", 1, 1, "rest", 1.0, 2.0, 3.0, "manual")

    assert worker.submit(command)
    assert worker.shutdown()
    assert received[0].command is command
    assert received[0].result.error == "cancelled"

    assert worker.prepare_restart() is False
    assert worker.prepare_restart(execution_epoch=2)
    assert worker.process_ready() == 0
    assert service.calls == []

    assert worker.submit(command) is False
    replacement = FlowCommand("flow-current", 2, 2, "rest", 4.0, 5.0, 6.0, "manual")
    assert worker.submit(replacement) is True
    assert worker.process_ready() == 1
    assert service.calls == ["rest"]


def test_restart_epoch_rebind_is_monotonic_and_rejects_delayed_commands() -> None:
    worker = FlowWorker(_FlowService())
    original = FlowCommand("original", 4, 1, "zero", 0, 0, 0, "startup")

    assert worker.submit(original)
    assert worker.process_ready() == 1
    assert worker.shutdown()

    assert worker.prepare_restart(execution_epoch=3) is False
    assert worker.prepare_restart(execution_epoch=5) is True
    assert worker.submit(original) is False
    assert worker.submit(FlowCommand("current", 5, 2, "zero", 0, 0, 0, "startup"))


def test_idle_flow_owner_can_only_advance_to_newer_actuation_epoch() -> None:
    worker = FlowWorker(_FlowService())
    assert worker.prepare_restart(execution_epoch=2)

    current = FlowCommand("current", 4, 1, "zero", 0, 0, 0, "startup")
    stale = FlowCommand("stale", 3, 2, "zero", 0, 0, 0, "startup")

    assert worker.submit(current) is True
    assert worker.process_ready() == 1
    assert worker.execution_context[0] == 4
    assert worker.submit(stale) is False


def test_flow_worker_is_the_thread_that_reads_airflow() -> None:
    class Hal:
        def __init__(self) -> None:
            self.read_thread_ids: list[int] = []

        def read_flow(self) -> float:
            self.read_thread_ids.append(threading.get_ident())
            return 1.25

        def release_serial_resources(self) -> None:
            return None

    service = _FlowService()
    service.hal = Hal()
    worker = FlowWorker(service, airflow_poll_interval_s=0.02)
    received = []
    ready = threading.Event()

    def sink(value: float, timestamp: float, error: str | None) -> None:
        received.append((value, timestamp, error, threading.get_ident()))
        ready.set()

    worker.set_airflow_sink(sink)
    worker.start()
    try:
        assert ready.wait(1.0)
    finally:
        assert worker.shutdown(1000)

    assert received[0][0] == 1.25
    assert received[0][2] is None
    assert service.hal.read_thread_ids == [received[0][3]]
    assert received[0][3] != threading.get_ident()


def test_actuation_owner_rejects_flow_intent_during_protocol_lease() -> None:
    submitted = []
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=True,
            safety_state="SAFE",
            has_protocol=True,
            device_lease="protocol",
        )
    )
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(),
        writer=lambda command: None,
        interlock=ingress,
        flow_submitter=submitted.append,
    )
    results = []
    worker.flow_result_ready.connect(results.append)

    worker.post_flow_intent(mode="rest", a=1, b=2, c=3, source="manual")
    worker.process_ready()

    assert submitted == []
    assert results[0].result.success is False
    assert "租约" in results[0].result.message


def test_actuation_owner_authorizes_idle_flow_and_consumes_result() -> None:
    submitted = []
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=False,
            safety_state="SAFE",
            device_lease="idle",
        )
    )
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(execution_epoch=4),
        writer=lambda command: None,
        interlock=ingress,
        flow_submitter=submitted.append,
    )
    results = []
    worker.flow_result_ready.connect(results.append)

    worker.post_flow_intent(mode="stim_start", a=1, b=2, c=0, source="pretest")
    worker.process_ready()
    command = submitted[0]
    assert command.execution_epoch == 4

    result = FlowCommandResult(
        command=command,
        result=FlowApplyResult(True, "ok", 1, 2, 0, 1),
    )
    worker.post_flow_result(result)
    worker.process_ready()

    assert results == [result]
    assert ingress.read()[1].flow_setpoints_ready is True


def test_non_threaded_shutdown_releases_serial_before_reporting_handoff() -> None:
    class HAL:
        def __init__(self) -> None:
            self.serial_resources_in_use = True
            self.release_calls = 0

        def release_serial_resources(self) -> None:
            self.release_calls += 1
            self.serial_resources_in_use = False

    service = _FlowService()
    service.hal = HAL()
    worker = FlowWorker(service)

    assert worker.shutdown()
    assert service.hal.release_calls == 1
    assert service.hal.serial_resources_in_use is False


@pytest.mark.parametrize(
    "lease_kind",
    [DeviceLeaseKind.PROTOCOL, DeviceLeaseKind.MAINTENANCE],
)
def test_correlated_safe_stop_releases_active_device_lease(lease_kind) -> None:
    worker = FlowWorker(_FlowService())
    if lease_kind == DeviceLeaseKind.PROTOCOL:
        assert worker.acquire_protocol_lease(1)
    else:
        assert worker.acquire_maintenance_lease("maintenance-1", 1) is not None
    identity = SafeStopIdentity("global-stop", 2, execution_epoch=2)

    a_receipt = worker.zero_a_for_safe_stop(identity, 100)
    assert a_receipt is not None and a_receipt.success
    assert worker.zero_all_for_safe_stop(identity, 100)
    evidence = (
        MaintenanceLeaseReleaseEvidence(True, True, True)
        if lease_kind == DeviceLeaseKind.MAINTENANCE
        else None
    )
    assert worker.release_lease_for_safe_stop(identity, evidence)
    assert worker._lease.snapshot.kind == DeviceLeaseKind.IDLE
