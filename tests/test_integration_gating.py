from unittest.mock import MagicMock, patch

import pytest

from app.controllers.main_controller import MainController
from app.models import AppState
from app.services.gating_service import GatingState
from app.workers import HardwareWorker


@pytest.fixture
def mock_worker():
    worker = MagicMock(spec=HardwareWorker)
    # Mock signals if needed, or just rely on manual calls in controller
    return worker

@pytest.fixture
def controller(mock_worker):
    state = AppState()
    # Ensure default thresholds
    state.inhale_threshold = 0.5
    state.exhale_threshold = -0.5

    ctrl = MainController(state=state, worker=mock_worker)
    # Mock internal logger to verify calls
    ctrl._breath_logger = MagicMock()
    return ctrl

def test_controller_wires_gating_service(controller):
    """Verify handle_breath_samples triggers gating logic and logging."""
    # Setup: 100Hz samples crossing threshold
    # 0.1 -> 0.6 (trigger inhale) -> 0.7 -> 0.1 (trigger neutral)
    samples = [0.1, 0.6, 0.7, 0.1]
    timestamp = 100.04 # End timestamp

    # Act
    controller.handle_breath_samples(samples, timestamp)

    # Assert: Service state updated
    assert controller.gating_service.current_state == GatingState.NEUTRAL
    assert controller.state.telemetry.gating_state == GatingState.NEUTRAL

    # Assert: Logger called for transitions
    # Transition 1: Neutral -> Inhale (at 0.6)
    # Transition 2: Inhale -> Neutral (at 0.1)
    assert controller._breath_logger.info.call_count == 2

    call_args_list = controller._breath_logger.info.call_args_list

    # Verify first transition log
    payload1 = call_args_list[0][0][0]
    assert payload1["event"] == "threshold_cross"
    assert payload1["gate_state"] == GatingState.INHALE
    assert payload1["sample_value"] == 0.6

    # Verify second transition log
    payload2 = call_args_list[1][0][0]
    assert payload2["event"] == "threshold_cross"
    assert payload2["gate_state"] == GatingState.NEUTRAL
    assert payload2["sample_value"] == 0.1

def test_controller_safety_blocks_gating(controller):
    """Verify safety state prevents valid gating output."""
    # Setup: Safety state is LOW_FLOW
    controller.state.telemetry.safety_state = "LOW_FLOW"

    # High flow sample that would normally trigger INHALE
    samples = [0.8]
    timestamp = 100.01

    # Act
    controller.handle_breath_samples(samples, timestamp)

    # Assert
    assert controller.gating_service.current_state == GatingState.BLOCKED
    assert controller.state.telemetry.gating_state == GatingState.BLOCKED

    # Verify log contains BLOCKED state
    call_args = controller._breath_logger.info.call_args
    log_payload = call_args[0][0]
    assert log_payload["event"] == "threshold_cross"
    assert log_payload["gate_state"] == GatingState.BLOCKED
    assert log_payload["safety_state"] == "LOW_FLOW"

def test_controller_persists_thresholds(controller):
    """Verify updating threshold via controller persists to config."""
    with patch.object(controller, '_persist_config_values') as mock_persist:
        # Act
        controller.update_breath_threshold("inhale", 1.5)

        # Assert memory update
        assert controller.state.inhale_threshold == 1.5
        assert controller.gating_service.inhale_threshold == 1.5

        # Assert persistence call
        mock_persist.assert_called_once()
        args = mock_persist.call_args[0][0]
        assert args["inhale_threshold"] == 1.5


def test_controller_feeds_calibrated_samples_into_protocol_executor(controller):
    controller.state.signal_offset = 1.0
    controller.state.signal_gain = 2.0
    controller.protocol_executor.process_breath_samples = MagicMock(
        return_value=controller.protocol_executor.empty_result()
    )

    controller.handle_breath_samples([-1.4], 50.0)

    args = controller.protocol_executor.process_breath_samples.call_args.kwargs
    assert args["samples"] == [-0.7999999999999998]
    assert args["safety_state"] == "SAFE"


def test_controller_protocol_executor_trigger_uses_valve_service(controller):
    from pathlib import Path

    from app.models import ProtocolDocument, ProtocolTrial, TriggerMode

    controller.state.loaded_protocol = ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=TriggerMode.MANUAL,
            )
        ],
    )
    controller.state.flow_setpoints_ready = True
    controller.state.hardware_ready = True
    controller.state.telemetry.connected = True
    controller.state.telemetry.safety_state = "SAFE"
    controller.valve_service.set_valve = MagicMock(return_value=(True, "ok"))

    controller.handle_protocol_start_requested()
    controller.handle_breath_samples([-0.6], 10.0)

    controller.valve_service.set_valve.assert_called_with(
        1,
        True,
        safety_state=controller._build_current_safety_state(),
        safety_close=False,
    )


def test_controller_protocol_close_uses_safety_close_path(controller):
    from pathlib import Path

    from app.models import ProtocolDocument, ProtocolTrial, TriggerMode

    controller.state.loaded_protocol = ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=TriggerMode.MANUAL,
            )
        ],
    )
    controller.state.flow_setpoints_ready = True
    controller.state.hardware_ready = True
    controller.state.telemetry.connected = True
    controller.state.telemetry.safety_state = "SAFE"
    controller.valve_service.set_valve = MagicMock(return_value=(True, "ok"))

    controller.handle_protocol_start_requested()
    controller.handle_breath_samples([-0.6], 10.0)
    controller.state.telemetry.safety_state = "LOW_FLOW"
    controller.protocol_executor.handle_safety_update("LOW_FLOW", timestamp=10.2)

    controller.valve_service.set_valve.assert_any_call(
        1,
        False,
        safety_state=controller._build_current_safety_state(),
        safety_close=True,
    )
