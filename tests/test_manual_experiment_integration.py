from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path

import pytest
from qfluentwidgets import FluentWindow

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, build_application
from app.models import (
    AppState,
    DeviceLeaseKind,
    HardwareVerificationPhase,
    ManualExperimentIntent,
    ManualExperimentStatus,
    ManualSupplyIntent,
    ProtocolExecutionReadiness,
    VerificationStatus,
    normalize_digital_target,
)
from app.services import MockHAL
from app.views import MainWindow
from app.views.hardware_settings_view import HardwareSettingsView
from app.workers import FlowCommand, HardwareWorker


class FakeClock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value = max(self.value, time.perf_counter_ns())
        return self.value


def _controller(
    tmp_path: Path,
    *,
    verification_duration=0,
    pending_port: int | None = None,
) -> tuple[MainController, FakeClock]:
    config = json.loads(Path("config/default_config.json").read_text(encoding="utf-8"))
    if pending_port is not None:
        config["hardware_profile"]["channels"][pending_port - 1]["verification"] = {
            "status": VerificationStatus.PENDING.value,
        }
    config["_local_config_path"] = str(tmp_path / "local_config.json")
    config["simulation_verification_duration_s"] = verification_duration
    state = AppState.from_config(config)
    state.simulation_mode = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.hardware_ready = True
    state.flow_setpoints_ready = False
    clock = FakeClock(time.perf_counter_ns())
    controller = MainController(
        state,
        HardwareWorker(hal=MockHAL(monotonic_ns_clock=clock), simulation=True),
        config=config,
        allow_test_actuation_bridge=True,
    )
    controller.actuation_worker._clock_ns = clock
    return controller, clock


def _intent(
    *,
    external_ports: tuple[int, ...] = (2, 4),
    duration_ns: int = 1_000_000_000,
) -> ManualExperimentIntent:
    return ManualExperimentIntent(
        external_ports=external_ports,
        sample_a_sccm=500,
        main_b_sccm=1000,
        vacuum_c_sccm=500,
        duration_ns=duration_ns,
    )


def _publish_fresh_safe_after_restore(
    controller: MainController,
    *,
    drain=None,
) -> None:
    timestamp = max(
        time.time(),
        controller.actuation_interlock.read()[1].airflow_sample_timestamp + 0.001,
    )
    controller.actuation_interlock.publish_airflow(
        airflow=250.0,
        timestamp=timestamp,
        hardware_state="SAFE",
    )
    controller.actuation_worker.post_interlock_changed(timestamp=timestamp)
    (drain or controller._drain_actuation_if_not_running)()


