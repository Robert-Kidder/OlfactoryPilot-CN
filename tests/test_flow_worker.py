from __future__ import annotations

from app.models import ProtocolExecutionState
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
