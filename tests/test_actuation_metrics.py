from dataclasses import FrozenInstanceError

import pytest

from app.models.actuation import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
)
from app.services.actuation_metrics import ActuationMetrics, ActuationMetricsConfig


def _command(
    *,
    command_id: str = "cmd-1",
    action: ActuationAction = ActuationAction.OPEN,
    expected_ns: int = 1_000_000_000,
) -> ActuationCommand:
    return ActuationCommand(
        command_id=command_id,
        execution_epoch=3,
        arm_epoch=7,
        sequence=11,
        trial_id="trial-1",
        trial_index=0,
        valve=3,
        action=action,
        category=ActuationCategory.NORMAL,
        expected_ns=expected_ns,
        duration_ns=100_000_000,
        wall_timestamp=100.0,
        safety_generation=5,
    )


def _receipt(
    jitter_ms: float,
    *,
    command_id: str = "cmd-1",
    action: ActuationAction = ActuationAction.OPEN,
    result: ActuationResult = ActuationResult.SUCCESS,
    category: ActuationCategory = ActuationCategory.NORMAL,
    stale: bool = False,
) -> ActuationReceipt:
    command = _command(command_id=command_id, action=action)
    offset_ns = int(round(jitter_ms * 1_000_000))
    return ActuationReceipt.from_write(
        command=command,
        started_ns=command.expected_ns,
        actual_ns=command.expected_ns + offset_ns,
        wall_timestamp=101.0,
        result=result,
        category=category,
        stale=stale,
    )


def test_action_models_are_frozen_and_preserve_signed_offset() -> None:
    command = _command()
    receipt = ActuationReceipt.from_write(
        command=command,
        started_ns=1_001_000_000,
        actual_ns=1_002_500_000,
        wall_timestamp=101.0,
        result=ActuationResult.SUCCESS,
    )

    assert receipt.offset_ms == pytest.approx(2.5)
    assert receipt.jitter_ms == pytest.approx(2.5)
    assert receipt.measurement_point == "daqmx_write_ack"
    assert receipt.execution_epoch == command.execution_epoch
    assert receipt.arm_epoch == command.arm_epoch
    with pytest.raises(FrozenInstanceError):
        command.sequence = 12  # type: ignore[misc]


def test_receipt_rejects_invalid_measurement_order_instead_of_masking_it() -> None:
    command = _command()

    with pytest.raises(ValueError, match="expected_ns <= started_ns <= actual_ns"):
        ActuationReceipt.from_write(
            command=command,
            started_ns=command.expected_ns - 1,
            actual_ns=command.expected_ns + 1,
            wall_timestamp=101.0,
            result=ActuationResult.SUCCESS,
        )


def test_metrics_use_nearest_rank_and_independent_rolling_windows() -> None:
    metrics = ActuationMetrics(
        ActuationMetricsConfig(window_size=100, min_samples=20, target_ms=20.0, single_limit_ms=30.0)
    )
    for index in range(1, 101):
        action = ActuationAction.OPEN if index % 2 else ActuationAction.CLOSE
        metrics.record(_receipt(float(index), command_id=f"cmd-{index}", action=action))

    snapshot = metrics.snapshot()
    assert snapshot.open.sample_count == 50
    assert snapshot.open.p95_ms == 95.0
    assert snapshot.close.sample_count == 50
    assert snapshot.close.p95_ms == 96.0
    assert snapshot.combined.sample_count == 100
    assert snapshot.combined.p95_ms == 95.0

    metrics.record(_receipt(0.5, command_id="cmd-101", action=ActuationAction.OPEN))
    assert metrics.snapshot().combined.sample_count == 100


def test_p95_and_single_thresholds_are_strict_with_warning_transitions() -> None:
    metrics = ActuationMetrics(
        ActuationMetricsConfig(window_size=100, min_samples=20, target_ms=20.0, single_limit_ms=30.0)
    )
    transitions = []
    for index in range(20):
        update = metrics.record(_receipt(20.0, command_id=f"boundary-{index}"))
        transitions.extend(update.warning_transitions)

    assert metrics.snapshot().open.warning is False
    assert metrics.snapshot().open.target_met is False
    assert transitions == []
    assert metrics.severe_latched is False

    first_over = metrics.record(_receipt(20.000001, command_id="warning-prime"))
    assert first_over.warning_transitions == ()
    entered = metrics.record(_receipt(20.000001, command_id="warning-enter"))
    assert [(item.stream, item.active) for item in entered.warning_transitions] == [
        ("open", True),
        ("combined", True),
    ]
    assert entered.severe is False

    exact = metrics.record(_receipt(30.0, command_id="exact-30"))
    assert exact.severe is False
    severe = metrics.record(_receipt(30.000001, command_id="over-30"))
    assert severe.severe is True
    assert metrics.severe_latched is True

    metrics.reset()
    for index in range(20):
        metrics.record(_receipt(21.0, command_id=f"high-{index}"))
    recovered = None
    for index in range(100):
        update = metrics.record(_receipt(1.0, command_id=f"low-{index}"))
        if update.warning_transitions:
            recovered = update
    assert recovered is not None
    assert [(item.stream, item.active) for item in recovered.warning_transitions] == [
        ("open", False),
        ("combined", False),
    ]


@pytest.mark.parametrize(
    ("result", "category", "stale"),
    [
        (ActuationResult.FAILED, ActuationCategory.NORMAL, False),
        (ActuationResult.CANCELLED, ActuationCategory.NORMAL, False),
        (ActuationResult.SUCCESS, ActuationCategory.SAFETY, False),
        (ActuationResult.SUCCESS, ActuationCategory.NORMAL, True),
    ],
)
def test_non_normal_success_receipts_are_excluded_from_quality_samples(
    result: ActuationResult,
    category: ActuationCategory,
    stale: bool,
) -> None:
    metrics = ActuationMetrics()
    update = metrics.record(_receipt(5.0, result=result, category=category, stale=stale))

    assert update.included is False
    assert metrics.snapshot().combined.sample_count == 0


def test_config_falls_back_safely_for_missing_or_invalid_values() -> None:
    defaults = ActuationMetricsConfig.from_mapping({})
    invalid = ActuationMetricsConfig.from_mapping(
        {
            "actuation_jitter_target_ms": float("nan"),
            "actuation_jitter_single_limit_ms": 0,
            "actuation_jitter_window_size": -1,
            "actuation_jitter_min_samples": 101,
        }
    )

    assert defaults == ActuationMetricsConfig()
    assert invalid == ActuationMetricsConfig()
