import os
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from app.main import DEFAULT_CONFIG, build_application
from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationReceipt,
    ActuationResult,
    ManualExperimentIdentity,
    ManualExperimentSnapshot,
    ManualExperimentStatus,
)
from app.services import MockHAL
from app.services.flow_service import FlowApplyResult


class CountingHAL(MockHAL):
    def __init__(self, *, fail_self_checks: int = 0) -> None:
        super().__init__(base_flow_sccm=0.0)
        self.prepare_count = 0
        self.self_check_count = 0
        self.read_flow_count = 0
        self.fail_self_checks = fail_self_checks
        self.connection_events = []

    def prepare_do_output(self) -> bool:
        self.prepare_count += 1
        self.connection_events.append("safe_do")
        return super().prepare_do_output()

    def self_check(self):
        self.self_check_count += 1
        self.connection_events.append("self_check")
        if self.self_check_count <= self.fail_self_checks:
            return [], False
        return super().self_check()

    def read_flow(self) -> float:
        self.read_flow_count += 1
        return super().read_flow()

    def set_flow(self, channel, value=None, *, comp=False):
        effective_channel = "A" if value is None else str(channel).upper()
        self.connection_events.append(f"zero:{effective_channel}")
        return super().set_flow(channel, value, comp=comp)


class RetainedDoFailureHAL(CountingHAL):
    def __init__(self) -> None:
        super().__init__()
        self.release_attempts = 0
        self._retained = False

    def prepare_do_output(self) -> bool:
        self.prepare_count += 1
        self._retained = True
        return False

    def release_do_output(self) -> bool:
        self.release_attempts += 1
        return False

    @property
    def do_resources_in_use(self) -> bool:
        return self._retained


class IncompatiblePowerOnHAL(CountingHAL):
    power_on_safe_compatible = False
    power_on_safety_blockers = ("Dev1/port0/line0",)


class SlowPrepareHAL(CountingHAL):
    def __init__(self) -> None:
        super().__init__()
        self.prepare_entered = threading.Event()
        self.prepare_release = threading.Event()
        self.release_count = 0

    def prepare_do_output(self) -> bool:
        self.prepare_count += 1
        self.connection_events.append("safe_do")
        self.prepare_entered.set()
        self.prepare_release.wait(2.0)
        return MockHAL.prepare_do_output(self)

    def release_do_output(self) -> bool:
        self.release_count += 1
        return super().release_do_output()


class RaisingPrepareHAL(CountingHAL):
    def prepare_do_output(self) -> bool:
        self.prepare_count += 1
        raise RuntimeError("DAQ prepare boom")


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def wait_until(qt_app, predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def close_window(window, qt_app):
    window.controller.shutdown_and_teardown()
    window.close()
    qt_app.processEvents()


def test_construction_and_show_are_passive_then_auto_connect_once(qt_app):
    hal = CountingHAL()
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
        hal=hal,
    )
    controller = window.controller

    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)
    window.show()
    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)

    assert controller.schedule_startup_auto_connect() is True
    assert controller.schedule_startup_auto_connect() is False
    wait_until(qt_app, lambda: controller.state.telemetry.connected)

    assert controller._connection_request_count == 1
    assert hal.prepare_count == 1
    assert hal.self_check_count == 1
    assert [channel for channel, _value, _comp in hal.flow_commands[:3]] == ["B", "C", "A"]
    assert hal.connection_events[:5] == [
        "safe_do",
        "self_check",
        "zero:B",
        "zero:C",
        "zero:A",
    ]
    assert controller.state.hardware_ready is True
    assert controller._startup_zero_confirmed is True

    # Offscreen Qt has no native HWND for showMinimized; hide/show exercises
    # the same repeated visibility lifecycle without a platform handle.
    window.hide()
    qt_app.processEvents()
    window.show()
    qt_app.processEvents()
    window.open_hardware_settings()
    qt_app.processEvents()
    assert controller._connection_request_count == 1
    close_window(window, qt_app)


def test_real_product_construction_with_injected_hal_is_passive(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=False, hal=hal)

    assert window.controller.state.simulation_mode is False
    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)
    assert hal.flow_commands == []
    window.close()
    window.controller.shutdown_and_teardown()


def test_repeated_request_while_connecting_does_not_duplicate_owners(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()

    assert controller.request_hardware_connection(source="startup") is True
    assert controller.request_hardware_connection(source="retry") is False
    wait_until(qt_app, lambda: controller.state.telemetry.connected)

    assert controller._connection_request_count == 1
    assert hal.prepare_count == 1
    assert hal.self_check_count == 1
    close_window(window, qt_app)


def test_incompatible_power_on_polarity_blocks_real_connection_before_io(qt_app):
    hal = IncompatiblePowerOnHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=False, hal=hal)
    controller = window.controller
    window.show()

    assert controller.request_hardware_connection(source="startup") is False
    assert controller._connection_phase == "FAILED"
    assert controller._connection_request_count == 0
    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)
    assert window._connect_button.isVisible()
    window.close()
    controller.shutdown_and_teardown()


