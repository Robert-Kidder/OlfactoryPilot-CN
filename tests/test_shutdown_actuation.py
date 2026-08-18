from __future__ import annotations

import pytest

from app.models import (
    AppState,
    AZeroReceipt,
    SafeStopIdentity,
    SelectorConfig,
    SelectorReceipt,
    SelectorRoute,
)
from app.services.shutdown_service import ShutdownService


class _Hardware:
    def __init__(self, order: list[str], *, ai_released: bool = True) -> None:
        self.order = order
        self.ai_released = ai_released

    def stop_heaters(self) -> bool:
        self.order.append("heaters")
        return True

    def flush_logs(self) -> None:
        self.order.append("flush")

    def release_ai_resources(self) -> bool:
        self.order.append("ai_release")
        return self.ai_released

    def stop(self) -> None:
        self.order.append("hardware_stop")


class _FlushFailingHardware(_Hardware):
    def flush_logs(self) -> None:
        self.order.append("flush")
        raise OSError("synthetic recorder disk failure")


class _Actuation:
    def __init__(
        self,
        order: list[str],
        *,
        close=True,
        selector=True,
        stopped=True,
        fallback=True,
    ) -> None:
        self.order = order
        self.close = close
        self.selector = selector
        self.stopped = stopped
        self.fallback = fallback

    def fence_for_safe_stop(self, **values):
        self.order.append(f"fence:{values['timeout_ms']}")
        return SafeStopIdentity(
            values["operation_id"],
            values["generation"],
            execution_epoch=9,
        )

    def route_selector_safe(self, plan, timeout_ms: int):
        self.order.append(f"selector:{timeout_ms}")
        command_id = "selector-safe"
        plan.expect_selector(command_id)
        return SelectorReceipt(
            command_id,
            plan.identity,
            plan.selector.target,
            SelectorRoute.COMPENSATION if self.selector else SelectorRoute.UNKNOWN,
            self.selector,
        )

    def close_odors_for_safe_stop(self, identity, timeout_ms: int) -> bool:
        self.order.append(f"odors:{timeout_ms}")
        return self.close

    def shutdown(self, timeout_ms: int) -> bool:
        self.order.append(f"actuation_stop:{timeout_ms}")
        return self.stopped

    def handoff_maintenance_for_safe_stop(self) -> bool:
        self.order.append("maintenance_handoff")
        return True

    def fallback_close_all_after_handoff(self) -> bool:
        self.order.append("fallback")
        return self.fallback


class _Flow:
    def __init__(
        self,
        order: list[str],
        *,
        a_zero: bool = True,
        all_zero: bool = True,
        lease_released: bool = True,
        serial_stopped: bool = True,
    ) -> None:
        self.order = order
        self.a_zero = a_zero
        self.all_zero = all_zero
        self.lease_released = lease_released
        self.serial_stopped = serial_stopped

    def zero_a_for_safe_stop(self, identity, timeout_ms: int):
        self.order.append(f"a_zero:{timeout_ms}")
        return AZeroReceipt(
            "a-zero",
            identity,
            self.a_zero,
            0.0,
            message="A zero failed" if not self.a_zero else "",
        )

    def zero_all_for_safe_stop(self, identity, timeout_ms: int) -> bool:
        self.order.append(f"all_zero:{timeout_ms}")
        return self.all_zero

    def release_lease_for_safe_stop(self, identity, evidence=None) -> bool:
        if evidence is not None:
            assert evidence.complete
        self.order.append("lease_release")
        return self.lease_released

    def shutdown(self, timeout_ms: int) -> bool:
        self.order.append(f"serial_stop:{timeout_ms}")
        return self.serial_stopped


def _state() -> AppState:
    state = AppState.from_config({"low_flow_threshold": 0.2, "safety_state": "SAFE"})
    state.hardware_ready = True
    state.telemetry.connected = True
    return state


def _service(**kwargs) -> ShutdownService:
    return ShutdownService(selector=SelectorConfig("Dev2/P1.0"), **kwargs)


def test_shutdown_orders_close_handoff_ai_then_serial(tmp_path, caplog) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "shutdown.json",
        actuation_timeout_ms=321,
        emergency_close_timeout_ms=123,
    )

    event = service.shutdown(source="test")

    assert event["result"] == "success"
    assert order == [
        "fence:321",
        "a_zero:321",
        "selector:123",
        "odors:123",
        "all_zero:321",
        "maintenance_handoff",
        "actuation_stop:321",
        "lease_release",
        "heaters",
        "flush",
        "ai_release",
        "serial_stop:321",
    ]
    assert "Shutdown guard blocked" not in caplog.text


def test_shutdown_fallback_only_after_do_owner_handoff(tmp_path) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order, close=False, stopped=True, fallback=True),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "fallback.json",
    )
    assert service.shutdown(source="test")["result"] == "success"
    assert order.index("fallback") > order.index("actuation_stop:2000")


