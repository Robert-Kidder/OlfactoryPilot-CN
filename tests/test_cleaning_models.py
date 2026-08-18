from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    CleaningConfigSnapshot,
    CleaningOperationIdentity,
    CleaningOutcome,
    CleaningResult,
    CleaningStatus,
)


def _config() -> dict:
    return {
        "cleaning": {
            "enabled": True,
            "gas_label": "Air",
            "flow_channel": "A",
            "default_flow_sccm": 1500,
            "max_approved_flow_sccm": 1500,
            "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
            "default_open_duration_s": 10,
            "max_open_duration_s": 120,
            "default_cycles": 3,
            "max_cycles": 20,
            "parallel_open_limit": 1,
            "default_channels": [2, 3],
            "external_labels": {"2": "2", "3": "4"},
        }
    }


def test_default_cleaning_snapshot_builds_strict_single_channel_plan() -> None:
    snapshot = CleaningConfigSnapshot.from_effective_config(
        _config(),
        available_channels={2: "Dev1/P0.1", 3: "Dev1/P0.2"},
    )
    identity = CleaningOperationIdentity(operation_id="clean-1", generation=4)

    plan = snapshot.build_plan(identity)

    assert snapshot.selected_channels == (2, 3)
    assert snapshot.external_label_for(2) == "2"
    assert snapshot.external_label_for(3) == "4"
    assert plan.flow_setpoints_sccm == (("A", 1500.0), ("B", 0.0), ("C", 0.0))
    assert [(step.channel, step.action_kind) for step in plan.steps] == [
        (2, ActuationAction.OPEN),
        (2, ActuationAction.CLOSE),
        (3, ActuationAction.OPEN),
        (3, ActuationAction.CLOSE),
    ] * 3
    assert len({step.step_id for step in plan.steps}) == 12
    assert len({step.command_id for step in plan.steps}) == 12
    assert all(step.operation_id == "clean-1" for step in plan.steps)
    assert all(step.generation == 4 for step in plan.steps)
    assert all(step.target.startswith("Dev1/") for step in plan.steps)

    with pytest.raises(FrozenInstanceError):
        snapshot.cycles = 5  # type: ignore[misc]


def test_production_default_recipe_expands_eight_routes_for_three_cycles() -> None:
    config = json.loads(
        Path("config/default_config.json").read_text(encoding="utf-8")
    )
    available = {
        int(channel): target
        for channel, target in config["valve_mapping"]["variants"]["20-channel"].items()
    }
    snapshot = CleaningConfigSnapshot.from_effective_config(
        config,
        available_channels=available,
    )
    plan = snapshot.build_plan(CleaningOperationIdentity("production-default", 1))

    assert snapshot.selected_channels == tuple(range(2, 10))
    assert tuple(
        snapshot.external_label_for(channel) for channel in snapshot.selected_channels
    ) == ("2", "4", "6", "8", "12", "14", "16", "18")
    assert snapshot.open_duration_s == 10
    assert snapshot.cycles == 3
    assert plan.flow_setpoints_sccm == (("A", 1500.0), ("B", 0.0), ("C", 0.0))
    assert len(plan.steps) == 8 * 3 * 2
    assert all(
        plan.steps[index].action_kind != plan.steps[index + 1].action_kind
        for index in range(0, len(plan.steps), 2)
    )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"default_channels": []}, "至少选择一路"),
        ({"default_channels": [2, 2]}, "不得重复"),
        ({"default_channels": [4]}, "未配置"),
        ({"default_flow_sccm": 1501}, "批准范围"),
        ({"default_flow_sccm": float("nan")}, "有限"),
        ({"default_open_duration_s": 0}, "每路时间"),
        ({"default_cycles": 0}, "循环轮数"),
        ({"parallel_open_limit": 2}, "parallel_open_limit"),
    ],
)
def test_invalid_cleaning_recipe_is_rejected_before_plan(
    override: dict,
    message: str,
) -> None:
    config = _config()
    config["cleaning"].update(override)

    with pytest.raises(ValueError, match=message):
        CleaningConfigSnapshot.from_effective_config(
            config,
            available_channels={2: "Dev1/P0.1", 3: "Dev1/P0.2"},
        )


def test_cleaning_actuation_identity_round_trips_without_protocol_trial_identity() -> None:
    command = ActuationCommand(
        command_id="clean-1:0001",
        execution_epoch=0,
        arm_epoch=0,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=2,
        action=ActuationAction.OPEN,
        category=ActuationCategory.CLEANING,
        expected_ns=100,
        duration_ns=None,
        wall_timestamp=1.0,
        safety_generation=0,
        target_device="Dev1",
        target_line="P0.1",
        operation_id="clean-1",
        generation=3,
        step_id="step-0001",
        action_kind=ActuationAction.OPEN,
    )

    receipt = ActuationReceipt.from_write(
        command=command,
        started_ns=100,
        actual_ns=110,
        wall_timestamp=1.1,
        result=ActuationResult.SUCCESS,
    )

    assert receipt.category == ActuationCategory.CLEANING
    assert receipt.operation_id == "clean-1"
    assert receipt.generation == 3
    assert receipt.step_id == "step-0001"
    assert receipt.action_kind == ActuationAction.OPEN
    assert receipt.trial_id is None


def test_cleaning_result_distinguishes_safe_abort_from_recovery() -> None:
    identity = CleaningOperationIdentity("clean-2", 2)
    aborted = CleaningResult(
        identity=identity,
        status=CleaningStatus.COMPLETED,
        outcome=CleaningOutcome.ABORTED,
    )
    recovery = CleaningResult(
        identity=identity,
        status=CleaningStatus.RECOVERY_REQUIRED,
        outcome=CleaningOutcome.FAILED,
        reason="流量清零回执缺失",
    )

    assert aborted.safe_terminal is True
    assert recovery.safe_terminal is False
