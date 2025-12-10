import pytest
from unittest.mock import MagicMock, patch
from app.controllers.main_controller import MainController
from app.models import AppState
from app.services import CalibrationSession, CalibrationResult

@pytest.fixture
def controller():
    state = AppState()
    worker = MagicMock()
    ctrl = MainController(state, worker)
    # Mock view
    ctrl.view = MagicMock()
    ctrl.view.calibration_view = MagicMock()
    return ctrl

def test_calibration_completion_logic(controller):
    # Setup active session with results ready
    controller.calibration_session.is_active = True
    controller.calibration_session.current_max = 3.0
    controller.calibration_session.current_min = 1.0
    
    # Mock is_finished to return True
    with patch.object(CalibrationSession, 'is_finished', return_value=True):
        with patch.object(controller, '_persist_config_values') as mock_persist:
            # Act: triggers stop() and application logic
            controller.handle_breath_samples([2.0], timestamp=100.0)
            
            # Assert Session Stopped
            assert not controller.calibration_session.is_active
            
            # Assert State Updated
            # Offset = -(3+1)/2 = -2.0
            # Gain = 10 / (3-1) = 5.0
            assert controller.state.signal_offset == -2.0
            assert controller.state.signal_gain == 5.0
            
            # Assert Persistence
            mock_persist.assert_called_once()
            args = mock_persist.call_args[0][0]
            assert args['signal_offset'] == -2.0
            assert args['signal_gain'] == 5.0
            
            # Assert View Updated
            controller.view.calibration_view.set_signal_transform.assert_called_with(-2.0, 5.0)
            controller.view.calibration_view.set_calibration_state.assert_called_with(False, "校准完成")