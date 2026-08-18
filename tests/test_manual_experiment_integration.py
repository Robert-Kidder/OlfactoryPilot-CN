from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, build_application
from app.models import (
    AppState,
    DeviceLeaseKind,
    ManualExperimentIntent,
    ManualExperimentStatus,
    ManualSupplyIntent,
)
from app.services import MockHAL
from app.workers import HardwareWorker


class FakeClock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _controller(tmp_path: Path) -> tuple[MainController, FakeClock]:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    config["_local_config_path"] = str(tmp_path / "local_config.json")
    state = AppState.from_config(config)
    state.simulation_mode = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.hardware_ready = True
    state.flow_setpoints_ready = False
    controller = MainController(
        state,
        HardwareWorker(hal=MockHAL(), simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    clock = FakeClock()
    controller.actuation_worker._clock_ns = clock
    return controller, clock


def _intent(*, duration_ns: int = 100) -> ManualExperimentIntent:
    return ManualExperimentIntent(
        external_ports=(2, 4),
        total_sccm=1000,
        sample_a_sccm=250,
        vacuum_c_sccm=100,
        duration_ns=duration_ns,
    )


def test_mock_controller_runs_manual_owner_and_releases_matching_lease(tmp_path) -> None:
    controller, clock = _controller(tmp_path)

    assert controller.handle_manual_release_requested(_intent())
    assert controller.actuation_worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    assert not controller.handle_manual_release_requested(_intent())

    clock.value = controller.actuation_worker.manual_snapshot.deadline_ns
    controller._drain_actuation_if_not_running()

    snapshot = controller.actuation_worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.COMPLETED
    assert snapshot.flow_zero_confirmed
    assert snapshot.selector_compensation_confirmed
    assert snapshot.supply_restored
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_controller_rejects_real_mode_before_any_manual_lease(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    controller.simulation_mode = False

    assert not controller.handle_manual_release_requested(_intent())
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert controller.actuation_worker.manual_snapshot.status is ManualExperimentStatus.IDLE


def test_mock_supply_uses_a_zero_compensation_gate_before_restoring_setpoints(
    tmp_path,
) -> None:
    controller, _ = _controller(tmp_path)

    assert controller.handle_manual_supply_requested(
        ManualSupplyIntent(
            enabled=True,
            total_sccm=1000,
            sample_a_sccm=250,
            vacuum_c_sccm=100,
        )
    )

    snapshot = controller.actuation_worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.COMPLETED
    assert snapshot.selected_external_ports == ()
    assert snapshot.flow_zero_confirmed
    assert snapshot.selector_compensation_confirmed
    assert snapshot.supply_restored
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_controller_stop_manual_is_fail_closed(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    assert controller.handle_manual_release_requested(_intent(duration_ns=10_000))

    assert controller.handle_manual_stop_requested()
    controller._drain_actuation_if_not_running()

    assert (
        controller.actuation_worker.manual_snapshot.status
        is ManualExperimentStatus.RECOVERY_REQUIRED
    )
    assert controller.actuation_worker._background_safe_stop_plan is not None


def test_hardware_profile_controller_gate_revision_and_rollback(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    profile = controller.state.hardware_profile
    assert profile is not None
    candidate = replace(profile, profile_name="Mock 候选方案")

    assert controller.handle_hardware_profile_save_requested(candidate, 0)
    assert controller.state.hardware_profile.profile_name == "Mock 候选方案"
    assert not controller.handle_hardware_profile_save_requested(profile, 0)
    assert controller.state.hardware_profile.profile_name == "Mock 候选方案"
    assert controller.handle_hardware_profile_rollback_requested(1)
    assert controller.state.hardware_profile.profile_name == profile.profile_name


def test_hardware_profile_save_is_blocked_while_connected(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    profile = controller.state.hardware_profile

    assert not controller.handle_hardware_profile_save_requested(profile, 0)
    assert not (tmp_path / "local_config.json").exists()


def test_product_navigation_exposes_v3_and_hides_legacy_entries(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]

    assert labels == ["概览", "文件", "手动实验 V3", "硬件设置", "清洗"]
    assert "预检" not in labels
    assert "校准" not in labels
    assert "协议" not in labels
