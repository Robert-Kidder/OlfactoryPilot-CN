from pathlib import Path

from app.models import (
    ActuationAction,
    ActuationReceipt,
    ActuationResult,
    ProtocolDocument,
    ProtocolExecutionReadiness,
    ProtocolExecutionStatus,
    ProtocolTrial,
    TriggerMode,
)
from app.services.gating_service import GatingService
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.protocol_executor import ProtocolExecutor


def _readiness() -> ProtocolExecutionReadiness:
    return ProtocolExecutionReadiness(True, True, True, "SAFE", True)


def _executor() -> ProtocolExecutor:
    document = ProtocolDocument(
        source_path=Path("async.csv"),
        source_name="async.csv",
        trials=[
            ProtocolTrial("one", 0, 100, 3, TriggerMode.MANUAL),
            ProtocolTrial("two", 0, 100, 4, TriggerMode.MANUAL),
        ],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, opened: (_ for _ in ()).throw(
            AssertionError("deferred executor must not write DO")
        ),
        deferred_actuation=True,
        clock=lambda: 10.0,
    )
    executor.start(document, readiness=_readiness(), timestamp=10.0)
    executor.accept_trigger(TriggerMode.MANUAL, readiness=_readiness(), timestamp=10.01)
    return executor


def _batch() -> BreathSampleBatch:
    return BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(10.02, 0.0, monotonic_ns=1_000_000_000, ai_epoch=1, sample_sequence=1),
            AnalogInputFrame(10.03, -0.6, monotonic_ns=1_010_000_000, ai_epoch=1, sample_sequence=2),
        )
    )


def test_deferred_open_is_pending_atomically_and_duplicate_exhale_yields_no_second_request() -> None:
    executor = _executor()

    first = executor.process_breath_samples(
        _batch(),
        safety_state="SAFE",
        readiness=_readiness(),
        safety_generation=7,
    )
    duplicate = executor.process_breath_samples(
        _batch(),
        safety_state="SAFE",
        readiness=_readiness(),
        safety_generation=7,
    )

    assert len(first.action_requests) == 1
    command = first.action_requests[0]
    assert command.action == ActuationAction.OPEN
    assert command.expected_ns == 1_010_000_000
    assert command.safety_generation == 7
    assert executor.state.pending_open_command_id == command.command_id
    assert executor.state.active_valve is None
    assert duplicate.action_requests == ()


def test_only_matching_open_and_close_ack_advance_deferred_state() -> None:
    executor = _executor()
    command = executor.process_breath_samples(
        _batch(),
        safety_state="SAFE",
        readiness=_readiness(),
        safety_generation=7,
    ).action_requests[0]
    open_receipt = ActuationReceipt.from_write(
        command=command,
        started_ns=command.expected_ns,
        actual_ns=command.expected_ns + 2_000_000,
        wall_timestamp=10.04,
        result=ActuationResult.SUCCESS,
    )

    opened = executor.consume_actuation_receipt(open_receipt, readiness=_readiness())

    assert opened.state.active_valve == 3
    assert opened.state.pending_open_command_id is None
    assert opened.state.trial_index == 0

    close_command = executor.create_close_request(
        open_receipt,
        sequence=99,
        safety_generation=7,
    )
    close_receipt = ActuationReceipt.from_write(
        command=close_command,
        started_ns=close_command.expected_ns,
        actual_ns=close_command.expected_ns + 1_000_000,
        wall_timestamp=10.15,
        result=ActuationResult.SUCCESS,
        actual_duration_ms=101.0,
    )
    closed = executor.consume_actuation_receipt(close_receipt, readiness=_readiness())

    assert closed.state.active_valve is None
    assert closed.state.pending_close_command_id is None
    assert closed.state.trial_index == 1
    assert closed.state.status == ProtocolExecutionStatus.WAITING_TRIGGER


def test_stale_successful_open_does_not_set_active_or_advance() -> None:
    executor = _executor()
    command = executor.process_breath_samples(
        _batch(),
        safety_state="SAFE",
        readiness=_readiness(),
        safety_generation=7,
    ).action_requests[0]
    executor.state.execution_epoch += 1
    receipt = ActuationReceipt.from_write(
        command=command,
        started_ns=command.expected_ns,
        actual_ns=command.expected_ns,
        wall_timestamp=10.04,
        result=ActuationResult.SUCCESS,
    )

    result = executor.consume_actuation_receipt(receipt, readiness=_readiness())

    assert result.state.active_valve is None
    assert result.state.trial_index == 0
    assert result.events[-1].result == "stale"
