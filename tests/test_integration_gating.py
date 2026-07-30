from unittest.mock import MagicMock, patch

import pytest

from app.controllers.main_controller import MainController
from app.models import AppState
from app.services.gating_service import GatingState
from app.workers import HardwareWorker


@pytest.fixture
def mock_worker():
    worker = MagicMock(spec=HardwareWorker)
    worker.isRunning.return_value = False
    # Mock signals if needed, or just rely on manual calls in controller
    return worker

@pytest.fixture
def controller(mock_worker):
    state = AppState()
    # Ensure default thresholds
    state.inhale_threshold = 0.5
    state.exhale_threshold = -0.5

    ctrl = MainController(
        state=state,
        worker=mock_worker,
        allow_test_actuation_bridge=True,
    )
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

    call = controller.protocol_executor.process_breath_samples.call_args
    batch = call.args[0]
    assert [sample.value for sample in batch.samples] == [-0.7999999999999998]
    assert call.kwargs["safety_state"] == "SAFE"


def test_controller_protocol_executor_trigger_uses_valve_service(controller):
    from pathlib import Path

    from app.models import (
        ActuationReceipt,
        ActuationResult,
        ProtocolDocument,
        ProtocolTrial,
        TriggerMode,
    )

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
    controller.actuation_interlock.update(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        has_protocol=True,
        recording_ready=True,
    )
    commands = []

    def writer(command):
        commands.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    controller.actuation_worker.writer = writer

    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.handle_breath_samples([-0.6], 10.0)

    assert len(commands) == 1
    assert commands[0].valve == 1
    assert commands[0].action.value == "open"
    assert commands[0].category.value == "normal"


def test_controller_protocol_close_uses_safety_close_path(controller):
    from pathlib import Path

    from app.models import (
        ActuationReceipt,
        ActuationResult,
        ProtocolDocument,
        ProtocolExecutionReadiness,
        ProtocolTrial,
        TriggerMode,
    )

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
    controller.actuation_interlock.update(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        has_protocol=True,
        recording_ready=True,
    )
    commands = []

    def writer(command):
        commands.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    controller.actuation_worker.writer = writer

    controller.handle_protocol_start_requested()
    controller.handle_protocol_manual_trigger_requested()
    controller.handle_breath_samples([-0.6], 10.0)
    controller.state.telemetry.safety_state = "LOW_FLOW"
    controller.actuation_interlock.update(safety_state="LOW_FLOW")
    controller.actuation_worker.post_readiness_update(
        readiness=ProtocolExecutionReadiness(True, True, True, "LOW_FLOW", True),
        timestamp=10.2,
    )
    controller._drain_actuation_if_not_running()

    assert [command.action.value for command in commands] == ["open", "close"]
    assert commands[-1].category.value == "safety"


def test_protocol_start_waits_for_confirmed_master_prepare(controller):
    from pathlib import Path

    from app.models import (
        ActuationReceipt,
        ActuationResult,
        ProtocolDocument,
        ProtocolExecutionStatus,
        ProtocolTrial,
        TriggerMode,
    )

    controller.state.master_valve_line = "Dev1/P1.0"
    controller.valve_service.master_valve_line = "Dev1/P1.0"
    controller.state.loaded_protocol = ProtocolDocument(
        source_path=Path("master.csv"),
        source_name="master.csv",
        trials=[ProtocolTrial("1", 0, 100, 1, TriggerMode.MANUAL)],
    )
    controller.state.flow_setpoints_ready = True
    controller.state.hardware_ready = True
    controller.state.telemetry.connected = True
    controller.state.telemetry.safety_state = "SAFE"
    controller.actuation_interlock.update(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        has_protocol=True,
        recording_ready=True,
    )
    commands = []

    def writer(command):
        commands.append(command)
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=command.expected_ns,
            wall_timestamp=10.0,
            result=ActuationResult.SUCCESS,
        )

    controller.actuation_worker.writer = writer

    controller.handle_protocol_start_requested()

    assert [(item.valve, item.category.value) for item in commands] == [(0, "warmup")]
    assert controller.valve_service.master_is_open() is True
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER

    controller.handle_protocol_manual_trigger_requested()
    controller.handle_breath_samples([-0.6], 10.0)

    assert [(item.valve, item.category.value) for item in commands[:2]] == [
        (0, "warmup"),
        (1, "normal"),
    ]


def test_protocol_start_stays_disarmed_when_master_prepare_fails(controller):
    from pathlib import Path

    from app.models import (
        ActuationReceipt,
        ActuationResult,
        ProtocolDocument,
        ProtocolExecutionStatus,
        ProtocolTrial,
        TriggerMode,
    )

    controller.state.master_valve_line = "Dev1/P1.0"
    controller.valve_service.master_valve_line = "Dev1/P1.0"
    controller.state.loaded_protocol = ProtocolDocument(
        source_path=Path("master-fail.csv"),
        source_name="master-fail.csv",
        trials=[ProtocolTrial("1", 0, 100, 1, TriggerMode.MANUAL)],
    )
    controller.state.flow_setpoints_ready = True
    controller.state.hardware_ready = True
    controller.state.telemetry.connected = True
    controller.state.telemetry.safety_state = "SAFE"
    controller.actuation_interlock.update(
        connected=True,
        hardware_ready=True,
        flow_setpoints_ready=True,
        safety_state="SAFE",
        has_protocol=True,
        recording_ready=True,
    )

    def writer(command):
        return ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns,
            actual_ns=None,
            wall_timestamp=10.0,
            result=ActuationResult.FAILED,
            message="master write failed",
        )

    controller.actuation_worker.writer = writer

    controller.handle_protocol_start_requested()

    assert controller.valve_service.master_is_open() is False
    assert controller.protocol_executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert controller._protocol_lease_epoch is None
    assert controller._protocol_master_prepare_pending is False
