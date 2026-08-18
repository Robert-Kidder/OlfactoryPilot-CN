from dataclasses import replace

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


def test_dedicated_selector_safety_route_supports_safe_high_polarity() -> None:
    class HAL:
        def __init__(self) -> None:
            self.calls = []

        def write_digital_ack(self, *, device, line, state, timeout_ms):
            self.calls.append((device, line, state, timeout_ms))
            return DigitalWriteAck(True, 1_100, 1_200, 10.1)

    hal = HAL()
    adapter = ActuationDOAdapter(
        hal=hal,
        target_resolver=lambda valve: (_ for _ in ()).throw(AssertionError(valve)),
        selector_target="Dev2/P1.0",
    )
    command = ActuationCommand(
        command_id="selector-safe-high",
        execution_epoch=4,
        arm_epoch=5,
        sequence=6,
        trial_id=None,
        trial_index=None,
        valve=0,
        action=ActuationAction.OPEN,
        category=ActuationCategory.SAFETY,
        expected_ns=1_000,
        duration_ns=None,
        wall_timestamp=10.0,
        safety_generation=7,
        target_device="Dev2",
        target_line="P1.0",
        operation_id="safe-stop-1",
        generation=1,
        step_id="selector_safe",
        action_kind=ActuationAction.OPEN,
    )

    receipt = adapter.execute(command)

    assert receipt.result == ActuationResult.SUCCESS
    assert hal.calls == [("Dev2", "P1.0", True, 100)]

    wrong_target = adapter.execute(
        replace(command, command_id="wrong-target", target_line="P1.1")
    )
    assert wrong_target.result == ActuationResult.FAILED
    assert hal.calls == [("Dev2", "P1.0", True, 100)]


def test_selector_business_route_cannot_select_safe_level() -> None:
    class HAL:
        def __init__(self) -> None:
            self.calls = []

        def write_digital_ack(self, *, device, line, state, timeout_ms):
            self.calls.append((device, line, state, timeout_ms))
            return DigitalWriteAck(True, 1_100, 1_200, 10.1)

    hal = HAL()
    adapter = ActuationDOAdapter(
        hal=hal,
        target_resolver=lambda valve: (_ for _ in ()).throw(AssertionError(valve)),
        selector_target="Dev2/P1.0",
        selector_odor_level=True,
    )
    base = ActuationCommand(
        command_id="selector-business",
        execution_epoch=1,
        arm_epoch=1,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=0,
        action=ActuationAction.CLOSE,
        category=ActuationCategory.MASTER,
        expected_ns=1_000,
        duration_ns=None,
        wall_timestamp=10.0,
        safety_generation=1,
        target_device="Dev2",
        target_line="P1.0",
        operation_id="plan-1",
        generation=1,
        step_id="selector_odor",
        action_kind=ActuationAction.CLOSE,
    )

    rejected = adapter.execute(base)
    accepted = adapter.execute(
        replace(
            base,
            command_id="selector-odor",
            action=ActuationAction.OPEN,
            action_kind=ActuationAction.OPEN,
        )
    )

    assert rejected.result == ActuationResult.FAILED
    assert accepted.result == ActuationResult.SUCCESS
    assert hal.calls == [("Dev2", "P1.0", True, 100)]
