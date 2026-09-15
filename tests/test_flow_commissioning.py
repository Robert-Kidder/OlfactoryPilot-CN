from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app.controllers import MainController
from app.models import (
    AppState,
    CommissioningMonitorStatus,
    DeviceLeaseKind,
    FlowSettlingMonitor,
    ManualExperimentStatus,
    ManualSupplyIntent,
    RealSupplyPolicy,
)
from app.services import FlowChannelReadback, FlowReadbackSnapshot, MockHAL
from app.services.authorized_hal import AuthorizedHAL
from app.services.flow_service import FlowApplyResult, FlowService
from app.workers import FlowWorker, HardwareWorker
from app.workers.flow_worker import FlowCommand


def _policy(**changes) -> RealSupplyPolicy:
    raw = {
        "real_supply_policy": {
            "enabled": True,
            "device_capacities_sccm": {"A": 5000, "B": None, "C": None},
            "requested_setpoint_tolerance_sccm": 1e-9,
            "accepted_readback_tolerance_sccm": 1.0,
            "active_mass_flow_tolerance_sccm": 25.0,
            "zero_mass_flow_tolerance_sccm": 5.0,
            "required_consecutive_samples": 6,
            "observation_window_s": 1.0,
            "maximum_settling_deadline_s": 5.0,
            "maximum_nonzero_hold_duration_s": 15.0,
        }
    }
    raw["real_supply_policy"].update(changes)
    return RealSupplyPolicy.from_config(raw)


def _snapshot(
    monotonic_ns: int,
    *,
    a_setpoint: float = 500.0,
    a_flow: float = 500.0,
    b_flow: float = 0.0,
    c_flow: float = 0.0,
    gas: str = "Air",
    fresh: bool = True,
) -> FlowReadbackSnapshot:
    readings = []
    for channel, unit, setpoint, mass_flow in (
        ("A", "a", a_setpoint, a_flow),
        ("B", "b", 0.0, b_flow),
        ("C", "c", 0.0, c_flow),
    ):
        readings.append(
            FlowChannelReadback(
                channel=channel,
                unit_id=unit,
                setpoint_sccm=setpoint,
                mass_flow_sccm=mass_flow,
                gas=gas,
                wall_timestamp=1.0,
                monotonic_ns=monotonic_ns,
                raw_frame=f"{unit} poll",
                fresh=fresh,
            )
        )
    return FlowReadbackSnapshot(
        readings=tuple(readings),
        wall_timestamp=1.0,
        monotonic_ns=monotonic_ns,
        fresh=fresh,
    )


def test_real_supply_policy_defaults_are_disabled_and_frozen() -> None:
    policy = RealSupplyPolicy.from_config({})

    assert policy.enabled is False
    assert policy.requested_setpoint_tolerance_sccm == 1e-9
    assert policy.accepted_readback_tolerance_sccm == 1.0
    assert policy.active_mass_flow_tolerance_sccm == 25.0
    assert policy.zero_mass_flow_tolerance_sccm == 5.0
    assert policy.required_consecutive_samples == 6
    assert policy.observation_window_s == 1.0
    assert policy.maximum_settling_deadline_s == 5.0
    assert policy.maximum_nonzero_hold_duration_s == 15.0
    with pytest.raises(FrozenInstanceError):
        policy.enabled = True  # type: ignore[misc]


