from __future__ import annotations

from app.models import AppState
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
    def __init__(self, order: list[str], *, close=True, stopped=True, fallback=True) -> None:
        self.order = order
        self.close = close
        self.stopped = stopped
        self.fallback = fallback

    def emergency_close_all(self, timeout_ms: int) -> bool:
        self.order.append(f"emergency:{timeout_ms}")
        return self.close

    def shutdown(self, timeout_ms: int) -> bool:
        self.order.append(f"actuation_stop:{timeout_ms}")
        return self.stopped

    def fallback_close_all_after_handoff(self) -> bool:
        self.order.append("fallback")
        return self.fallback


class _Flow:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def shutdown(self, timeout_ms: int) -> bool:
        self.order.append(f"serial_stop:{timeout_ms}")
        return True


def _state() -> AppState:
    state = AppState.from_config({"low_flow_threshold": 0.2, "safety_state": "SAFE"})
    state.hardware_ready = True
    state.telemetry.connected = True
    return state


def test_shutdown_orders_close_handoff_ai_then_serial(tmp_path, caplog) -> None:
    order = []
    service = ShutdownService(
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
        "emergency:123",
        "actuation_stop:321",
        "heaters",
        "flush",
        "ai_release",
        "serial_stop:321",
    ]
    assert "Shutdown guard blocked" not in caplog.text


def test_shutdown_fallback_only_after_do_owner_handoff(tmp_path) -> None:
    order = []
    service = ShutdownService(
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
    service = ShutdownService(
        state=state,
        worker=_Hardware(order),
        actuation_worker=_Actuation(order, close=False, stopped=False),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "stuck.json",
    )

    event = service.shutdown(source="test")

    assert event["result"] == "unsafe"
    assert "fallback" not in order
    assert "DO ownership" in event["error"]
    assert state.hardware_ready is False


def test_shutdown_is_unsafe_when_ai_worker_does_not_stop(tmp_path) -> None:
    order = []
    service = ShutdownService(
        state=_state(),
        worker=_Hardware(order, ai_released=False),
        actuation_worker=_Actuation(order),
        flow_worker=_Flow(order),
        retry_limit=0,
        record_path=tmp_path / "ai-stuck.json",
    )

    event = service.shutdown(source="test")

    assert event["result"] == "unsafe"
    assert event["valves_closed"] is True
    assert "AI" in event["error"]


def test_shutdown_receipts_and_owner_release_continue_after_log_failure(
    tmp_path,
) -> None:
    order = []
    service = ShutdownService(
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
        "emergency:123",
        "actuation_stop:321",
        "heaters",
        "flush",
        "ai_release",
        "serial_stop:321",
    ]