def test_prepare_timeout_cancels_owner_and_cleans_up_without_retry(qt_app):
    hal = SlowPrepareHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    assert controller.request_hardware_connection(source="startup") is True
    assert hal.prepare_entered.wait(1.0)
    request_count = controller._connection_request_count

    threading.Timer(0.05, hal.prepare_release.set).start()
    controller._handle_connection_timeout()
    wait_until(qt_app, lambda: controller._connection_phase == "FAILED")

    assert hal.release_count == 1
    assert hal.do_resources_in_use is False
    assert controller._connection_request_count == request_count
    assert controller.state.telemetry.connected is False
    assert controller.state.hardware_ready is False
    controller.shutdown_and_teardown()
    window.close()


def test_late_self_check_and_zero_completion_cannot_undo_connected_state(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller.state.telemetry.connected)

    controller.handle_self_check([], False)
    controller._handle_connection_self_check(
        [],
        False,
        controller._connection_request_count - 1,
    )
    controller._handle_startup_zero_completed(
        FlowApplyResult(True, "late", 0.0, 0.0, 0.0, 0.0)
    )

    assert controller._connection_phase == "CONNECTED"
    assert controller.state.telemetry.connected is True
    assert controller.state.hardware_ready is True
    close_window(window, qt_app)


def test_close_before_queued_auto_connect_is_zero_io(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    assert controller.schedule_startup_auto_connect() is True
    window.close()
    qt_app.processEvents()

    assert controller._startup_auto_connect_consumed is True
    assert controller._connection_request_count == 0
    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)
    controller.shutdown_and_teardown()


def test_startup_failure_has_no_auto_retry_and_manual_retry_reuses_transaction(qt_app):
    hal = CountingHAL(fail_self_checks=1)
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    requested = []
    original = controller.request_hardware_connection

    def record_request(*, source):
        requested.append(source)
        return original(source=source)

    controller.request_hardware_connection = record_request
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller._connection_phase == "FAILED")

    for _ in range(20):
        qt_app.processEvents()
    window.render_telemetry(
        controller.state.telemetry,
        hardware_ready=controller.state.hardware_ready,
    )
    assert requested == ["startup"]
    assert controller._connection_request_count == 1
    assert controller._connection_timeout_timer.isActive() is False
    assert window._connect_button.text() == "重新连接"
    assert window._connection_badge.text() == "设备未连接"
    assert window._connect_button.isVisible()
    assert window.manual_experiment_view.notice_frame is None

    window._connect_button.click()
    wait_until(qt_app, lambda: controller.state.telemetry.connected)
    assert requested == ["startup", "retry"]
    assert controller._connection_request_count == 2
    assert hal.self_check_count == 2
    close_window(window, qt_app)


def test_partial_do_acquisition_failure_attempts_same_owner_cleanup_and_requires_recovery(qt_app):
    hal = RetainedDoFailureHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller._connection_phase == "RECOVERY_REQUIRED")

    assert hal.prepare_count == 1
    assert hal.release_attempts == 1
    assert controller.state.telemetry.connected is False
    assert controller.state.hardware_ready is False
    assert controller._unsafe_shutdown_latched is True
    assert window._connection_badge.text() == "设备未连接"
    assert window._connect_button.isVisible()
    assert window.manual_experiment_view.current_notice_title == "需要立即处理"
    assert "立即关闭设备电源" in window.manual_experiment_view.detail_label.text()
    controller.teardown()
    window.close()


def test_unsafe_shutdown_retry_failure_keeps_latch_and_safety_alert(qt_app):
    hal = RaisingPrepareHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    controller._unsafe_shutdown_latched = True
    window.show()
    window.render_connection_phase("RECOVERY_REQUIRED")
    window.render_connection_safety_alert(
        "设备未能正常停止，请立即关闭设备电源"
    )

    window._connect_button.click()
    wait_until(qt_app, lambda: controller._connection_phase == "RECOVERY_REQUIRED")

    assert hal.prepare_count == 1
    assert controller._unsafe_shutdown_latched is True
    assert controller._unsafe_shutdown_retry_in_progress is False
    assert controller.state.telemetry.connected is False
    assert window._connection_badge.text() == "设备未连接"
    assert window.manual_experiment_view.current_notice_title == "需要立即处理"
    assert (
        window.manual_experiment_view.detail_label.text()
        == "设备未能正常停止，请立即关闭设备电源"
    )
    controller.teardown()
    window.close()