def test_capacity_gate_uses_a_plus_c_and_unknown_channels_only_allow_zero() -> None:
    policy = _policy()

    assert policy.controller_targets(
        sample_a_sccm=500,
        main_b_sccm=0,
        vacuum_c_sccm=0,
    ) == (500.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="A controller target.*commissioning 批准上限"):
        policy.controller_targets(
            sample_a_sccm=500.001,
            main_b_sccm=0,
            vacuum_c_sccm=0,
        )
    with pytest.raises(ValueError, match="至少需要一个已批准的非零"):
        policy.controller_targets(
            sample_a_sccm=0,
            main_b_sccm=0,
            vacuum_c_sccm=0,
        )
    with pytest.raises(ValueError, match="B 设备容量未知"):
        policy.controller_targets(
            sample_a_sccm=500,
            main_b_sccm=1,
            vacuum_c_sccm=0,
        )
    with pytest.raises(ValueError, match="C 设备容量未知"):
        policy.controller_targets(
            sample_a_sccm=500,
            main_b_sccm=0,
            vacuum_c_sccm=1,
        )
    with pytest.raises(ValueError, match="B 设备容量未知"):
        policy.controller_targets(
            sample_a_sccm=1,
            main_b_sccm=5e-10,
            vacuum_c_sccm=0,
        )
    with pytest.raises(ValueError, match="A controller target.*commissioning 批准上限"):
        policy.controller_targets(
            sample_a_sccm=500.0 + 5e-10,
            main_b_sccm=0,
            vacuum_c_sccm=0,
        )
    capacity_limited = replace(
        policy,
        capacities=replace(policy.capacities, a_sccm=100.0),
        approved_maxima=replace(policy.approved_maxima, a_sccm=100.0),
    )
    with pytest.raises(ValueError, match="A controller target.*设备容量"):
        capacity_limited.controller_targets(
            sample_a_sccm=100.0 + 5e-10,
            main_b_sccm=0,
            vacuum_c_sccm=0,
        )
    known_b = replace(policy, capacities=replace(policy.capacities, b_sccm=1000))
    with pytest.raises(ValueError, match="B controller target.*commissioning 批准上限"):
        known_b.controller_targets(
            sample_a_sccm=500,
            main_b_sccm=1,
            vacuum_c_sccm=0,
        )
    known_c = replace(
        policy,
        capacities=replace(policy.capacities, c_sccm=1000),
        approved_maxima=replace(
            policy.approved_maxima,
            a_sccm=5000,
            c_sccm=1000,
        ),
    )
    with pytest.raises(ValueError, match="A controller target"):
        known_c.controller_targets(
            sample_a_sccm=4500,
            main_b_sccm=0,
            vacuum_c_sccm=501,
        )


def test_settling_requires_six_fresh_samples_spanning_one_second() -> None:
    first_tx = 1_000_000_000
    monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="real-supply-1",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )

    assert monitor.evaluate(_snapshot(first_tx + 1, fresh=False)) is None
    for index in range(5):
        assert monitor.evaluate(_snapshot(first_tx + 1 + index * 200_000_000)) is None
    result = monitor.evaluate(_snapshot(first_tx + 1_000_000_001))

    assert result is not None
    assert result.status is CommissioningMonitorStatus.STABLE
    assert result.consecutive_samples == 6


def test_invalid_freshness_or_order_resets_consecutive_window() -> None:
    first_tx = 1_000_000_000
    monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="real-supply-order-reset",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )
    for offset in (1, 200_000_001, 400_000_001):
        assert monitor.evaluate(_snapshot(first_tx + offset)) is None

    # Duplicate and pre-TX evidence each invalidate the entire accumulated run.
    assert monitor.evaluate(_snapshot(first_tx + 400_000_001)) is None
    assert monitor.evaluate(_snapshot(first_tx - 1)) is None
    for offset in (600_000_001, 800_000_001, 1_000_000_001, 1_200_000_001, 1_400_000_001):
        assert monitor.evaluate(_snapshot(first_tx + offset)) is None
    result = monitor.evaluate(_snapshot(first_tx + 1_600_000_001))

    assert result is not None and result.stable
    assert result.consecutive_samples == 6


