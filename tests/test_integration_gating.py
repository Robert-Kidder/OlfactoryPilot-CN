import pytest
from unittest.mock import MagicMock, patch
from app.controllers.main_controller import MainController
from app.models import AppState, Telemetry
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
    msg1, args1 = call_args_list[0]
    assert "threshold_cross" in msg1
    log_payload1 = args1[0]
    assert log_payload1["gate_state"] == GatingState.INHALE
    assert log_payload1["sample_value"] == 0.6
    
    # Verify second transition log
    msg2, args2 = call_args_list[1]
    assert "threshold_cross" in msg2
    log_payload2 = args2[0]
    assert log_payload2["gate_state"] == GatingState.NEUTRAL
    assert log_payload2["sample_value"] == 0.1

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