def test_mock_controller_runs_manual_owner_and_releases_matching_lease(tmp_path, qtbot) -> None:
    controller, clock = _controller(tmp_path)

    assert controller.handle_manual_release_requested(_intent())
    assert controller.actuation_worker.manual_snapshot.status is ManualExperimentStatus.FLOW_PENDING
    _publish_fresh_safe_after_restore(controller)
    assert controller.actuation_worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    assert not controller.handle_manual_release_requested(_intent())

    clock.value = controller.actuation_worker.manual_snapshot.deadline_ns
    controller._drain_actuation_if_not_running()
    _publish_fresh_safe_after_restore(controller)

    snapshot = controller.actuation_worker.manual_snapshot
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    assert snapshot.status is ManualExperimentStatus.COMPLETED
    assert snapshot.flow_zero_confirmed
    assert snapshot.selector_compensation_confirmed
    assert snapshot.supply_restored
    assert snapshot.supply_enabled
    assert window.manual_experiment_view.snapshot.supply_enabled
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_manual_500_1000_500_uses_exact_three_phase_targets_and_normal_completion(
    tmp_path,
) -> None:
    controller, clock = _controller(tmp_path)
    flow_commands = []
    controller.flow_worker.result_ready.connect(
        lambda wrapped: flow_commands.append(wrapped.command)
    )

    assert controller.handle_manual_release_requested(_intent(external_ports=(2, 4)))
    _publish_fresh_safe_after_restore(controller)
    stimulating = controller.actuation_worker.manual_snapshot
    assert stimulating.status is ManualExperimentStatus.STIMULATING
    assert stimulating.open_confirmed == (2, 4)

    clock.value = stimulating.deadline_ns
    controller._drain_actuation_if_not_running()
    _publish_fresh_safe_after_restore(controller)

    assert [(command.mode, command.a, command.b, command.c) for command in flow_commands] == [
        ("manual_baseline", 1000.0, 1000.0, 500.0),
        ("manual_stimulus", 500.0, 1000.0, 0.0),
        ("manual_post_close_a_zero", 0.0, 1000.0, 500.0),
        ("manual_restore_supply", 1000.0, 1000.0, 500.0),
    ]
    completed = controller.actuation_worker.manual_snapshot
    assert completed.status is ManualExperimentStatus.COMPLETED
    assert completed.close_confirmed == (2, 4)
    assert completed.possibly_open == ()
    assert not completed.recovery_reason
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_supplied_mock_runs_ports_04_06_for_five_seconds_without_protocol_crosstalk(
    tmp_path,
    monkeypatch,
) -> None:
    controller, clock = _controller(tmp_path)
    assert controller.handle_manual_supply_requested(
        ManualSupplyIntent(
            enabled=True,
            sample_a_sccm=500,
            main_b_sccm=1000,
            vacuum_c_sccm=500,
        )
    )
    _publish_fresh_safe_after_restore(controller)
    assert controller.actuation_worker.manual_snapshot.supply_enabled
    original_epoch = controller.actuation_worker.protocol_state.execution_epoch
    original_event_count = len(controller.actuation_worker.protocol_state.events)

    assert controller.handle_manual_release_requested(
        _intent(external_ports=(4, 6), duration_ns=5_000_000_000)
    )
    _publish_fresh_safe_after_restore(controller)
    stimulating = controller.actuation_worker.manual_snapshot
    assert stimulating.status is ManualExperimentStatus.STIMULATING
    assert stimulating.open_confirmed == (4, 6)
    assert stimulating.deadline_ns - stimulating.ready_ns == 5_000_000_000

    completion_events: list[tuple[str, object]] = []
    expected_token = controller._manual_lease_token
    assert expected_token is not None
    controller.actuation_worker.receipt_ready.connect(
        lambda receipt: completion_events.append(("do_receipt", receipt.step_id))
    )
    controller.flow_worker.result_ready.connect(
        lambda wrapped: completion_events.append(("flow_receipt", wrapped.command.mode))
    )
    controller.actuation_worker.manual_snapshot_ready.connect(
        lambda snapshot: (
            completion_events.append(("snapshot", snapshot.status.value))
            if snapshot.status is ManualExperimentStatus.COMPLETED
            else None
        )
    )
    release_lease = controller.flow_worker.release_lease

    def record_exact_manual_release(token):
        assert token == expected_token
        released = release_lease(token)
        assert released
        completion_events.append(("lease_release", token.token))
        return released

    monkeypatch.setattr(controller.flow_worker, "release_lease", record_exact_manual_release)

    clock.value = stimulating.deadline_ns
    controller._drain_actuation_if_not_running()
    _publish_fresh_safe_after_restore(controller)

    completed = controller.actuation_worker.manual_snapshot
    assert completed.status is ManualExperimentStatus.COMPLETED
    assert completed.close_confirmed == (4, 6)
    assert completed.possibly_open == ()
    assert completed.flow_zero_confirmed
    assert completed.selector_compensation_confirmed
    assert completed.supply_restored
    assert completed.supply_enabled
    assert controller.actuation_worker.protocol_state.possibly_open_valves == set()
    assert controller.actuation_worker.protocol_state.execution_epoch == original_epoch
    assert len(controller.actuation_worker.protocol_state.events) == original_event_count
    assert controller.actuation_worker._background_safe_stop_plan is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert completion_events == [
        ("do_receipt", "manual-close:4"),
        ("do_receipt", "manual-close:6"),
        ("flow_receipt", "manual_post_close_a_zero"),
        ("do_receipt", "selector_compensation"),
        ("flow_receipt", "manual_restore_supply"),
        ("snapshot", ManualExperimentStatus.COMPLETED.value),
        ("lease_release", expected_token.token),
    ]


