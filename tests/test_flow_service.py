from __future__ import annotations

import pytest

from app.services.flow_service import FlowService


class DummyHal:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, bool]] = []

    def set_flow(self, channel: str, value: float, *, comp: bool = False) -> bool:
        self.calls.append((channel, float(value), bool(comp)))
        return True


def test_master_valve_failure_does_not_block_mfc_setpoint():
    hal = DummyHal()
    master_calls: list[tuple[str | None, str, bool]] = []

    def master_writer(*, device: str | None, line: str, state: bool) -> bool:
        master_calls.append((device, line, state))
        return False  # simulate relay failure

    service = FlowService(hal, master_target="Dev1/P1.0", master_writer=master_writer)

    result = service.apply_rest(a_target=1.0, b_target=2.0, c_target=3.0)

    assert result.success is True
    assert master_calls == []
    assert hal.calls == [
        ("B", 2.0, False),
        ("C", 3.0, False),
        ("A", 4.0, True),
    ]


def test_mfc_setpoint_does_not_switch_master_valve():
    hal = DummyHal()
    master_calls: list[tuple[str | None, str, bool]] = []

    def master_writer(*, device: str | None, line: str, state: bool) -> bool:
        master_calls.append((device, line, state))
        return True

    service = FlowService(hal, master_target="Dev1/P1.0", master_writer=master_writer)

    result = service.apply_rest(a_target=1.0, b_target=2.0, c_target=3.0)

    assert result.success is True
    assert master_calls == []
    # Flow writes仍按顺序执行
    assert hal.calls[0][0] == "B" and hal.calls[1][0] == "C" and hal.calls[2][0] == "A"


def test_partial_failure_rolls_back_previous_channels():
    class FailingHal(DummyHal):
        def set_flow(self, channel: str, value: float, *, comp: bool = False) -> bool:
            self.calls.append((channel, float(value), bool(comp)))
            if channel == "C":
                return False
            return True

    hal = FailingHal()
    service = FlowService(hal)

    result = service.apply_rest(a_target=1.0, b_target=2.0, c_target=3.0)

    assert result.success is False
    # Calls: B success, C fails, rollback B -> 0.0
    assert hal.calls == [
        ("B", 2.0, False),
        ("C", 3.0, False),
        ("B", 0.0, False),
    ]


def test_apply_zero_sets_all_channels_to_zero_without_comp():
    hal = DummyHal()
    service = FlowService(hal)

    result = service.apply_zero()

    assert result.success is True
    assert result.message == "流量已清零"
    assert hal.calls == [
        ("B", 0.0, False),
        ("C", 0.0, False),
        ("A", 0.0, False),
    ]
