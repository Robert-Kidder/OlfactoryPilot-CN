from __future__ import annotations

import math
import threading

import pytest

from app.services.ttl_trigger_service import (
    TtlInputError,
    TtlPulse,
    TtlTriggerConfig,
    TtlTriggerService,
)


def _service(*, debounce_ms: float = 0.0) -> TtlTriggerService:
    return TtlTriggerService(
        TtlTriggerConfig(
            high_threshold_v=2.0,
            low_threshold_v=0.8,
            debounce_ms=debounce_ms,
            poll_hz=1000,
        )
    )


def test_low_high_sustained_high_emits_one_then_low_rearms_next_rise() -> None:
    service = _service()
    service.arm(arm_epoch=7)

    pulses = [
        service.process_sample(value, timestamp=index / 1000)
        for index, value in enumerate([0.0, 2.1, 4.0, 3.0, 0.7, 2.5])
    ]

    emitted = [pulse for pulse in pulses if pulse is not None]
    assert emitted == [
        TtlPulse(timestamp=0.001, arm_epoch=7, sequence=1),
        TtlPulse(timestamp=0.005, arm_epoch=7, sequence=2),
    ]


def test_hysteresis_band_does_not_change_latch_and_thresholds_are_inclusive() -> None:
    service = _service()
    service.arm(arm_epoch=1)

    assert service.process_sample(0.8, timestamp=0.0) is None
    first = service.process_sample(2.0, timestamp=0.001)
    assert first is not None
    assert service.process_sample(1.0, timestamp=0.002) is None
    assert service.process_sample(2.0, timestamp=0.003) is None
    assert service.process_sample(0.8, timestamp=0.004) is None
    second = service.process_sample(2.0, timestamp=0.005)
    assert second is not None
    assert second.sequence == 2


def test_debounce_requires_stable_high_and_low_without_sleep() -> None:
    service = _service(debounce_ms=2.0)
    service.arm(arm_epoch=3)

    assert service.process_sample(0.0, timestamp=0.000) is None
    assert service.process_sample(2.1, timestamp=0.001) is None
    assert service.process_sample(0.0, timestamp=0.002) is None
    assert service.process_sample(2.1, timestamp=0.003) is None
    pulse = service.process_sample(2.1, timestamp=0.005)
    assert pulse == TtlPulse(timestamp=0.005, arm_epoch=3, sequence=1)
    assert service.process_sample(0.0, timestamp=0.006) is None
    assert service.process_sample(2.1, timestamp=0.007) is None
    assert service.process_sample(0.0, timestamp=0.008) is None
    assert service.process_sample(0.0, timestamp=0.010) is None
    assert service.process_sample(2.1, timestamp=0.011) is None
    second = service.process_sample(2.1, timestamp=0.013)
    assert second == TtlPulse(timestamp=0.013, arm_epoch=3, sequence=2)


def test_ttl_manual_ttl_while_high_does_not_create_synthetic_rise() -> None:
    service = _service()
    service.arm(arm_epoch=1)
    service.process_sample(0.0, timestamp=0.0)
    first = service.process_sample(3.0, timestamp=0.001)
    service.disarm()
    service.arm(arm_epoch=2)

    assert first == TtlPulse(timestamp=0.001, arm_epoch=1, sequence=1)
    assert service.process_sample(3.0, timestamp=0.002) is None
    assert service.process_sample(0.0, timestamp=0.003) is None
    assert service.process_sample(3.0, timestamp=0.004) == TtlPulse(
        timestamp=0.004,
        arm_epoch=2,
        sequence=2,
    )


def test_payload_is_frozen_and_captures_epoch_at_sample_time() -> None:
    service = _service()
    service.arm(arm_epoch=10)
    service.process_sample(0.0, timestamp=1.0)
    pulse = service.process_sample(2.1, timestamp=1.1)
    service.arm(arm_epoch=11)

    assert pulse == TtlPulse(timestamp=1.1, arm_epoch=10, sequence=1)
    with pytest.raises((AttributeError, TypeError)):
        pulse.arm_epoch = 99  # type: ignore[misc]


def test_epoch_change_during_debounce_cannot_relabel_old_edge() -> None:
    service = _service(debounce_ms=5.0)
    service.arm(arm_epoch=1)
    service.process_sample(0.0, timestamp=0.000)
    assert service.process_sample(3.0, timestamp=0.001) is None

    service.arm(arm_epoch=2)

    assert service.process_sample(3.0, timestamp=0.006) is None
    assert service.process_sample(0.0, timestamp=0.007) is None
    assert service.process_sample(3.0, timestamp=0.008) is None
    assert service.process_sample(3.0, timestamp=0.013) == TtlPulse(
        timestamp=0.013,
        arm_epoch=2,
        sequence=1,
    )


def test_epoch_change_waits_for_inflight_edge_to_capture_old_epoch() -> None:
    service = _service(debounce_ms=5.0)
    service.arm(arm_epoch=1)
    service.process_sample(0.0, timestamp=0.000)
    assert service.process_sample(3.0, timestamp=0.001) is None
    entered_capture = threading.Event()
    release_capture = threading.Event()
    arm_finished = threading.Event()
    original_clear = service._clear_candidate
    first_clear = {"pending": True}

    def blocking_clear() -> None:
        original_clear()
        if first_clear["pending"]:
            first_clear["pending"] = False
            entered_capture.set()
            assert release_capture.wait(1.0)

    service._clear_candidate = blocking_clear
    pulses: list[TtlPulse | None] = []
    reader = threading.Thread(
        target=lambda: pulses.append(service.process_sample(3.0, timestamp=0.006))
    )
    armer = threading.Thread(
        target=lambda: (service.arm(arm_epoch=2), arm_finished.set())
    )

    reader.start()
    assert entered_capture.wait(1.0)
    armer.start()
    assert not arm_finished.wait(0.05)
    release_capture.set()
    reader.join(1.0)
    armer.join(1.0)

    assert not reader.is_alive()
    assert not armer.is_alive()
    assert pulses == [TtlPulse(timestamp=0.006, arm_epoch=1, sequence=1)]
    assert service.arm_epoch == 2


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_sample_is_explicit_error(value: float) -> None:
    service = _service()

    with pytest.raises(TtlInputError, match="TTL 输入样本"):
        service.process_sample(value, timestamp=1.0)


def test_invalid_config_uses_safe_defaults_and_logs_chinese_warning(caplog) -> None:
    config = TtlTriggerConfig.from_mapping(
        {
            "ttl_high_threshold_v": math.nan,
            "ttl_low_threshold_v": 3.0,
            "ttl_debounce_ms": -1,
            "ttl_poll_hz": 0,
        }
    )

    assert config == TtlTriggerConfig()
    assert "TTL 配置无效" in caplog.text