def test_manual_safe_readiness_is_protocol_isolated_across_lifecycle_windows(
    tmp_path,
    monkeypatch,
) -> None:
    controller, clock = _controller(tmp_path)
    worker = controller.actuation_worker
    safe_readiness = ProtocolExecutionReadiness(True, True, True, "SAFE", True)
    baseline = (
        worker.protocol_state.execution_epoch,
        tuple(event for event in worker.protocol_state.events if event.event == "blocked"),
        worker._background_safe_stop_plan,
    )

    def assert_protocol_unchanged() -> None:
        assert (
            worker.protocol_state.execution_epoch,
            tuple(event for event in worker.protocol_state.events if event.event == "blocked"),
            worker._background_safe_stop_plan,
        ) == baseline

    drain = controller._drain_actuation_if_not_running
    post_manual_start = worker.post_manual_start

    def queue_readiness_before_start(plan, *, lease_token):
        worker.post_readiness_update(readiness=safe_readiness)
        return post_manual_start(plan, lease_token=lease_token)

    monkeypatch.setattr(worker, "post_manual_start", queue_readiness_before_start)
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)

    assert controller.handle_manual_release_requested(_intent())
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL
    assert worker._manual_start_pending
    worker.process_ready(max_items=1)
    assert worker.manual_snapshot.status is ManualExperimentStatus.IDLE
    assert worker._manual_start_pending
    assert_protocol_unchanged()

    drain()
    controller.actuation_interlock.publish_airflow(
        airflow=250.0,
        timestamp=time.time(),
        hardware_state="SAFE",
    )
    worker.post_interlock_changed(timestamp=time.time())
    drain()
    assert worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING
    assert_protocol_unchanged()

    worker.post_readiness_update(readiness=safe_readiness)
    drain()
    assert worker.manual_snapshot.status is ManualExperimentStatus.STIMULATING
    assert_protocol_unchanged()

    terminal_results = []
    worker.manual_result_ready.disconnect(controller._handle_manual_result)
    worker.manual_result_ready.connect(terminal_results.append)
    clock.value = worker.manual_snapshot.deadline_ns
    drain()
    _publish_fresh_safe_after_restore(controller, drain=drain)
    assert worker.manual_snapshot.status is ManualExperimentStatus.COMPLETED
    assert terminal_results[-1].completed
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL

    worker.post_readiness_update(readiness=safe_readiness)
    drain()
    assert_protocol_unchanged()
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.MANUAL

    controller._handle_manual_result(terminal_results[-1])
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
            sample_a_sccm=500,
            main_b_sccm=1000,
            vacuum_c_sccm=500,
        )
    )
    _publish_fresh_safe_after_restore(controller)

    snapshot = controller.actuation_worker.manual_snapshot
    assert snapshot.status is ManualExperimentStatus.COMPLETED
    assert snapshot.selected_external_ports == ()
    assert snapshot.flow_zero_confirmed
    assert snapshot.selector_compensation_confirmed
    assert snapshot.supply_restored
    assert snapshot.supply_enabled
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_controller_stop_manual_is_fail_closed(tmp_path, qtbot) -> None:
    controller, _ = _controller(tmp_path)
    assert controller.handle_manual_release_requested(_intent(duration_ns=1_000_000_000))

    assert controller.handle_manual_stop_requested()
    controller._drain_actuation_if_not_running()

    assert (
        controller.actuation_worker.manual_snapshot.status
        is ManualExperimentStatus.RECOVERY_REQUIRED
    )
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    assert controller.actuation_worker._background_safe_stop_plan is not None
    assert not controller.actuation_worker.manual_snapshot.supply_enabled
    assert not window.manual_experiment_view.snapshot.supply_enabled


def test_pending_manual_start_cancel_releases_exact_owner(tmp_path, monkeypatch) -> None:
    controller, _ = _controller(tmp_path)
    monkeypatch.setattr(controller, "_drain_actuation_if_not_running", lambda: None)

    assert controller.handle_manual_release_requested(_intent())
    assert controller._manual_lease_token is not None
    assert controller.handle_manual_stop_requested()
    controller.actuation_worker.process_ready()

    assert controller._manual_lease_token is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert controller.actuation_interlock.read()[1].device_lease == "idle"
    assert controller.actuation_worker.manual_snapshot.status is ManualExperimentStatus.IDLE


def test_controller_publishes_concise_manual_readiness_reason(
    tmp_path,
    qtbot,
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)

    controller.state.telemetry.connected = False
    controller._render_manual_snapshot()
    snapshot = window.manual_experiment_view.snapshot
    assert not snapshot.controls_enabled
    assert not snapshot.can_apply_flow
    assert "当前不可操作" in snapshot.detail_text
    assert "请先连接设备" in snapshot.detail_text
    assert "intent" not in snapshot.detail_text
    assert "lease" not in snapshot.detail_text

    controller.state.telemetry.connected = True
    controller.state.hardware_ready = True
    controller.state.telemetry.safety_state = "SAFE"
    controller._render_manual_snapshot()
    assert window.manual_experiment_view.snapshot.controls_enabled
    assert window.manual_experiment_view.snapshot.can_apply_flow

    token = controller.device_lease.acquire(
        DeviceLeaseKind.PROTOCOL,
        operation_id="readiness-test",
        generation=1,
    )
    assert token is not None
    controller._render_manual_snapshot()
    snapshot = window.manual_experiment_view.snapshot
    assert not snapshot.controls_enabled
    assert not snapshot.can_apply_flow
    assert "设备正在执行其他操作" in snapshot.detail_text
    assert "lease" not in snapshot.detail_text


