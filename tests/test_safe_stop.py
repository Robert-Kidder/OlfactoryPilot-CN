from __future__ import annotations

import pytest

from app.models import (
    AppState,
    AZeroReceipt,
    SafeStopIdentity,
    SafeStopPlan,
    SafeStopStatus,
    SelectorConfig,
    SelectorReceipt,
    SelectorRoute,
)
from app.services.safety_manager import SafetyManager
from app.services.valve_service import ValveService


def _identity(generation: int = 3) -> SafeStopIdentity:
    return SafeStopIdentity("safe-stop-1", generation, execution_epoch=8)


def _plan() -> SafeStopPlan:
    return SafeStopPlan(_identity(), SelectorConfig("Dev2/P1.0"))


def test_selector_is_binary_route_without_closed_state() -> None:
    selector = SelectorConfig("Dev2/P1.0")

    assert selector.route_for_level(False) == SelectorRoute.COMPENSATION
    assert selector.route_for_level(True) == SelectorRoute.ODOR
    assert set(SelectorRoute) == {
        SelectorRoute.ODOR,
        SelectorRoute.COMPENSATION,
        SelectorRoute.UNKNOWN,
    }


def test_selector_rejects_odor_as_the_safe_route() -> None:
    with pytest.raises(ValueError, match="补偿出口"):
        SelectorConfig("Dev2/P1.0", safe_route=SelectorRoute.ODOR)


def test_app_state_parses_selector_without_adding_valve_21() -> None:
    state = AppState.from_config(
        {
            "hardware_variant": "20-channel",
            "valve_mapping": {
                "selector": {
                    "target": "Dev2/P1.0",
                    "safe_route": "compensation",
                    "safe_level": False,
                    "odor_level": True,
                },
                "variants": {
                    "20-channel": {
                        str(channel): f"Dev1/P0.{channel - 1}"
                        for channel in range(1, 3)
                    }
                },
            },
        }
    )

    assert state.selector == SelectorConfig("Dev2/P1.0")
    assert state.master_valve_line == "Dev2/P1.0"  # legacy read alias
    assert set(state.get_active_valve_map()) == {1, 2}


@pytest.mark.parametrize("safe_route", ["odor", "not-a-route"])
def test_app_state_disables_invalid_selector_safe_route(safe_route) -> None:
    state = AppState.from_config(
        {
            "valve_mapping": {
                "selector": {
                    "target": "Dev2/P1.0",
                    "safe_route": safe_route,
                }
            }
        }
    )

    assert state.selector is None
    assert state.master_valve_line == ""


@pytest.mark.parametrize(
    ("safe_level", "odor_level"),
    [("false", True), (False, "true")],
)
def test_app_state_rejects_string_selector_polarity(
    safe_level,
    odor_level,
) -> None:
    state = AppState.from_config(
        {
            "valve_mapping": {
                "selector": {
                    "target": "Dev2/P1.0",
                    "safe_level": safe_level,
                    "odor_level": odor_level,
                }
            }
        }
    )

    assert state.selector is None


def test_app_state_rejects_selector_target_reused_by_odor_valve() -> None:
    state = AppState.from_config(
        {
            "valve_mapping": {
                "selector": {"target": "Dev2/P1.0"},
                "variants": {"20-channel": {"1": "Dev2/P1.0"}},
            }
        }
    )

    assert state.selector is None
    assert state.master_valve_line == ""


def test_app_state_rejects_selector_physical_alias_and_odor_ids_outside_1_20() -> None:
    state = AppState.from_config(
        {
            "hardware_variant": "20-channel",
            "valve_mapping": {
                "selector": {"target": "Dev2/P1.0"},
                "variants": {
                    "20-channel": {
                        "0": "Dev1/P0.0",
                        "1": "Dev1/P0.1",
                        "21": "Dev1/P0.2",
                        "2": "dev2/port1/line0",
                    }
                },
            },
        }
    )

    assert set(state.get_active_valve_map()) == {1, 2}
    assert state.selector is None
    assert state.master_valve_line == ""


def test_valve_service_keeps_selector_out_of_odor_close_set() -> None:
    variants = {
        "20-channel": {
            channel: f"Dev{1 if channel <= 12 else 2}/P0.{channel - 1}"
            for channel in range(1, 21)
        }
    }
    state = AppState(
        hardware_variant="20-channel",
        valve_variants=variants,
        selector=SelectorConfig("Dev2/P1.0"),
        master_valve_line="Dev2/P1.0",
    )
    service = ValveService(
        state=state,
        safety_manager=SafetyManager(),
        worker=object(),
        valve_variants=variants,
        hardware_variant="20-channel",
        selector=state.selector,
    )

    closes = service.all_configured_close_steps()
    selector_step = service.selector_route_step(SelectorRoute.COMPENSATION)

    assert len(closes) == 20
    assert all(step.logical_valve != 0 for step in closes)
    assert all((step.device, step.line) != ("Dev2", "P1.0") for step in closes)
    assert (selector_step.logical_valve, selector_step.state, selector_step.role) == (
        0,
        False,
        "selector_safe_route",
    )