def test_settling_rejects_non_air_gas_and_resets_valid_window() -> None:
    first_tx = 1_000_000_000
    monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="real-supply-gas",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )
    for offset in (1, 200_000_001, 400_000_001):
        assert monitor.evaluate(_snapshot(first_tx + offset)) is None
    assert monitor.evaluate(_snapshot(first_tx + 600_000_001, gas="N2")) is None
    for offset in (800_000_001, 1_000_000_001, 1_200_000_001, 1_400_000_001, 1_600_000_001):
        assert monitor.evaluate(_snapshot(first_tx + offset)) is None
    assert monitor.evaluate(_snapshot(first_tx + 1_800_000_001)).stable


@pytest.mark.parametrize(
    "config",
    [
        {"real_supply_policy": []},
        {"real_supply_policy": {"device_capacities_sccm": []}},
        {"real_supply_policy": {"commissioning_approved_maxima_sccm": ""}},
    ],
)
def test_policy_rejects_falsey_malformed_mappings(config) -> None:
    with pytest.raises(ValueError, match="必须是对象"):
        RealSupplyPolicy.from_config(config)


def test_policy_freezes_expected_gas_as_air() -> None:
    assert _policy().expected_gas == "Air"
    with pytest.raises(ValueError, match="expected_gas.*Air"):
        _policy(expected_gas="N2")


def test_settling_boundaries_and_hold_timeout_are_fail_closed_once() -> None:
    first_tx = 2_000_000_000
    monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="real-supply-2",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )
    for index in range(6):
        result = monitor.evaluate(
            _snapshot(
                first_tx + 1 + index * 200_000_000,
                a_setpoint=501.0,
                a_flow=525.0,
                b_flow=5.0,
                c_flow=-5.0,
            )
        )
    assert result is not None and result.stable

    failed = monitor.evaluate(
        _snapshot(first_tx + 15_000_000_000),
    )
    assert failed is not None
    assert failed.status is CommissioningMonitorStatus.FAILED
    assert "hold duration" in failed.reason
    assert monitor.evaluate(_snapshot(first_tx + 15_200_000_000)) is None


@pytest.mark.parametrize(
    "out_of_band_flow",
    [
        {"a_flow": 525.001},
        {"b_flow": 5.001},
        {"c_flow": -5.001},
    ],
)
def test_settling_out_of_band_samples_never_confirm_and_deadline_fails(
    out_of_band_flow,
) -> None:
    first_tx = 3_000_000_000
    monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="real-supply-3",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )
    for index in range(8):
        assert monitor.evaluate(
            _snapshot(first_tx + 1 + index * 400_000_000, **out_of_band_flow)
        ) is None
    failed = monitor.evaluate(
        _snapshot(first_tx + 5_000_000_000, **out_of_band_flow)
    )

    assert failed is not None
    assert failed.status is CommissioningMonitorStatus.FAILED
    assert "settling deadline" in failed.reason


