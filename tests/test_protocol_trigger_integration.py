from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.controllers.main_controller import MainController
from app.models import (
    ActuationAction,
    ActuationReceipt,
    ActuationResult,
    AppState,
    ProtocolDocument,
    ProtocolTrial,
    TriggerMode,
)
from app.models.protocol_execution import ProtocolExecutionStatus
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlPulse
from app.workers import HardwareWorker


def _document(mode: TriggerMode = TriggerMode.MANUAL) -> ProtocolDocument:
    return ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=mode,
            )
        ],
    )


def _controller(mode: TriggerMode = TriggerMode.MANUAL) -> MainController:
    state = AppState(simulation_mode=True)
    state.valve_variants = {"20-channel": {1: "Dev1/P0.0", 2: "Dev1/P0.1"}}
    state.loaded_protocol = _document(mode)
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    worker = HardwareWorker(hal=MockHAL(), simulation=True)
    controller = MainController(state, worker, allow_test_actuation_bridge=True)
    controller.protocol_executor.reset(state.loaded_protocol)
    return controller


def test_controller_connects_ttl_worker_signals_once() -> None:
    state = AppState()
    worker = MagicMock(spec=HardwareWorker)

    MainController(state, worker)

    worker.ttl_pulse.connect.assert_called_once()
    worker.ttl_input_error.connect.assert_called_once()


def test_manual_handler_uses_common_readiness_even_when_ai6_not_ready() -> None:
    controller = _controller()
    controller.worker.hal = MagicMock(ttl_input_ready=False)

    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert controller.protocol_executor.state.current_mode == TriggerMode.MANUAL


def test_protocol_start_is_blocked_while_manual_valve_is_open() -> None:
    controller = _controller()
    controller.valve_service._states[1] = True
    previous_epoch = controller.protocol_executor.state.execution_epoch

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.execution_epoch == previous_epoch
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert "阀" in controller.state.status_message


def test_ttl_handler_forwards_immutable_payload_without_relabeling_epoch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    old_epoch = controller.protocol_executor.state.arm_epoch
    pulse = TtlPulse(timestamp=12.25, arm_epoch=old_epoch, sequence=9)
    controller.protocol_executor.accept_trigger = MagicMock(
        return_value=controller.protocol_executor.empty_result()
    )

    controller.handle_ttl_pulse(pulse)

    kwargs = controller.protocol_executor.accept_trigger.call_args.kwargs
    assert kwargs["timestamp"] == 12.25
    assert kwargs["captured_epoch"] == old_epoch
    assert kwargs["sequence"] == 9


def test_queued_ttl_pulse_is_rejected_after_mode_switch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
    )

    controller.handle_protocol_trigger_mode_requested("manual")
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.current_mode == TriggerMode.MANUAL
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert controller.protocol_executor.state.recent_event.result == "ignored"


def test_queued_ttl_pulse_is_rejected_after_stop() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
    )
    old_epoch = queued.arm_epoch

    controller.handle_protocol_stop_requested()
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.STOPPED
    assert controller.protocol_executor.state.trial_index == 0
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert controller.protocol_executor.state.recent_event.result == "ignored"


def test_queued_ttl_pulse_cannot_advance_after_disconnect() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    queued = TtlPulse(
        timestamp=10.0,
        arm_epoch=controller.protocol_executor.state.arm_epoch,
        sequence=1,
    )
    old_epoch = queued.arm_epoch

    controller.state.telemetry.connected = False
    controller.actuation_interlock.update(connected=False, safety_state="DATA_STALE")
    controller.actuation_worker.post_readiness_update(
        readiness=controller._execution_readiness(), timestamp=10.0
    )
    controller._drain_actuation_if_not_running()
    controller.handle_ttl_pulse(queued)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.trial_index == 0
    assert controller.protocol_executor.state.waiting_started_at is None
    assert controller.protocol_executor.state.active_valve is None
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert controller.protocol_executor.state.recent_event.result == "rejected"


def test_ttl_read_error_blocks_running_executor_and_invalidates_epoch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    old_epoch = controller.protocol_executor.state.arm_epoch

    controller.handle_ttl_input_error("TTL/共享 AI 读取失败：USB disconnected")

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert "读取失败" in controller.protocol_executor.state.recent_event.message


def test_runtime_read_error_keeps_ttl_rearm_rejected_until_a_frame_recovers() -> None:
    class FailingHAL(MockHAL):
        def read_ai_frame(self, timestamp: float | None = None):
            raise RuntimeError("USB disconnected")

    controller = _controller(TriggerMode.TTL)
    controller.worker.hal = FailingHAL()
    controller.handle_protocol_start_requested()

    controller.worker._emit_ai_frame(10.0)
    controller.handle_protocol_rearm_requested()

    assert controller.worker.ttl_input_ready is False
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.recent_event.event == "rearm_rejected"
    assert "AI0" in controller.protocol_executor.state.recent_event.message


def test_breath_sample_blocks_on_fresh_readiness_loss_before_opening_valve() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.state.telemetry.connected = False

    controller.handle_breath_samples([-0.6], 10.0)

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.active_valve is None
    assert controller.worker.hal.get_line_state("Dev1/P0.0") is None
    assert "连接" in controller.protocol_executor.state.recent_event.message