def test_safe_stop_requires_matching_a_zero_before_selector() -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")

    assert plan.accept_a_zero(
        AZeroReceipt("a-zero-1", _identity(), True, 0.0)
    )
    plan.expect_selector("selector-1")
    assert plan.accept_selector(
        SelectorReceipt(
            "selector-1",
            _identity(),
            "Dev2/P1.0",
            SelectorRoute.COMPENSATION,
            True,
        )
    )
    assert plan.complete(odors_closed=True, owners_handed_off=True)
    assert plan.status == SafeStopStatus.COMPLETED


def test_selector_cannot_be_requested_before_a_zero_receipt() -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")

    with pytest.raises(RuntimeError, match="禁止切换 selector"):
        plan.expect_selector("selector-1")


@pytest.mark.parametrize(
    ("receipt", "reason"),
    [
        (AZeroReceipt("wrong", _identity(), True, 0.0), "身份不匹配"),
        (AZeroReceipt("a-zero-1", _identity(4), True, 0.0), "身份不匹配"),
        (AZeroReceipt("a-zero-1", _identity(), True, 0.0, stale=True), "失效"),
        (AZeroReceipt("a-zero-1", _identity(), False, 0.0), "未确认"),
        (AZeroReceipt("a-zero-1", _identity(), True, 1.0), "未确认"),
        (AZeroReceipt("a-zero-1", _identity(), True, float("nan")), "未确认"),
    ],
)
def test_invalid_a_zero_receipt_requires_recovery(receipt, reason) -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")

    assert not plan.accept_a_zero(receipt)
    assert plan.status == SafeStopStatus.RECOVERY_REQUIRED
    assert reason in plan.recovery_reason


def test_conflicting_duplicate_a_zero_receipt_requires_recovery() -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")
    assert plan.accept_a_zero(AZeroReceipt("a-zero-1", _identity(), True, 0.0))

    assert not plan.accept_a_zero(
        AZeroReceipt("a-zero-1", _identity(), False, 0.0, message="conflict")
    )
    assert plan.status == SafeStopStatus.RECOVERY_REQUIRED
    assert "冲突" in plan.recovery_reason


def test_late_duplicate_a_zero_receipt_requires_recovery() -> None:
    plan = _plan()
    receipt = AZeroReceipt("a-zero-1", _identity(), True, 0.0)
    plan.expect_a_zero(receipt.command_id)
    assert plan.accept_a_zero(receipt)
    plan.expect_selector("selector-1")

    assert not plan.accept_a_zero(receipt)
    assert plan.status == SafeStopStatus.RECOVERY_REQUIRED
    assert "迟到" in plan.recovery_reason


def test_identical_duplicate_selector_receipt_requires_recovery() -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")
    assert plan.accept_a_zero(AZeroReceipt("a-zero-1", _identity(), True, 0.0))
    receipt = SelectorReceipt(
        "selector-1",
        _identity(),
        "Dev2/P1.0",
        SelectorRoute.COMPENSATION,
        True,
    )
    plan.expect_selector(receipt.command_id)
    assert plan.accept_selector(receipt)

    assert not plan.accept_selector(receipt)
    assert plan.status == SafeStopStatus.RECOVERY_REQUIRED
    assert "重复" in plan.recovery_reason


def test_selector_unknown_and_timeout_require_recovery() -> None:
    plan = _plan()
    plan.expect_a_zero("a-zero-1")
    assert plan.accept_a_zero(AZeroReceipt("a-zero-1", _identity(), True, 0.0))
    plan.expect_selector("selector-1")

    assert not plan.accept_selector(
        SelectorReceipt(
            "selector-1",
            _identity(),
            "Dev2/P1.0",
            SelectorRoute.UNKNOWN,
            False,
        )
    )
    assert plan.status == SafeStopStatus.RECOVERY_REQUIRED

    timed_out = _plan()
    timed_out.expect_a_zero("a-zero-2")
    timed_out.timeout("A 清零 receipt")
    assert timed_out.status == SafeStopStatus.RECOVERY_REQUIRED
    assert "超时" in timed_out.recovery_reason
