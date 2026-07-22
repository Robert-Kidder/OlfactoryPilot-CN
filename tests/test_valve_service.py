from __future__ import annotations

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    AppState,
    SafetyState,
)
from app.services import SafetyManager, ValveService


class DummyWorker:
    def __init__(self) -> None:
        self.commands: list[tuple[str | None, str, bool]] = []
        self.fail_master_writes = False

    @property
    def is_connected(self) -> bool:
        return True

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        self.commands.append((device, line, state))
        if self.fail_master_writes and line == "P1.0":
            return False
        return True


def build_service() -> ValveService:
    state = AppState(
        hardware_variant="20-channel",
        valve_variants={"20-channel": {1: "Dev1/P0.0"}},
        master_valve_line="Dev1/P1.0",
    )
    worker = DummyWorker()
    return ValveService(
        state=state,
        safety_manager=SafetyManager(low_flow_threshold=0.2),
        worker=worker,
        valve_variants=state.valve_variants,
        hardware_variant=state.hardware_variant,
        master_valve_line=state.master_valve_line,
    )


def test_valve_blocked_when_not_safe():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="LOW_FLOW",
        airflow=0.0,
        threshold=0.2,
        updated_at=0.0,
        reason="气流低于阈值",
    )
    ok, message = service.set_valve(1, True, safety_state=safety_state)
    assert not ok
    assert "安全" in message


def test_valve_blocked_until_mfc_setpoints_ready():
    service = build_service()
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.0,
        threshold=0.2,
        updated_at=0.0,
        reason="idle",
    )

    ok, message = service.set_valve(1, True, safety_state=safety_state)

    assert not ok
    assert "MFC" in message
    assert service.worker.commands == []


def test_valve_toggle_sets_master_valve():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.3,
        threshold=0.2,
        updated_at=0.0,
        reason="",
    )
    ok, message = service.set_valve(1, True, safety_state=safety_state)
    assert ok
    assert "打开" in message
    assert service.is_open(1)
    # master + channel command recorded
    assert service.worker.commands[0] == ("Dev1", "P1.0", True)
    assert service.worker.commands[1][0:2] == ("Dev1", "P0.0")
    # close channel -> master保持开启但通道关闭
    ok, _ = service.set_valve(1, False, safety_state=safety_state)
    assert ok
    assert service.worker.commands[-1][-1] is False


def test_master_state_reflects_commands():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="",
    )
    ok, _ = service.set_valve(1, True, safety_state=safety_state)
    assert ok
    assert service.master_is_open() is True  # 常开
    ok, _ = service.set_valve(1, False, safety_state=safety_state)
    assert ok
    assert service.master_is_open() is True  # 不再随通道关闭


def test_master_reopens_after_cached_state_reset():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="",
    )
    ok, _ = service.set_valve(1, True, safety_state=safety_state)
    assert ok
    first_commands = list(service.worker.commands)

    service.reset_cached_state()
    ok, _ = service.set_valve(1, True, safety_state=safety_state)
    assert ok
    # Master should be driven again after cache reset
    assert service.worker.commands[:2] == first_commands[:2]
    assert service.worker.commands[2:4] == first_commands[:2]


def test_blocks_when_mapping_missing():
    state = AppState(
        hardware_variant="20-channel",
        valve_variants={"20-channel": {}},
        master_valve_line="Dev1/P1.0",
    )
    worker = DummyWorker()
    service = ValveService(
        state=state,
        safety_manager=SafetyManager(low_flow_threshold=0.2),
        worker=worker,
        valve_variants=state.valve_variants,
        hardware_variant=state.hardware_variant,
        master_valve_line=state.master_valve_line,
    )
    state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="",
    )
    ok, message = service.set_valve(1, True, safety_state=safety_state)
    assert ok is False
    assert "未找到 20 通道映射" in message
    assert worker.commands == []


def test_closing_valve_does_not_require_master_write():
    service = build_service()
    service.state.flow_setpoints_ready = True
    service._states[1] = True
    service.worker.fail_master_writes = True
    safety_state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=0.0,
        reason="",
    )

    ok, message = service.set_valve(1, False, safety_state=safety_state)

    assert ok is True
    assert "关闭" in message
    assert service.worker.commands == [("Dev1", "P0.0", False)]


def test_safety_close_is_allowed_when_not_safe():
    service = build_service()
    service.state.flow_setpoints_ready = True
    service._states[1] = True
    safety_state = SafetyState(
        state="LOW_FLOW",
        airflow=0.0,
        threshold=0.2,
        updated_at=1.0,
        reason="气流低",
    )

    ok, message = service.set_valve(
        1,
        False,
        safety_state=safety_state,
        safety_close=True,
    )

    assert ok is True
    assert "安全关闭" in message
    assert service.is_open(1) is False
    assert service.worker.commands == [("Dev1", "P0.0", False)]


def test_safety_close_cannot_open_when_not_safe():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety_state = SafetyState(
        state="LOW_FLOW",
        airflow=0.0,
        threshold=0.2,
        updated_at=1.0,
        reason="气流低",
    )

    ok, message = service.set_valve(
        1,
        True,
        safety_state=safety_state,
        safety_close=True,
    )

    assert ok is False
    assert "安全关闭参数不能用于打开阀门" in message
    assert service.is_open(1) is False
    assert service.worker.commands == []


def test_async_plan_contains_master_then_channel_without_mutating_cache():
    service = build_service()
    service.state.flow_setpoints_ready = True
    safety = SafetyState("SAFE", 0.5, 0.2, 1.0, "")

    ok, plan_or_message = service.plan_valve(1, True, safety_state=safety)

    assert ok is True
    plan = plan_or_message
    assert [(step.logical_valve, step.device, step.line, step.state) for step in plan.steps] == [
        (0, "Dev1", "P1.0", True),
        (1, "Dev1", "P0.0", True),
    ]
    assert service.master_is_open() is False
    assert service.is_open(1) is False


def test_async_cache_commits_only_successful_receipt():
    service = build_service()
    step = service.plan_valve(
        1,
        False,
        safety_state=SafetyState("LOW_FLOW", 0.0, 0.2, 1.0, ""),
        safety_close=True,
    )[1].steps[0]
    command = ActuationCommand(
        command_id="close",
        execution_epoch=1,
        arm_epoch=1,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=step.logical_valve,
        action=ActuationAction.CLOSE,
        category=ActuationCategory.SAFETY,
        expected_ns=1_000,
        duration_ns=None,
        wall_timestamp=1.0,
        safety_generation=1,
        target_device=step.device,
        target_line=step.line,
    )
    failed = ActuationReceipt.from_write(
        command=command,
        started_ns=1_000,
        actual_ns=None,
        wall_timestamp=1.0,
        result=ActuationResult.FAILED,
    )

    service.commit_receipt(failed)
    assert service.is_open(1) is False
    service._states[1] = True
    service.commit_receipt(failed)
    assert service.is_open(1) is True

    succeeded = ActuationReceipt.from_write(
        command=command,
        started_ns=1_000,
        actual_ns=1_100,
        wall_timestamp=1.0,
        result=ActuationResult.SUCCESS,
    )
    service.commit_receipt(succeeded)
    assert service.is_open(1) is False
