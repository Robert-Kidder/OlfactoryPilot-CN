from __future__ import annotations

from app.models.safety_state import SafetyState
from app.services import SafetyManager


def test_low_flow_sets_state_and_reason():
    manager = SafetyManager(low_flow_threshold=0.5, recovery_margin=0.1)

    state = manager.evaluate_state(airflow=0.2, timestamp=1.0)

    assert state.state == "LOW_FLOW"
    assert "阈值" in state.reason


def test_hysteresis_requires_margin_for_recovery():
    manager = SafetyManager(low_flow_threshold=0.5, recovery_margin=0.1)

    low = manager.evaluate_state(airflow=0.2, timestamp=1.0)
    mid = manager.evaluate_state(airflow=0.54, timestamp=1.1, previous=low)
    recovered = manager.evaluate_state(airflow=0.65, timestamp=1.2, previous=mid)

    assert low.state == "LOW_FLOW"
    assert mid.state == "LOW_FLOW"  # still sticky
    assert recovered.state == "SAFE"


def test_stale_data_detected_when_gap_exceeds_threshold():
    manager = SafetyManager(low_flow_threshold=0.2, stale_after_s=1.0)
    prev = SafetyState(
        state="SAFE",
        airflow=0.8,
        threshold=0.2,
        updated_at=0.0,
        reason="ok",
    )

    stale = manager.evaluate_state(airflow=0.8, timestamp=2.1, previous=prev)

    assert stale.state == "DATA_STALE"
    assert "过期" in stale.reason


def test_invalid_airflow_flags_stale():
    manager = SafetyManager(low_flow_threshold=0.2)

    stale = manager.evaluate_state(airflow=float("nan"), timestamp=1.0)

    assert stale.state == "DATA_STALE"
    assert "异常" in stale.reason


def test_hardware_override_wins_over_flow_state():
    manager = SafetyManager(low_flow_threshold=0.2)

    state = manager.evaluate_state(airflow=0.8, timestamp=1.0, hardware_state="FAULT")

    assert state.state == "FAULT"
    assert "硬件" in state.reason


def test_hardware_fault_latches_until_safe():
    manager = SafetyManager(low_flow_threshold=0.2)
    previous = SafetyState(
        state="FAULT",
        airflow=0.1,
        threshold=0.2,
        updated_at=1.0,
        reason="硬件故障",
    )

    latched = manager.evaluate_state(airflow=0.8, timestamp=2.0, previous=previous)
    cleared = manager.evaluate_state(
        airflow=0.8, timestamp=3.0, previous=latched, hardware_state="SAFE"
    )

    assert latched.state == "FAULT"
    assert cleared.state == "SAFE"


def test_evaluate_retains_string_api():
    manager = SafetyManager(low_flow_threshold=0.5, recovery_margin=0.1)

    result = manager.evaluate(airflow=0.2, timestamp=1.0, previous_state="SAFE")

    assert result == "LOW_FLOW"


def test_validate_threshold_rules():
    manager = SafetyManager(low_flow_threshold=0.2)

    invalids = [None, "abc", float("nan"), float("inf"), 0, -1, 1001]
    for val in invalids:
        ok, _ = manager.validate_threshold(val)
        assert ok is False

    ok, msg = manager.validate_threshold(0.5)
    assert ok is True
    assert "有效" in msg


def test_guard_blocks_when_not_ready():
    manager = SafetyManager(low_flow_threshold=0.2)
    state = SafetyState(
        state="SAFE",
        airflow=0.8,
        threshold=0.2,
        updated_at=1.0,
        reason="ok",
    )

    allowed, reason = manager.guard_command(
        safety_state=state, hardware_ready=False, action="Connect", source="Protocol"
    )

    assert allowed is False
    assert "硬件未就绪" in reason


def test_guard_blocks_low_flow_state():
    manager = SafetyManager(low_flow_threshold=0.2)
    state = SafetyState(
        state="LOW_FLOW",
        airflow=0.1,
        threshold=0.2,
        updated_at=1.0,
        reason="气流低于阈值 0.20",
    )

    allowed, reason = manager.guard_command(
        safety_state=state, hardware_ready=True, action="FlowApply", source="Protocol"
    )

    assert allowed is False
    assert "安全阻断" in reason


def test_guard_allows_safe_state():
    manager = SafetyManager(low_flow_threshold=0.2)
    state = SafetyState(
        state="SAFE",
        airflow=0.5,
        threshold=0.2,
        updated_at=1.0,
        reason="ok",
    )

    allowed, reason = manager.guard_command(
        safety_state=state, hardware_ready=True, action="FlowApply", source="Protocol"
    )

    assert allowed is True
    assert reason == "允许执行"


def test_infinite_airflow_is_treated_as_stale():
    manager = SafetyManager(low_flow_threshold=0.2)

    state = manager.evaluate_state(airflow=float("inf"), timestamp=1.0)

    assert state.state == "DATA_STALE"