def test_protocol_replacement_close_failure_keeps_old_document_and_active_valve() -> None:
    controller = _controller()
    old_document = controller.state.loaded_protocol

    def writer(command):
        result = (
            ActuationResult.FAILED
            if command.action == ActuationAction.CLOSE
            else ActuationResult.SUCCESS
        )
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns if result == ActuationResult.SUCCESS else None,
            wall_timestamp=10.0,
            result=result,
            message="关闭失败" if result == ActuationResult.FAILED else "ok",
        )

    controller.actuation_worker.writer = writer
    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.handle_breath_samples([-0.6], 10.0)
    candidate = ProtocolDocument(
        source_path=Path("candidate.csv"),
        source_name="candidate.csv",
        trials=[
            ProtocolTrial(
                trial_id="new",
                timing_ms=0,
                duration_ms=100,
                valve=2,
                trigger=TriggerMode.TTL,
            )
        ],
    )

    with patch(
        "app.controllers.main_controller.parse_protocol_file",
        return_value=candidate,
    ):
        loaded = controller.handle_protocol_file_selected("candidate.csv")

    assert loaded is False
    assert controller.state.loaded_protocol is old_document
    assert controller.protocol_executor.state.document is old_document
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.active_valve == 1


def test_telemetry_cannot_release_pending_protocol_start_lease() -> None:
    controller = _controller()
    controller.actuation_worker.post_start = MagicMock()
    controller.handle_protocol_start_requested()

    assert controller._protocol_start_pending is True
    assert controller.flow_worker.execution_context[1] == "protocol"

    controller.handle_telemetry(
        {
            "timestamp": 10.0,
            "connected": True,
            "airflow": 1.0,
            "safety_state": "SAFE",
        }
    )

    assert controller._protocol_start_pending is True
    assert controller.actuation_interlock.read()[1].device_lease == "protocol"
    assert controller.flow_worker.execution_context[1] == "protocol"


def test_rejected_start_releases_pending_flow_lease() -> None:
    controller = _controller()
    controller.state.telemetry.connected = False
    controller.actuation_interlock.update(connected=False, safety_state="DATA_STALE")

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert controller._protocol_start_pending is False
    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[1] == "idle"


def test_rejected_start_from_blocked_state_does_not_retain_flow_lease() -> None:
    controller = _controller()
    controller.protocol_executor.state.status = ProtocolExecutionStatus.BLOCKED

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller._protocol_start_pending is False
    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[1] == "idle"


def test_start_epoch_resync_failure_releases_flow_lease_and_stops() -> None:
    controller = _controller()
    original_acquire = controller.flow_worker.acquire_protocol_lease
    calls = 0

    def fail_second_acquire(epoch: int) -> bool:
        nonlocal calls
        calls += 1
        return original_acquire(epoch) if calls == 1 else False

    controller.flow_worker.acquire_protocol_lease = fail_second_acquire

    controller.handle_protocol_start_requested()
    controller._drain_actuation_if_not_running()

    assert calls == 2
    assert controller._protocol_lease_epoch is None
    assert controller._protocol_start_pending is False
    assert controller.flow_worker.execution_context[1] == "idle"
    assert controller.actuation_interlock.read()[1].connected is False
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.STOPPED


def test_production_controller_rejects_start_when_owner_workers_are_stopped() -> None:
    state = AppState(simulation_mode=True)
    state.loaded_protocol = _document()
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    controller = MainController(state, HardwareWorker(hal=MockHAL(), simulation=True))
    controller.protocol_executor.reset(state.loaded_protocol)

    controller.handle_protocol_start_requested()

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.READY
    assert controller.flow_worker.execution_context[1] == "idle"
    assert "worker" in controller.state.status_message


def test_active_epoch_drift_keeps_exact_flow_token_until_terminal_release() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    held_epoch = controller._protocol_lease_epoch
    assert held_epoch is not None

    active = replace(
        controller._protocol_snapshot,
        status=ProtocolExecutionStatus.BLOCKED,
        execution_epoch=held_epoch + 1,
    )
    controller._handle_protocol_snapshot(active)

    assert controller._protocol_lease_epoch == held_epoch
    assert controller.flow_worker.execution_context[:2] == (held_epoch, "protocol")

    terminal = replace(
        active,
        status=ProtocolExecutionStatus.STOPPED,
        execution_epoch=held_epoch + 2,
    )
    controller._handle_protocol_snapshot(terminal)

    assert controller._protocol_lease_epoch is None
    assert controller.flow_worker.execution_context[:2] == (
        held_epoch + 2,
        "idle",
    )


def test_failed_terminal_flow_release_keeps_fail_closed_lease_token() -> None:
    controller = _controller()
    controller.handle_protocol_start_requested()
    held_epoch = controller._protocol_lease_epoch
    assert held_epoch is not None
    controller.flow_worker.release_protocol_lease = MagicMock(return_value=False)
    terminal = replace(
        controller._protocol_snapshot,
        status=ProtocolExecutionStatus.STOPPED,
        execution_epoch=held_epoch + 1,
    )

    controller._handle_protocol_snapshot(terminal)

    assert controller._protocol_lease_epoch == held_epoch
    interlock = controller.actuation_interlock.read()[1]
    assert interlock.device_lease == "protocol"
    assert interlock.connected is False
    assert "租约释放失败" in controller.state.status_message
