from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.controllers.main_controller import MainController
from app.models import AppState, ProtocolDocument, ProtocolTrial, TriggerMode
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
    state.loaded_protocol = _document(mode)
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    worker = HardwareWorker(hal=MockHAL(), simulation=True)
    controller = MainController(state, worker)
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


def test_ttl_read_error_blocks_running_executor_and_invalidates_epoch() -> None:
    controller = _controller(TriggerMode.TTL)
    controller.handle_protocol_start_requested()
    old_epoch = controller.protocol_executor.state.arm_epoch

    controller.handle_ttl_input_error("TTL/共享 AI 读取失败：USB disconnected")

    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller.protocol_executor.state.arm_epoch > old_epoch
    assert "读取失败" in controller.protocol_executor.state.recent_event.message


def test_protocol_replacement_close_failure_keeps_old_document_and_active_valve() -> None:
    controller = _controller()
    old_document = controller.state.loaded_protocol

    def writer(channel: int, open_state: bool, **kwargs) -> tuple[bool, str]:
        return (True, "ok") if open_state else (False, "关闭失败")

    controller.valve_service.set_valve = MagicMock(side_effect=writer)
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