def test_hardware_profile_controller_gate_revision_and_rollback(tmp_path, qtbot) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    window.hardware_settings_view = HardwareSettingsView()
    qtbot.addWidget(window.hardware_settings_view)
    controller.bind_view(window)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    controller.state.telemetry.safety_state = "SAFE"
    profile = controller.state.hardware_profile
    assert profile is not None
    channels = list(profile.channels)
    channels[1] = replace(channels[1], target="Dev2/P1.1")
    selector = replace(profile.selector, target="Dev2/P1.2")
    candidate = replace(
        profile,
        profile_name="Mock 候选方案",
        channels=tuple(channels),
        selector=selector,
    )

    assert controller.handle_hardware_profile_save_requested(candidate, 0)
    assert window.hardware_settings_view.save_feedback_label.text() == "设置已保存"
    assert "hardware-settings" not in (
        window.manual_experiment_view._notification_coordinator._events
    )
    assert not controller._configuration_restart_required
    assert controller.state.hardware_profile.profile_name == "Mock 候选方案"
    assert controller.state.selector == selector
    assert controller.valve_service.resolve_target(2) == ("Dev2", "P1.1")
    assert controller._current_cleaning_targets()[2] == "Dev2/P1.1"
    assert controller.valve_service.selector == selector
    assert normalize_digital_target(controller.actuation_adapter.selector_target) == normalize_digital_target("Dev2/P1.2")
    assert controller.shutdown_service.selector == selector
    assert controller.flow_service.master_target == "Dev2/P1.2"
    assert controller.session_file_service.master_target == ("Dev2", "P1.2")
    assert controller.config["valve_mapping"]["selector"]["target"] == "Dev2/P1.2"
    assert not window.manual_experiment_view.snapshot.ports[1].available
    assert not controller.handle_hardware_profile_save_requested(profile, 0)
    assert controller.state.hardware_profile.profile_name == "Mock 候选方案"
    assert controller.handle_hardware_profile_rollback_requested(1)
    assert controller.state.hardware_profile.profile_name == profile.profile_name
    assert controller.valve_service.resolve_target(2) == ("Dev1", "P0.1")