def test_unsafe_shutdown_retry_success_clears_safety_alert_only_after_connected(
    qt_app,
):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    controller._unsafe_shutdown_latched = True
    window.show()
    window.render_connection_phase("RECOVERY_REQUIRED")
    window.render_connection_safety_alert(
        "设备未能正常停止，请立即关闭设备电源"
    )

    window._connect_button.click()
    assert controller._unsafe_shutdown_retry_in_progress is True
    assert window.manual_experiment_view.current_notice_title == "需要立即处理"
    wait_until(qt_app, lambda: controller.state.telemetry.connected)

    assert controller._connection_phase == "CONNECTED"
    assert controller._unsafe_shutdown_latched is False
    assert controller._unsafe_shutdown_retry_in_progress is False
    assert window.manual_experiment_view.current_notice_title == ""
    assert "立即关闭设备电源" not in window.manual_experiment_view.detail_label.text()
    close_window(window, qt_app)


def test_do_prepare_exception_fails_transaction_without_self_check(qt_app):
    hal = RaisingPrepareHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller._connection_phase == "FAILED")

    assert hal.prepare_count == 1
    assert hal.self_check_count == 0
    assert hal.do_resources_in_use is False
    controller.shutdown_and_teardown()
    window.close()


def test_disconnected_global_stop_and_shutdown_do_not_acquire_hardware(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller

    controller.stop_hardware()
    controller.shutdown_and_teardown()

    assert controller._connection_request_count == 0
    assert (hal.prepare_count, hal.self_check_count, hal.read_flow_count) == (0, 0, 0)
    window.close()


def test_runtime_disconnect_fails_closed_without_auto_reconnect(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller.state.telemetry.connected)
    request_count = controller._connection_request_count

    controller.worker.consume_airflow_sample(
        float("nan"),
        time.time(),
        "serial disconnected",
    )
    wait_until(qt_app, lambda: controller._connection_phase == "RUNTIME_DISCONNECTED")

    assert controller._connection_phase == "RUNTIME_DISCONNECTED"
    assert controller._connection_request_count == request_count
    assert controller.state.telemetry.connected is False
    assert controller.state.hardware_ready is False
    assert window._connection_badge.text() == "设备未连接"
    assert window._connect_button.text() == "重新连接"
    assert window._connect_button.isVisible()
    for _ in range(20):
        qt_app.processEvents()
    assert controller._connection_request_count == request_count

    window._connect_button.click()
    controller.handle_telemetry(
        {
            "connected": True,
            "airflow": 999.0,
            "timestamp": 0.0,
            "application_safety_state": "SAFE",
            "application_safety_reason": "queued before Global Stop",
            "airflow_sample_timestamp": -1.0,
        }
    )
    assert controller.state.telemetry.connected is False
    wait_until(qt_app, lambda: controller._connection_phase == "CONNECTED")
    assert controller._connection_request_count == request_count + 1
    assert window._connection_badge.text() == "设备已连接"
    assert not window._connect_button.isVisible()
    assert all(value == 0.0 for _channel, value, _comp in hal.flow_commands[-3:])
    assert not any(hal._digital_state.values())
    close_window(window, qt_app)


def test_global_stop_success_allows_manual_reconnect_without_auto_retry(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller._connection_phase == "CONNECTED")
    request_count = controller._connection_request_count
    window.manual_experiment_view.clear_notice()
    old_identity = ManualExperimentIdentity("manual-before-stop", 1, 0)
    old_snapshot = ManualExperimentSnapshot(
        status=ManualExperimentStatus.STIMULATING,
        identity=old_identity,
        selected_external_ports=(2,),
        open_confirmed=(2,),
        possibly_open=(2,),
    )
    controller._manual_generation = 1
    controller._manual_snapshot = old_snapshot

    controller.stop_hardware()
    qt_app.processEvents()
    controller._handle_manual_snapshot(old_snapshot)

    assert controller._connection_phase == "DISCONNECTED"
    assert controller.state.telemetry.connected is False
    assert controller.state.hardware_ready is False
    assert window._connection_badge.text() == "设备未连接"
    assert window._connect_button.text() == "重新连接"
    assert window._connect_button.isVisible()
    assert controller._manual_snapshot.status is ManualExperimentStatus.IDLE
    assert controller._manual_snapshot.identity is None
    assert controller._manual_snapshot.open_confirmed == ()
    assert controller._manual_snapshot.possibly_open == ()
    assert "已安全停止" not in window._connection_badge.text()
    notice = window.manual_experiment_view.notice_frame
    assert notice is None or not notice.isVisibleTo(window)
    for _ in range(20):
        qt_app.processEvents()
    assert controller._connection_request_count == request_count

    window._connect_button.click()
    controller.handle_telemetry(
        {
            "connected": True,
            "airflow": 999.0,
            "timestamp": 0.0,
            "application_safety_state": "SAFE",
            "application_safety_reason": "queued before Global Stop",
            "airflow_sample_timestamp": -1.0,
        }
    )
    assert controller.state.telemetry.connected is False
    wait_until(qt_app, lambda: controller._connection_phase == "CONNECTED")
    assert controller._connection_request_count == request_count + 1
    assert window._connection_badge.text() == "设备已连接"
    assert not window._connect_button.isVisible()
    assert all(value == 0.0 for _channel, value, _comp in hal.flow_commands[-3:])
    assert not any(hal._digital_state.values())
    close_window(window, qt_app)


def test_global_stop_failure_keeps_fail_closed_warning(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller._connection_phase == "CONNECTED")
    original_shutdown = controller.shutdown_service.shutdown
    controller.shutdown_service.shutdown = lambda **_kwargs: {
        "source": "stop",
        "result": "unsafe",
        "error": "valve close receipt timeout",
        "ts": time.time(),
    }

    controller.stop_hardware()
    qt_app.processEvents()

    assert controller._connection_phase == "RECOVERY_REQUIRED"
    assert controller._unsafe_shutdown_latched is True
    assert controller.state.telemetry.connected is False
    assert controller.state.hardware_ready is False
    assert window._connection_badge.text() == "设备未连接"
    assert window.manual_experiment_view.current_notice_title == "需要立即处理"
    assert (
        window.manual_experiment_view.detail_label.text()
        == "设备未能正常停止，请立即关闭设备电源"
    )
    assert "receipt" not in window.manual_experiment_view.detail_label.text()

    controller.shutdown_service.shutdown = original_shutdown
    close_window(window, qt_app)


def test_safety_receipt_driver_error_is_logged_but_not_exposed_in_ui(
    qt_app,
    caplog,
):
    _, window = build_application(
        DEFAULT_CONFIG,
        start_worker=False,
        simulation=True,
    )
    controller = window.controller
    window.show()
    raw_error = (
        "NI-DAQmx 数字输出异常：Write cannot be performed when auto start is false; "
        "Status Code: -200846; Task Name: hidden"
    )
    receipt = ActuationReceipt(
        command_id="safe-stop-selector-test",
        execution_epoch=1,
        arm_epoch=1,
        sequence=1,
        trial_id=None,
        trial_index=None,
        valve=0,
        action=ActuationAction.CLOSE,
        category=ActuationCategory.SAFETY,
        expected_ns=1,
        started_ns=2,
        actual_ns=None,
        wall_timestamp=1.0,
        offset_ms=None,
        jitter_ms=None,
        result=ActuationResult.UNCERTAIN,
        measurement_point="daqmx_write_ack",
        message=raw_error,
    )

    with caplog.at_level("INFO", logger="protocol_execution"):
        controller._handle_actuation_receipt(receipt)
        qt_app.processEvents()

    assert raw_error in caplog.text
    assert window._actuation_alert_label.text() == ""
    assert window.manual_experiment_view.current_notice_title == "需要立即处理"
    assert (
        window.manual_experiment_view.detail_label.text()
        == "设备未能正常停止，请立即关闭设备电源"
    )
    visible_text = " ".join(
        widget.text() for widget in window.findChildren(type(window._status_label))
    )
    assert "NI-DAQmx" not in visible_text
    assert "-200846" not in visible_text
    close_window(window, qt_app)


def test_queued_precommit_disconnected_payload_does_not_undo_connection(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    controller = window.controller
    window.show()
    controller.schedule_startup_auto_connect()
    wait_until(qt_app, lambda: controller.state.telemetry.connected)

    controller.handle_telemetry(
        {
            "connected": False,
            "airflow": 0.0,
            "timestamp": time.time(),
            "airflow_sample_timestamp": time.time(),
        }
    )

    assert controller._connection_phase == "CONNECTED"
    assert controller.state.telemetry.connected is True
    close_window(window, qt_app)


def test_worker_start_is_idle_until_connect_requests_self_check(qt_app):
    hal = CountingHAL()
    _, window = build_application(DEFAULT_CONFIG, simulation=True, hal=hal)
    worker = window.controller.worker

    worker.start()
    wait_until(qt_app, worker.isRunning)
    for _ in range(5):
        qt_app.processEvents()
    assert hal.self_check_count == 0
    worker.stop()
    window.controller.teardown()
    window.close()