def test_real_controller_capacity_gate_precedes_single_authoritative_plan(
    tmp_path,
    monkeypatch,
) -> None:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    config["real_supply_policy"]["enabled"] = True
    config["_local_config_path"] = str(tmp_path / "local_config.json")
    state = AppState.from_config(config)
    state.simulation_mode = False
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.hardware_ready = True
    hal = MockHAL()
    controller = MainController(
        state,
        HardwareWorker(hal=hal, simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    submitted = []
    monkeypatch.setattr(
        controller.actuation_worker,
        "post_manual_start",
        lambda plan, *, lease_token: submitted.append((plan, lease_token)) or True,
    )

    assert not controller.handle_manual_supply_requested(
        ManualSupplyIntent(True, 500, 1, 0)
    )
    assert not controller.handle_manual_supply_requested(
        ManualSupplyIntent(True, 500.001, 0, 0)
    )
    assert not controller.handle_manual_supply_requested(
        ManualSupplyIntent(True, 0, 0, 0)
    )
    assert submitted == []
    assert hal.flow_commands == []

    assert controller.handle_manual_supply_requested(
        ManualSupplyIntent(True, 500, 0, 0)
    )
    assert len(submitted) == 1
    assert submitted[0][0].supply_only
    assert submitted[0][0].requires_commissioning_settling
    assert hal.flow_commands == []


def test_accepted_readback_mismatch_blocks_settling_monitor_and_requires_recovery() -> None:
    policy = _policy()
    hal = MockHAL()
    worker = FlowWorker(FlowService(hal))
    token = worker.acquire_lease(
        DeviceLeaseKind.MANUAL,
        operation_id="real-supply-readback-mismatch",
        generation=1,
    )
    assert token is not None
    assert worker.authorize_real_supply(
        operation_id=token.operation_id,
        generation=token.generation,
        targets_sccm=(500.0, 0.0, 0.0),
        policy=policy,
    )
    applied = FlowService(hal).apply_rest(
        a_target=500.0,
        b_target=0.0,
        c_target=0.0,
    )
    mismatched = replace(applied, a_setpoint_readback_sccm=501.001)

    result = worker._apply_real_supply_receipt_gate(
        FlowCommand(
            command_id="flow-readback-mismatch",
            execution_epoch=0,
            sequence=1,
            mode="manual_restore_supply",
            a=500.0,
            b=0.0,
            c=0.0,
            source="manual:experiment",
            operation_id=token.operation_id,
            generation=token.generation,
            lease_token=token.token,
        ),
        mismatched,
    )

    assert result.success is False
    assert result.error == "accepted_readback_mismatch"
    assert result.recovery_required is True
    assert worker._commissioning_monitor is None


def test_preclose_readbacks_gate_selector_without_consuming_restore_authorization() -> None:
    policy = _policy()
    worker = FlowWorker(FlowService(MockHAL()))
    token = worker.acquire_manual_lease("real-supply-preclose", 1)
    assert token is not None
    assert worker.authorize_real_supply(
        operation_id=token.operation_id,
        generation=token.generation,
        targets_sccm=(500.0, 0.0, 0.0),
        policy=policy,
    )
    command = FlowCommand(
        "preclose",
        0,
        1,
        "manual_post_close_a_zero",
        0.0,
        0.0,
        0.0,
        "manual:experiment",
        operation_id=token.operation_id,
        generation=token.generation,
        lease_token=token.token,
    )
    base = FlowApplyResult(
        True,
        "ok",
        0.0,
        0.0,
        0.0,
        0.0,
        a_setpoint_readback_sccm=0.0,
        b_setpoint_readback_sccm=1.001,
        c_setpoint_readback_sccm=0.0,
    )

    rejected = worker._apply_real_supply_receipt_gate(command, base)
    assert not rejected.success
    assert rejected.error == "pre_close_readback_mismatch"
    assert worker._real_supply_authorization is not None

    accepted = worker._apply_real_supply_receipt_gate(
        command,
        replace(base, b_setpoint_readback_sccm=1.0),
    )
    assert accepted.success
    assert worker._real_supply_authorization is not None


def test_duplicate_restore_consumes_one_authorization_and_has_no_second_tx() -> None:
    hal = MockHAL()
    worker = FlowWorker(FlowService(hal))
    token = worker.acquire_manual_lease("real-supply-once", 1)
    assert token is not None
    assert worker.authorize_real_supply(
        operation_id=token.operation_id,
        generation=token.generation,
        targets_sccm=(500.0, 0.0, 0.0),
        policy=_policy(),
    )
    first = FlowCommand(
        "restore-1",
        0,
        1,
        "manual_restore_supply",
        500.0,
        0.0,
        0.0,
        "manual:experiment",
        operation_id=token.operation_id,
        generation=token.generation,
        lease_token=token.token,
    )
    second = replace(first, command_id="restore-2", sequence=2)
    results = []
    worker.result_ready.connect(results.append)

    assert worker.submit(first)
    assert worker.submit(second)
    assert worker.process_ready() == 2

    assert len(hal.flow_commands) == 3
    assert results[0].result.success
    assert not results[1].result.success
    assert "已消费" in results[1].result.message

    worker.cancel_real_supply_authorization(token.operation_id)
    assert worker.release_lease(token)
    next_token = worker.acquire_manual_lease("real-supply-next", 2)
    assert next_token is not None
    assert worker.authorize_real_supply(
        operation_id=next_token.operation_id,
        generation=next_token.generation,
        targets_sccm=(500.0, 0.0, 0.0),
        policy=_policy(),
    )


def test_cancelled_restore_receipt_cannot_install_a_monitor() -> None:
    hal = MockHAL()
    worker = FlowWorker(FlowService(hal))
    token = worker.acquire_manual_lease("real-supply-cancel", 1)
    assert token is not None
    assert worker.authorize_real_supply(
        operation_id=token.operation_id,
        generation=token.generation,
        targets_sccm=(500.0, 0.0, 0.0),
        policy=_policy(),
    )
    command = FlowCommand(
        "restore-cancelled",
        0,
        1,
        "manual_restore_supply",
        500.0,
        0.0,
        0.0,
        "manual:experiment",
        operation_id=token.operation_id,
        generation=token.generation,
        lease_token=token.token,
    )
    result = FlowService(hal).apply_flows(
        a_target=500,
        b_target=0,
        c_target=0,
        mode="manual_restore_supply",
    )

    worker.cancel_real_supply_authorization(token.operation_id)
    rejected = worker._apply_real_supply_receipt_gate(command, result)

    assert not rejected.success
    assert rejected.recovery_required
    assert worker._commissioning_monitor is None


def test_monitor_polls_without_sink_before_queued_commands_and_audits_sample(
    caplog,
) -> None:
    class SnapshotHAL(MockHAL):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0
            self.commands_at_poll: list[int] = []

        def read_flow_snapshot(self) -> FlowReadbackSnapshot:
            self.polls += 1
            self.commands_at_poll.append(len(self.flow_commands))
            return _snapshot(time.perf_counter_ns())

    hal = SnapshotHAL()
    worker = FlowWorker(FlowService(hal), airflow_poll_interval_s=0.02)
    first_tx = time.perf_counter_ns() - 6_000_000_000
    worker._commissioning_monitor = FlowSettlingMonitor(
        policy=_policy(),
        operation_id="poll-without-sink",
        targets_sccm=(500, 0, 0),
        first_nonzero_tx_monotonic_ns=first_tx,
    )
    order = []
    worker.commissioning_result_ready.connect(
        lambda _result: order.append("result"),
        Qt.ConnectionType.DirectConnection,
    )
    worker.flow_snapshot_ready.connect(
        lambda _snapshot: order.append("snapshot"),
        Qt.ConnectionType.DirectConnection,
    )
    assert worker.submit(FlowCommand("queued", 0, 1, "rest", 1, 0, 0, "manual"))

    with caplog.at_level(logging.INFO, logger="app.workers.flow_worker"):
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while not order and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        finally:
            assert worker.shutdown(1000)

    assert hal.polls >= 1
    assert hal.commands_at_poll[0] == 0
    assert order[:2] == ["result", "snapshot"]
    audit = next(record.message for record in caplog.records if "commissioning_sample" in record.message)
    assert "raw_frame" in audit and "setpoint_sccm" in audit
    assert "mass_flow_sccm" in audit and "monotonic_ns" in audit


def test_authorized_hal_forwards_read_only_flow_session_contract() -> None:
    delegate = MockHAL()
    delegate.set_flow("A", 500.0)
    proxy = AuthorizedHAL(delegate, [])

    assert tuple(proxy.read_flow_snapshot().by_channel) == ("A", "B", "C")
    assert proxy.serial_desynchronized is delegate.serial_desynchronized
    assert proxy.last_setpoint_tx_monotonic_ns("A") == delegate.last_setpoint_tx_monotonic_ns("A")
    assert proxy.remaining == ()


def test_flow_worker_publishes_three_channel_snapshot_from_its_owner_thread() -> None:
    class SnapshotHAL(MockHAL):
        def __init__(self) -> None:
            super().__init__(base_flow_sccm=500)
            self.snapshot_thread_ids: list[int] = []
            self.legacy_reads = 0

        def read_flow(self) -> float:
            self.legacy_reads += 1
            return super().read_flow()

        def read_flow_snapshot(self) -> FlowReadbackSnapshot:
            self.snapshot_thread_ids.append(threading.get_ident())
            return super().read_flow_snapshot()

    hal = SnapshotHAL()
    worker = FlowWorker(FlowService(hal), airflow_poll_interval_s=0.02)
    received = []
    ready = threading.Event()
    worker.flow_snapshot_ready.connect(
        lambda snapshot: (received.append(snapshot), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )
    worker.set_airflow_sink(lambda *_args: None)

    worker.start()
    try:
        assert ready.wait(1.0)
    finally:
        assert worker.shutdown(1000)

    assert tuple(item.channel for item in received[0].readings) == ("A", "B", "C")
    assert hal.legacy_reads == 0
    assert hal.snapshot_thread_ids
    assert set(hal.snapshot_thread_ids) == {hal.snapshot_thread_ids[0]}
    assert hal.snapshot_thread_ids[0] != threading.get_ident()


def test_real_supply_waits_for_settling_and_hold_timeout_enters_safe_stop(
    tmp_path,
) -> None:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    config["real_supply_policy"]["enabled"] = True
    config["_local_config_path"] = str(tmp_path / "local_config.json")
    state = AppState.from_config(config)
    state.simulation_mode = False
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.hardware_ready = True
    hal = MockHAL(base_flow_sccm=0)
    controller = MainController(
        state,
        HardwareWorker(hal=hal, simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    monitor_results = []
    safe_stop_modes = []
    controller.flow_worker.commissioning_result_ready.connect(monitor_results.append)
    controller.flow_worker.result_ready.connect(
        lambda wrapped: (
            safe_stop_modes.append(wrapped.command.mode)
            if wrapped.command.source == "safety:safe-stop"
            else None
        )
    )

    assert controller.handle_manual_supply_requested(
        ManualSupplyIntent(True, 500, 0, 0)
    )
    snapshot = controller.actuation_worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.RESTORING_SUPPLY
    assert snapshot.supply_transitioning
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    monitor = controller.flow_worker._commissioning_monitor
    assert monitor is not None
    first_tx = monitor.first_nonzero_tx_monotonic_ns

    for index in range(6):
        controller.flow_worker._evaluate_commissioning(
            _snapshot(first_tx + 1 + index * 200_000_000)
        )
        controller._drain_actuation_if_not_running()

    snapshot = controller.actuation_worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.RESTORING_SUPPLY
    assert snapshot.supply_enabled
    assert not snapshot.supply_transitioning
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    assert [result.status for result in monitor_results] == [
        CommissioningMonitorStatus.STABLE
    ]

    controller.flow_worker._evaluate_commissioning(
        _snapshot(first_tx + 15_000_000_000),
        now_ns=first_tx + 15_000_000_000,
    )
    controller._drain_actuation_if_not_running()

    assert controller.actuation_worker.manual_snapshot.status is (
        ManualExperimentStatus.RECOVERY_REQUIRED
    )
    assert [result.status for result in monitor_results] == [
        CommissioningMonitorStatus.STABLE,
        CommissioningMonitorStatus.FAILED,
    ]
    assert controller.flow_worker._commissioning_monitor is None
    assert safe_stop_modes == ["safe_stop_a_zero", "zero"]

    controller.flow_worker._evaluate_commissioning(
        _snapshot(first_tx + 16_000_000_000),
        now_ns=first_tx + 16_000_000_000,
    )
    controller._drain_actuation_if_not_running()
    assert safe_stop_modes == ["safe_stop_a_zero", "zero"]
