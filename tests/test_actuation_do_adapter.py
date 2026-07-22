from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationResult,
)
from app.services.actuation_do_adapter import ActuationDOAdapter
from app.services.hal import DigitalWriteAck


def _command(
    *,
    action: ActuationAction = ActuationAction.OPEN,
    category: ActuationCategory = ActuationCategory.NORMAL,
) -> ActuationCommand:
    return ActuationCommand(
        command_id="cmd",
        execution_epoch=1,
        arm_epoch=2,
        sequence=3,
        trial_id="trial",
        trial_index=0,
        valve=9,
        action=action,
        category=category,
        expected_ns=1_000,
        duration_ns=100_000_000 if action == ActuationAction.OPEN else None,
        wall_timestamp=10.0,
        safety_generation=4,
    )


def test_adapter_preserves_hal_write_measurement_without_retimestamping() -> None:
    class HAL:
        def __init__(self) -> None:
            self.calls = []

        def write_digital_ack(self, *, device, line, state, timeout_ms):
            self.calls.append((device, line, state, timeout_ms))
            return DigitalWriteAck(
                success=True,
                started_ns=1_100,
                actual_ns=1_200,
                wall_timestamp=10.1,
            )

    hal = HAL()
    adapter = ActuationDOAdapter(
        hal=hal,
        target_resolver=lambda valve: ("Dev1", "P1.0"),
        write_timeout_ms=100,
    )

    receipt = adapter.execute(_command())

    assert hal.calls == [("Dev1", "P1.0", True, 100)]
    assert receipt.started_ns == 1_100
    assert receipt.actual_ns == 1_200
    assert receipt.offset_ms == 0.0002
    assert receipt.measurement_point == "daqmx_write_ack"


def test_safety_category_can_only_close_and_never_calls_hal_for_open() -> None:
    class HAL:
        def write_digital_ack(self, **kwargs):
            raise AssertionError("must not write")

    adapter = ActuationDOAdapter(
        hal=HAL(),
        target_resolver=lambda valve: ("Dev1", "P1.0"),
    )

    receipt = adapter.execute(
        _command(action=ActuationAction.OPEN, category=ActuationCategory.SAFETY)
    )

    assert receipt.result == ActuationResult.FAILED
    assert "安全关闭命令不能用于打开" in receipt.message
