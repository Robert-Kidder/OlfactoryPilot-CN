from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest

from app.controllers.main_controller import MainController
from app.models import AppState
from app.services import CalibrationSession


@pytest.fixture
def controller():
    state = AppState()
    worker = MagicMock()
    worker.isRunning.return_value = False
    ctrl = MainController(state, worker, allow_test_actuation_bridge=True)
    # Mock view and calibration view interfaces used in controller
    ctrl.view = MagicMock()
    ctrl.view.calibration_view = MagicMock()
    return ctrl


def test_calibration_completion_logic(controller):
    controller.calibration_session.is_active = True
    controller.calibration_session.current_max = 3.0
    controller.calibration_session.current_min = 1.0

    with patch.object(CalibrationSession, "is_finished", return_value=True), patch.object(
        controller, "_persist_config_values"
    ) as mock_persist:
        controller.handle_breath_samples([2.0], timestamp=100.0)

    assert not controller.calibration_session.is_active
    assert controller.state.signal_offset == -2.0  # -(3+1)/2
    assert controller.state.signal_gain == 5.0     # 10 / (3-1)

    mock_persist.assert_called_once()
    args = mock_persist.call_args[0][0]
    assert args["signal_offset"] == -2.0
    assert args["signal_gain"] == 5.0
    controller.view.calibration_view.set_signal_transform.assert_called_with(-2.0, 5.0)
    controller.view.calibration_view.set_calibration_state.assert_called_with(False, ANY)
    controller.view.calibration_view.update_calibration_stats.assert_called_with(3.0, 1.0, -2.0, 5.0)


def test_calibration_affects_gating(controller):
    # Apply calibration so small raw value still exceeds threshold after gain
    controller.state.signal_offset = 0.0
    controller.state.signal_gain = 10.0
    controller.gating_service.set_thresholds(inhale=5.0, exhale=-5.0)

    controller.handle_breath_samples([0.6], timestamp=100.0)

    assert controller.state.telemetry.gating_state == "INHALE_ABOVE"