def test_shutdown_does_not_cross_thread_fallback_when_daq_owner_is_stuck(tmp_path) -> None:
    order = []
    state = _state()
    service = _service(
        state=state,
        worker=_Hardware(order),
        actuation_worker=_Actuation(order, close=False, stopped=False),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "stuck.json",
    )

    event = service.shutdown(source="test")

    assert event["result"] == "recovery_required"
    assert "fallback" not in order
    assert "DO ownership" in event["error"]
    assert state.hardware_ready is False


def test_shutdown_is_unsafe_when_ai_worker_does_not_stop(tmp_path) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order, ai_released=False),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "ai-stuck.json",
    )

    event = service.shutdown(source="test")

    assert event["result"] == "recovery_required"
    assert event["valves_closed"] is False
    assert "AI" in event["error"]


def test_shutdown_receipts_and_owner_release_continue_after_log_failure(
    tmp_path,
) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_FlushFailingHardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "disk-failed.json",
        actuation_timeout_ms=321,
        emergency_close_timeout_ms=123,
    )

    event = service.shutdown(source="recorder-failed")

    assert event["result"] == "unsafe"
    assert event["valves_closed"] is True
    assert "flush" in event["error"]
    assert order == [
        "fence:321",
        "a_zero:321",
        "selector:123",
        "odors:123",
        "all_zero:321",
        "maintenance_handoff",
        "actuation_stop:321",
        "lease_release",
        "heaters",
        "flush",
        "ai_release",
        "serial_stop:321",
    ]


def test_a_zero_failure_never_routes_selector_and_requires_recovery(tmp_path) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order, a_zero=False),
        retry_limit=0,
        record_path=tmp_path / "a-zero-failed.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["a_zero_confirmed"] is False
    assert event["selector_safe_confirmed"] is False
    assert not any(item.startswith("selector:") for item in order)
    assert any(item.startswith("odors:") for item in order)


def test_selector_failure_is_recovery_required_not_safe_stop(tmp_path) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order, selector=False),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "selector-failed.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["a_zero_confirmed"] is True
    assert event["selector_safe_confirmed"] is False
    assert order.index("a_zero:2000") < order.index("selector:500")


def test_missing_selector_is_recovery_required_and_never_reports_safe_stop(tmp_path) -> None:
    order = []
    service = ShutdownService(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "selector-missing.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["safe_stop_status"] == "recovery_required"
    assert event["a_zero_confirmed"] is True
    assert event["selector_safe_confirmed"] is False
    assert any(item.startswith("a_zero:") for item in order)
    assert not any(item.startswith("selector:") for item in order)
    assert order.index("a_zero:2000") < order.index("odors:500")


def test_unconfirmed_owner_fence_is_recovery_required(tmp_path) -> None:
    order = []
    actuation = _Actuation(order)
    actuation.fence_for_safe_stop = lambda **_values: None
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=actuation,
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "fence-missing.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["safe_stop_status"] == "recovery_required"
    assert not any(item.startswith("a_zero:") for item in order)
    assert not any(item.startswith("selector:") for item in order)


def test_a_zero_owner_exception_is_recovery_required_and_never_routes_selector(
    tmp_path,
) -> None:
    order = []
    flow = _Flow(order)

    def fail_a_zero(*_args, **_kwargs):
        order.append("a_zero_exception")
        raise RuntimeError("synthetic serial owner failure")

    flow.zero_a_for_safe_stop = fail_a_zero
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=flow,
        retry_limit=0,
        record_path=tmp_path / "a-zero-exception.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["safe_stop_status"] == "recovery_required"
    assert "serial owner failure" in event["error"]
    assert not any(item.startswith("selector:") for item in order)


def test_selector_owner_exception_is_recovery_required_not_safe_stop(tmp_path) -> None:
    order = []
    actuation = _Actuation(order)

    def fail_selector(*_args, **_kwargs):
        order.append("selector_exception")
        raise RuntimeError("synthetic selector owner failure")

    actuation.route_selector_safe = fail_selector
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=actuation,
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "selector-exception.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["safe_stop_status"] == "recovery_required"
    assert "selector owner failure" in event["error"]


@pytest.mark.parametrize(
    ("flow_kwargs", "expected_error"),
    [
        ({"lease_released": False}, "device lease"),
        ({"serial_stopped": False}, "serial owner"),
    ],
)
def test_resource_handoff_failure_downgrades_safe_stop_plan(
    tmp_path,
    flow_kwargs,
    expected_error,
) -> None:
    order = []
    service = _service(
        state=_state(),
        worker=_Hardware(order),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order, **flow_kwargs),
        retry_limit=0,
        record_path=tmp_path / "handoff-failed.json",
    )

    event = service.shutdown(source="fault")

    assert event["result"] == "recovery_required"
    assert event["safe_stop_status"] == "recovery_required"
    assert event["valves_closed"] is False
    assert expected_error in event["error"]