def test_profile_save_refreshes_alias_disabled_state_in_manual_and_settings(
    tmp_path, qtbot
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    profile = controller.state.hardware_profile
    channels = list(profile.channels)
    channels[3] = replace(channels[3], display_name="柠檬", enabled=False)

    assert controller.handle_hardware_profile_save_requested(
        replace(profile, channels=tuple(channels)), 0
    )

    manual_port = window.manual_experiment_view.snapshot.ports[3]
    manual_tile = window.manual_experiment_view.port_buttons[4]
    settings = window.hardware_settings_view
    assert manual_port.display_name == "柠檬"
    assert manual_port.available is False
    assert manual_tile.text() == "气口 04\n柠檬"
    assert manual_tile.property("portState") == "disabled"
    assert settings.name_inputs[4].text() == "柠檬"
    assert not settings.enabled_checks[4].isChecked()
    assert settings.overview_buttons[4].text() == "气口 04\n柠檬"
    assert window.manual_experiment_view.profile_revision == 1
    assert settings.draft.revision == 1


def test_mapping_save_publishes_one_revision_to_every_consumer(tmp_path, qtbot) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    profile = controller.state.hardware_profile
    channels = list(profile.channels)
    channels[3] = replace(
        channels[3],
        internal_valve=10,
        target=profile.resolved_target_for(10),
    )

    assert controller.handle_hardware_profile_save_requested(
        replace(profile, channels=tuple(channels)), 0
    )

    store_channel = controller._hardware_profile_store.profile.registry.by_external_port(4)
    state_channel = controller.state.hardware_profile.registry.by_external_port(4)
    registry_channel = controller.state.channel_registry.by_external_port(4)
    settings_channel = window.hardware_settings_view.draft.to_profile().registry.by_external_port(4)
    manual_channel = window.manual_experiment_view._registry.by_external_port(4)
    assert {
        store_channel.internal_valve,
        state_channel.internal_valve,
        registry_channel.internal_valve,
        settings_channel.internal_valve,
        manual_channel.internal_valve,
    } == {10}
    assert store_channel.verification.status is VerificationStatus.MAPPING_CHANGED
    assert controller.valve_service.resolve_target(10) == ("Dev1", "P1.1")
    assert controller._runtime_hardware_profile_revision == 1
    assert window.manual_experiment_view.profile_revision == 1
    assert window.hardware_settings_view.draft.revision == 1


def test_profile_runtime_bind_failure_rolls_disk_and_all_runtime_consumers_back(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    original = controller.state.hardware_profile
    candidate = replace(original, selector=replace(original.selector, target="Dev2/P1.2"))
    real_rebind = controller.session_file_service.rebind_master_target
    calls = 0

    def fail_first_rebind(target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected runtime bind failure")
        return real_rebind(target)

    monkeypatch.setattr(
        controller.session_file_service,
        "rebind_master_target",
        fail_first_rebind,
    )

    assert not controller.handle_hardware_profile_save_requested(candidate, 0)
    assert controller.state.hardware_profile == original
    assert controller.valve_service.selector == original.selector
    assert normalize_digital_target(controller.actuation_adapter.selector_target) == normalize_digital_target(original.selector.target)
    assert controller.shutdown_service.selector == original.selector
    assert controller.flow_service.master_target == original.selector.target
    assert controller.session_file_service.master_target == ("Dev2", "P1.0")
    assert controller._hardware_profile_store.profile == original
    assert controller._hardware_profile_store.revision == 0
    assert not (tmp_path / "local_config.json").exists()


def test_connection_change_latches_restart_before_any_old_hal_access(
    tmp_path, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    original = controller.state.hardware_profile
    candidate = replace(
        original,
        connections=replace(original.connections, serial_port="COM18"),
    )
    calls = []
    monkeypatch.setattr(controller.worker, "request_self_check", lambda: calls.append("self-check"))
    monkeypatch.setattr(controller.worker, "start", lambda *_args: calls.append("start"))

    assert controller.handle_hardware_profile_save_requested(candidate, 0)
    assert controller._configuration_restart_required
    controller.connect_hardware()
    controller.request_self_check()
    controller.reset_hardware()
    assert calls == []
    assert "重启程序后生效" in controller.state.status_message

    restarted, _ = _controller(tmp_path)
    assert restarted.state.hardware_profile.connections.serial_port == "COM18"
    assert not restarted._configuration_restart_required


def test_disk_commit_failure_restores_runtime_without_revision_or_lkg_flip(
    tmp_path, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    store = controller._hardware_profile_store
    original = controller.state.hardware_profile
    candidate = replace(original, profile_name="prepared-only")
    original_lkg = store._last_known_good

    def fail_write(_data):
        raise OSError("disk fault")

    monkeypatch.setattr(store, "_atomic_write", fail_write)
    assert not controller.handle_hardware_profile_save_requested(candidate, 0)
    assert controller.state.hardware_profile == original
    assert store.profile == original
    assert store.revision == 0
    assert store._last_known_good == original_lkg
    assert not (tmp_path / "local_config.json").exists()


def test_compensation_failure_latches_configuration_divergence(
    tmp_path, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    candidate = replace(controller.state.hardware_profile, profile_name="will-fail")

    def fail_bind(_target):
        raise RuntimeError("bind and restore fault")

    monkeypatch.setattr(controller.session_file_service, "rebind_master_target", fail_bind)
    assert not controller.handle_hardware_profile_save_requested(candidate, 0)
    assert controller._configuration_diverged
    assert not controller.handle_manual_release_requested(_intent())
    assert "配置分歧" in controller.state.status_message


def test_hardware_profile_save_is_blocked_while_connected(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    profile = controller.state.hardware_profile

    assert not controller.handle_hardware_profile_save_requested(profile, 0)
    assert not (tmp_path / "local_config.json").exists()


def test_hardware_profile_save_is_blocked_without_safe_confirmation(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    controller.state.telemetry.safety_state = "UNKNOWN"

    assert not controller.handle_hardware_profile_save_requested(
        controller.state.hardware_profile,
        0,
    )
    assert not (tmp_path / "local_config.json").exists()


def test_mock_verification_requires_isolated_correlated_open_close_receipts(
    tmp_path,
    qtbot,
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller.state.telemetry.connected = True
    controller.state.hardware_ready = True
    controller.state.telemetry.safety_state = "SAFE"
    candidate = controller.state.hardware_profile
    controller._render_hardware_profile()

    assert not window.hardware_settings_view.name_inputs[2].isEnabled()
    assert not window.hardware_settings_view.save_button.isEnabled()
    assert window.hardware_settings_view.mock_buttons[2].isEnabled()

    controller.handle_hardware_mock_verify_requested(2, candidate)

    assert controller._hardware_verification_run.phase is (
        HardwareVerificationPhase.AWAITING_CONFIRMATION
    )
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert controller.handle_hardware_verification_result_requested(2, True)

    verified = controller.state.hardware_profile.channels[1]
    assert verified.verification.status is VerificationStatus.MOCK_VERIFIED
    assert verified.verification.fingerprint == verified.mapping_fingerprint
    finished = controller._hardware_verification_snapshot()
    assert finished.phase is HardwareVerificationPhase.FINISHED
    assert finished.result is VerificationStatus.MOCK_VERIFIED
    assert window.hardware_settings_view.snapshot.verification == finished
    assert json.loads((tmp_path / "local_config.json").read_text(encoding="utf-8"))["hardware_profile_revision"] == 1
    assert not window.hardware_settings_view.name_inputs[2].isEnabled()
    coordinator = window.manual_experiment_view._notification_coordinator
    assert "hardware-settings" not in coordinator._events
    assert window.hardware_settings_view.verification_labels[2].text() == "待现场确认"
    assert window.hardware_settings_view.verification_panel.isHidden()
    assert not window.hardware_settings_view.editor_stack.isHidden()


def test_mock_verification_refreshes_simulation_availability_without_restart(
    tmp_path,
    qtbot,
) -> None:
    controller, _ = _controller(tmp_path, pending_port=2)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller._render_hardware_profile()

    assert not window.manual_experiment_view.port_buttons[2].isEnabled()
    assert not controller.state.hardware_profile.registry.by_external_port(2).available

    controller.handle_hardware_mock_verify_requested(
        2,
        controller.state.hardware_profile,
        expected_revision=0,
    )
    assert controller.handle_hardware_verification_result_requested(2, True)

    channel = controller.state.hardware_profile.registry.by_external_port(2)
    assert channel.verification.status is VerificationStatus.MOCK_VERIFIED
    assert not channel.available
    assert window.manual_experiment_view.port_buttons[2].isEnabled()
    assert window.manual_experiment_view.profile_revision == 1


def test_mock_verification_failure_does_not_publish_fingerprint(
    tmp_path,
    qtbot,
    monkeypatch,
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller.state.telemetry.connected = True
    controller.state.hardware_ready = True
    controller.state.telemetry.safety_state = "SAFE"
    candidate = controller.state.hardware_profile
    monkeypatch.setattr(MockHAL, "write_digital", lambda *_args, **_kwargs: False)

    controller.handle_hardware_mock_verify_requested(2, candidate)

    rendered = window.hardware_settings_view.draft.to_profile().channels[1]
    assert rendered == controller.state.hardware_profile.channels[1]
    assert rendered.mapping_fingerprint == candidate.channels[1].mapping_fingerprint
    assert rendered.verification.status is VerificationStatus.FAILED
    assert rendered.verification.fingerprint == rendered.mapping_fingerprint
    assert "验证失败" in window.hardware_settings_view.status_label.text()
    notice = window.manual_experiment_view._notification_coordinator.current
    assert notice is not None
    assert notice.title == "验证失败"
    assert notice.message == "请检查气口后重试。"
    assert not any(
        term in notice.message
        for term in ("映射", "fingerprint", "安全动作", "revision", "写入")
    )
    assert notice.actionable is True
    assert window.hardware_settings_view.verification_panel.isHidden()
    assert not window.hardware_settings_view.editor_stack.isHidden()


def test_mock_verification_stop_persists_incomplete_without_mapping_change(
    tmp_path,
    qtbot,
) -> None:
    controller, _ = _controller(tmp_path)
    controller._hardware_verification_duration_s = 20
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    before = controller.state.hardware_profile.registry.by_external_port(2)

    controller.handle_hardware_mock_verify_requested(
        2,
        controller.state.hardware_profile,
        expected_revision=0,
    )

    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert not window.hardware_settings_view.verification_stop_button.isHidden()
    window.hardware_settings_view.verification_stop_button.click()

    after = controller.state.hardware_profile.registry.by_external_port(2)
    assert after.verification.status is VerificationStatus.INCOMPLETE
    assert after.verification.fingerprint == before.mapping_fingerprint
    assert (after.internal_valve, after.target, after.active_high) == (
        before.internal_valve,
        before.target,
        before.active_high,
    )
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert window.hardware_settings_view.verification_stop_button.isHidden()
    assert window.hardware_settings_view.verification_panel.isHidden()
    assert not window.hardware_settings_view.editor_stack.isHidden()
    assert window.hardware_settings_view.verification_hints[2].text() == "验证已停止"
    assert "hardware-settings" not in (
        window.manual_experiment_view._notification_coordinator._events
    )


def test_verification_rejects_unsaved_draft_and_competing_owner_before_actuation(
    tmp_path, qtbot
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    saved = controller.state.hardware_profile
    channels = list(saved.channels)
    channels[1] = replace(channels[1], active_high=False)
    dirty = replace(saved, channels=tuple(channels))

    controller.handle_hardware_mock_verify_requested(2, dirty)
    assert "先保存并重连" in window.hardware_settings_view.status_label.text()
    notice = window.manual_experiment_view._notification_coordinator.current
    assert notice is not None
    assert notice.title == "无法开始验证"
    assert notice.message == "请检查设置和设备后重试。"
    assert not (tmp_path / "local_config.json").exists()

    token = controller.device_lease.acquire(
        DeviceLeaseKind.MANUAL, operation_id="busy", generation=1
    )
    assert token is not None
    controller.handle_hardware_mock_verify_requested(2, saved)
    assert "正在执行其他操作" in window.hardware_settings_view.status_label.text()
    assert not (tmp_path / "local_config.json").exists()


def test_successful_save_clears_dismissed_failure_before_reentry(
    tmp_path, qtbot
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    profile = controller.state.hardware_profile

    assert not controller.handle_hardware_profile_save_requested(profile, 0)
    first = window.manual_experiment_view.notice_frame
    assert first is not None
    notice = window.manual_experiment_view._notification_coordinator.current
    assert notice is not None and notice.message in {
        "请重新保存。",
        "请稍后重新保存。",
    }
    assert not any(
        term in notice.message
        for term in ("安全动作", "运行时", "revision", "owner", "lease")
    )
    first.close()
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    controller.state.telemetry.safety_state = "SAFE"
    candidate = replace(profile, profile_name="saved-after-failure")

    assert controller.handle_hardware_profile_save_requested(candidate, 0)
    assert "hardware-settings" not in (
        window.manual_experiment_view._notification_coordinator._events
    )
    controller.state.telemetry.connected = True
    assert not controller.handle_hardware_profile_save_requested(candidate, 1)
    assert window.manual_experiment_view.notice_frame is not None
    assert window.manual_experiment_view.current_notice_title == "保存失败"


def test_failed_profile_save_preserves_unsaved_settings_draft(tmp_path, qtbot) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    settings = window.hardware_settings_view
    settings.name_inputs[4].setText("尚未保存的柠檬")
    candidate = settings.draft.to_profile()
    controller.state.telemetry.connected = True

    assert not controller.handle_hardware_profile_save_requested(candidate, 0)
    assert settings.draft.channels[3].display_name == "尚未保存的柠檬"
    assert settings.name_inputs[4].text() == "尚未保存的柠檬"


def test_nonfailure_verification_clears_dismissed_failure_before_reentry(
    tmp_path, qtbot, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    original_write = MockHAL.write_digital
    monkeypatch.setattr(MockHAL, "write_digital", lambda *_args, **_kwargs: False)

    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    first = window.manual_experiment_view.notice_frame
    assert first is not None
    first.close()
    monkeypatch.setattr(MockHAL, "write_digital", original_write)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=1
    )
    assert controller.handle_hardware_verification_result_requested(2, True)

    assert "hardware-settings" not in (
        window.manual_experiment_view._notification_coordinator._events
    )
    monkeypatch.setattr(MockHAL, "write_digital", lambda *_args, **_kwargs: False)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=2
    )
    assert window.manual_experiment_view.notice_frame is not None
    assert window.manual_experiment_view.current_notice_title == "验证失败"


def test_verification_evidence_save_failure_uses_concise_global_copy(
    tmp_path, qtbot, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)

    def fail_evidence_save(*_args, **_kwargs):
        raise OSError("revision fingerprint mapping diagnostics")

    monkeypatch.setattr(
        controller._hardware_profile_store,
        "update_verification",
        fail_evidence_save,
    )
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    assert controller.handle_hardware_verification_result_requested(2, True)

    notice = window.manual_experiment_view._notification_coordinator.current
    assert notice is not None
    assert notice.title == "验证结果保存失败"
    assert notice.message == "请重新验证。"
    assert not any(
        term in notice.message
        for term in ("revision", "fingerprint", "mapping", "诊断")
    )
    assert "revision fingerprint mapping diagnostics" in (
        window.hardware_settings_view.status_label.text()
    )


def test_production_verification_entry_is_available_after_all_gates(
    tmp_path, qtbot
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.simulation_mode = False
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    controller._render_hardware_profile()

    assert window.hardware_settings_view.mock_buttons[2].isEnabled()
    assert not window.hardware_settings_view.name_inputs[2].isEnabled()
    assert "现场验证未开放" not in window.hardware_settings_view.status_label.text()
    assert not (tmp_path / "local_config.json").exists()


def test_short_nonzero_verification_timer_completes_and_releases(
    tmp_path,
) -> None:
    controller, _ = _controller(tmp_path, verification_duration=0.01)

    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    assert controller._hardware_verification_run is not None
    controller._hardware_verification_run = replace(
        controller._hardware_verification_run,
        deadline_ns=0,
    )
    controller._handle_hardware_verification_tick()

    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    assert controller._hardware_verification_run.phase is (
        HardwareVerificationPhase.AWAITING_CONFIRMATION
    )
    assert controller.handle_hardware_verification_result_requested(2, True)
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.MOCK_VERIFIED
    )


def test_verification_waits_for_current_user_result_and_rejects_duplicate(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )

    assert controller._hardware_verification_run.phase is (
        HardwareVerificationPhase.AWAITING_CONFIRMATION
    )
    assert not (tmp_path / "local_config.json").exists()
    assert controller.handle_hardware_verification_result_requested(2, False)
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.FAILED
    )
    assert controller._hardware_profile_store.revision == 1
    assert not controller.handle_hardware_verification_result_requested(2, True)
    assert controller._hardware_profile_store.revision == 1


def test_awaiting_confirmation_expires_as_incomplete(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    controller._hardware_verification_run = replace(
        controller._hardware_verification_run,
        deadline_ns=0,
    )

    controller._handle_hardware_verification_tick()

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


def test_enabled_button_confirmation_and_main_window_wiring(
    tmp_path, qtbot, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)
    window.show()
    controller._render_hardware_profile()
    monkeypatch.setattr(
        "app.views.hardware_settings_view.MessageBox.exec", lambda _self: True
    )

    button = window.hardware_settings_view.mock_buttons[2]
    assert button.isEnabled()
    button.click()

    assert controller._hardware_profile_store.revision == 0
    assert not window.hardware_settings_view.verification_positive_button.isHidden()
    window.hardware_settings_view.verification_positive_button.click()
    assert controller._hardware_profile_store.revision == 1
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.MOCK_VERIFIED
    )


def test_verification_cas_rejection_still_cleans_up_lease(tmp_path) -> None:
    controller, _ = _controller(tmp_path, verification_duration=20)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    path = tmp_path / "local_config.json"
    path.write_text(
        json.dumps({"hardware_profile_revision": 1}), encoding="utf-8"
    )

    controller.handle_hardware_verification_stop_requested(2)

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert controller._hardware_profile_store.revision == 0


def test_save_then_reconnect_keeps_revision_synchronized_for_verification(
    tmp_path,
) -> None:
    controller, _ = _controller(tmp_path)
    controller.state.telemetry.connected = False
    controller.state.hardware_ready = False
    controller.state.telemetry.safety_state = "SAFE"
    profile = controller.state.hardware_profile
    channels = list(profile.channels)
    channels[1] = replace(channels[1], display_name="saved-alias")

    assert controller.handle_hardware_profile_save_requested(
        replace(profile, channels=tuple(channels)), 0
    )
    assert controller._runtime_hardware_profile_revision == 1
    controller.state.telemetry.connected = True
    controller.state.hardware_ready = True
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=1
    )
    assert controller.handle_hardware_verification_result_requested(2, True)

    assert controller._hardware_profile_store.revision == 2
    assert controller._runtime_hardware_profile_revision == 2


def test_teardown_marks_active_verification_incomplete(tmp_path) -> None:
    controller, _ = _controller(tmp_path, verification_duration=20)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )

    controller.teardown(timeout_ms=50)

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


@pytest.mark.parametrize("invalid_state", ("disconnect", "unsafe"))
def test_runtime_disconnect_or_safety_change_interrupts_verification(
    tmp_path, invalid_state
) -> None:
    controller, _ = _controller(tmp_path, verification_duration=20)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    if invalid_state == "disconnect":
        controller.state.telemetry.connected = False
    else:
        controller.state.telemetry.safety_state = "UNSAFE"

    controller._handle_hardware_verification_tick()

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


@pytest.mark.parametrize("busy_kind", ("queued", "inflight"))
def test_flow_queue_or_inflight_command_blocks_verification_lease(
    tmp_path, busy_kind
) -> None:
    controller, _ = _controller(tmp_path, verification_duration=20)
    command = FlowCommand(
        command_id="busy",
        execution_epoch=0,
        sequence=1,
        mode="idle",
        a=0.0,
        b=0.0,
        c=0.0,
        source="ui",
    )
    if busy_kind == "queued":
        controller.flow_worker._queue.append(command)
    else:
        controller.flow_worker._active_command = command

    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


def test_global_stop_cancels_verification_as_incomplete(
    tmp_path, monkeypatch
) -> None:
    controller, _ = _controller(tmp_path, verification_duration=20)
    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    monkeypatch.setattr(
        controller.shutdown_service,
        "shutdown",
        lambda **_kwargs: {"result": "success", "source": "stop"},
    )

    controller.stop_hardware()

    assert controller._hardware_verification_run is None
    assert (
        controller.state.hardware_profile.registry.by_external_port(2).verification.status
        is VerificationStatus.INCOMPLETE
    )


@pytest.mark.parametrize("raw", ("nan", "inf", -1, object()))
def test_invalid_verification_duration_falls_back_without_lease_leak(
    tmp_path, raw
) -> None:
    controller, _ = _controller(tmp_path, verification_duration=raw)
    assert math.isfinite(controller._hardware_verification_duration_s)
    assert controller._hardware_verification_duration_s == 20.0

    controller.handle_hardware_mock_verify_requested(
        2, controller.state.hardware_profile, expected_revision=0
    )
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.VERIFICATION
    controller.handle_hardware_verification_stop_requested(2)
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE


@pytest.mark.parametrize("port", ("not-a-port", 99))
def test_invalid_verification_port_is_productized_and_never_acquires_lease(
    tmp_path, qtbot, port
) -> None:
    controller, _ = _controller(tmp_path)
    window = MainWindow(controller, controller.state)
    qtbot.addWidget(window)
    controller.bind_view(window)

    controller.handle_hardware_mock_verify_requested(
        port, controller.state.hardware_profile, expected_revision=0
    )

    assert controller._hardware_verification_run is None
    assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
    assert "验证失败" in window.hardware_settings_view.status_label.text()


def test_product_entry_is_single_fluent_manual_interface(qt_app) -> None:
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    assert isinstance(window, FluentWindow)
    assert window._manual_interface.findChild(type(window.manual_experiment_view)) is (
        window.manual_experiment_view
    )
    assert window.stackedWidget.count() == 2
    assert not hasattr(window, "settings_dialog")
    assert isinstance(window.hardware_settings_view, HardwareSettingsView)
    assert not hasattr(window, "tabs")
