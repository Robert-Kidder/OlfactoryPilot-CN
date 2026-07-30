from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from app.models import (
    ActuationCategory,
    AppState,
    ProtocolExecutionReadiness,
    ProtocolExecutionSnapshot,
    ProtocolExecutionStatus,
    SafetyState,
)
from app.models.session import SessionState, SessionStatus, SessionViewSnapshot
from app.services import (
    ActuationDOAdapter,
    ActuationMetrics,
    AnalogInputFrame,
    BreathSampleBatch,
    CalibrationSession,
    FlowApplyResult,
    FlowService,
    GatingService,
    MockHAL,
    ProtocolExecutor,
    ProtocolExecutorResult,
    ProtocolParseError,
    SafetyManager,
    ShutdownService,
    TtlPulse,
    ValveService,
    parse_protocol_file,
)
from app.services.session_file_service import SessionFileError, SessionFileService
from app.services.valve_service import ValveWritePlan
from app.workers import (
    ActuationInterlockIngress,
    ActuationWorker,
    FlowWorker,
    HardwareWorker,
    InterlockSnapshot,
)
from app.workers.session_writer import (
    RecorderReadinessLatch,
    SessionRecorderIngress,
    SessionWriterConfig,
    SessionWriterWorker,
)

if TYPE_CHECKING:
    from app.views import MainWindow

LOG = logging.getLogger(__name__)


class RecoveryScanWorker(QThread):
    completed = Signal(object, object, object)

    def __init__(
        self,
        service: SessionFileService,
        output_path: Path,
        request_id: int,
    ) -> None:
        super().__init__()
        self._service = service
        self._output_path = output_path
        self._request_id = request_id
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            result = self._service.scan_recovery(
                self._output_path,
                cancel_event=self._cancel_event,
            )
        except Exception as exc:  # pragma: no cover - defensive thread boundary
            result = exc
        self.completed.emit(self._request_id, self._output_path, result)


class MainController(QObject):
    _pretest_sequence_completed = Signal(str, list, object, bool, str)
    _startup_zero_completed = Signal(object)
    _session_finalized = Signal(object)

    def __init__(
        self,
        state: AppState,
        worker: HardwareWorker,
        safety_manager=None,
        config: dict | None = None,
        *,
        allow_test_actuation_bridge: bool = False,
    ) -> None:
        super().__init__()
        self.config = dict(config or {})
        self._allow_test_actuation_bridge = bool(allow_test_actuation_bridge)
        self.state = state
        self.simulation_mode = state.simulation_mode
        self.worker = worker
        self.safety_manager = safety_manager or SafetyManager()
        shutdown_cfg = dict(self.config)
        if "shutdown_record_path" not in shutdown_cfg:
            shutdown_cfg["shutdown_record_path"] = str(
                Path.cwd() / "logs" / "last_shutdown_event.json"
            )
        self.shutdown_service = ShutdownService(
            state=state,
            worker=worker,
            safety_manager=self.safety_manager,
            retry_limit=int(shutdown_cfg.get("shutdown_retry_limit", 2)),
            retry_interval=float(shutdown_cfg.get("shutdown_retry_interval_s", 0.2)),
            record_path=self._resolve_record_path(shutdown_cfg),
        )
        self.gating_service = GatingService(
            inhale_threshold=state.inhale_threshold,
            exhale_threshold=state.exhale_threshold,
        )
        self.valve_service = ValveService(
            state=state,
            safety_manager=self.safety_manager,
            worker=worker,
            valve_variants=state.valve_variants,
            hardware_variant=state.hardware_variant,
            master_valve_line=state.master_valve_line,
        )
        self.protocol_executor = ProtocolExecutor(
            gating_service=self.gating_service,
            valve_writer=self._reject_synchronous_protocol_write,
            config=config or {},
            clock=time.time,
            deferred_actuation=True,
        )
        self._protocol_logger = logging.getLogger("protocol_execution")
        hal_instance = getattr(worker, "hal", None) or MockHAL()
        self.actuation_interlock = ActuationInterlockIngress(
            InterlockSnapshot(
                connected=bool(state.telemetry.connected),
                hardware_ready=bool(state.hardware_ready),
                flow_setpoints_ready=bool(state.flow_setpoints_ready),
                safety_state=state.telemetry.safety_state,
                ttl_input_ready=bool(getattr(worker, "ttl_input_ready", False)),
                has_protocol=bool(state.loaded_protocol),
                device_lease="idle",
            ),
            safety_manager=self.safety_manager,
        )
        self.actuation_adapter = ActuationDOAdapter(
            hal=hal_instance,
            target_resolver=self.valve_service.resolve_target,
            write_timeout_ms=int((config or {}).get("actuation_write_timeout_ms", 100)),
        )
        self.flow_service = FlowService(
            hal_instance,
            master_target=state.master_valve_line or None,
            master_writer=None,
        )
        telemetry_hz = max(0.1, float(self.config.get("telemetry_hz", 5.0)))
        self.flow_worker = FlowWorker(
            self.flow_service,
            parent=self,
            airflow_poll_interval_s=1.0 / telemetry_hz,
        )
        airflow_sink = getattr(worker, "consume_airflow_sample", None)
        if callable(airflow_sink):
            self.flow_worker.set_airflow_sink(airflow_sink)
        self.actuation_worker = ActuationWorker(
            protocol_executor=self.protocol_executor,
            writer=self.actuation_adapter.execute,
            interlock=self.actuation_interlock,
            valve_service=self.valve_service,
            sample_transform=state.apply_calibration,
            normal_queue_capacity=int((config or {}).get("actuation_normal_queue_capacity", 256)),
            flow_submitter=self.flow_worker.submit,
            metrics=ActuationMetrics(self.config),
            parent=self,
        )
        self.shutdown_service.actuation_worker = self.actuation_worker
        self.shutdown_service.flow_worker = self.flow_worker
        self.shutdown_service.actuation_timeout_ms = int(
            (config or {}).get("actuation_shutdown_timeout_ms", 2000)
        )
        self.shutdown_service.emergency_close_timeout_ms = int(
            (config or {}).get("actuation_emergency_close_timeout_ms", 500)
        )
        if hasattr(worker, "set_actuation_sink"):
            worker.set_actuation_sink(
                self.actuation_worker,
                interlock_ingress=self.actuation_interlock,
            )
        if hasattr(worker, "set_self_check_coordinator"):
            worker.set_self_check_coordinator(
                before=self._pause_flow_owner_for_self_check,
                after=self._resume_flow_owner_after_self_check,
            )
        self.calibration_session = CalibrationSession() # Story 2.6
        self._last_safety_state: SafetyState | None = None
        self._has_seen_connection = False
        self._connect_in_progress = False
        self._pretest_sequence_in_progress = False
        self._actuation_request_sequence = 0
        self._pending_plan_ui: dict[str, dict] = {}
        self._pending_flow_context: dict[str, dict] = {}
        self._last_flow_result: FlowApplyResult | None = None
        self._pending_protocol_load = None
        self._last_document_load_success: bool | None = None
        self._protocol_lease_epoch: int | None = None
        self._protocol_start_pending = False
        self._protocol_master_prepare_pending = False
        self.session_state = SessionState()
        self.session_file_service = SessionFileService()
        self.recorder_readiness = RecorderReadinessLatch()
        self.session_writer: SessionWriterWorker | None = None
        self.session_ingress: SessionRecorderIngress | None = None
        self._session_generation = 0
        self._session_controller_sequence = 0
        self._session_controller_fenced = False
        self._session_protocol_document = None
        self._session_close_pending_reason = ""
        self._session_finalize_started = False
        self._session_global_stop_in_progress = False
        self._session_finalize_event = threading.Event()
        self._session_finalize_result = None
        self._session_finalize_thread: threading.Thread | None = None
        self._pretest_thread: threading.Thread | None = None
        self._session_preview = None
        self._session_display_message = ""
        self._session_recovery_messages: tuple[str, ...] = ()
        self._last_recovery_output: Path | None = None
        self._last_recovery_location: Path | None = None
        self._recovery_scan_worker: RecoveryScanWorker | None = None
        self._recovery_scan_request = 0
        self._pending_recovery_output: Path | None = None
        previous_shutdown = state.last_shutdown_event or {}
        self._unsafe_shutdown_latched = bool(
            previous_shutdown and previous_shutdown.get("result") != "success"
        )
        self.view: MainWindow | None = None
        self.worker.telemetry_ready.connect(self.handle_telemetry)
        self.worker.status_message.connect(self.handle_status)
        self.worker.self_check_completed.connect(self.handle_self_check)
        if hasattr(self.worker, "breath_samples"):
            self.worker.breath_samples.connect(self.handle_breath_samples)
        if hasattr(self.worker, "ttl_pulse"):
            self.worker.ttl_pulse.connect(self.handle_ttl_pulse)
        if hasattr(self.worker, "ttl_input_error"):
            self.worker.ttl_input_error.connect(self.handle_ttl_input_error)
        self.actuation_worker.ttl_arm_requested.connect(
            self.worker.post_ttl_arm,
            Qt.ConnectionType.DirectConnection,
        )
        self.actuation_worker.ttl_disarm_requested.connect(
            self.worker.post_ttl_disarm,
            Qt.ConnectionType.DirectConnection,
        )
        self.actuation_worker.executor_result_ready.connect(self._publish_protocol_result)
        self.actuation_worker.start_result_ready.connect(self._handle_protocol_start_result)
        self.actuation_worker.receipt_ready.connect(self._handle_actuation_receipt)
        self.actuation_worker.plan_result_ready.connect(self._handle_actuation_plan_result)
        self.flow_worker.result_ready.connect(self.actuation_worker.post_flow_result)
        self.actuation_worker.flow_result_ready.connect(self._handle_flow_command_result)
        self.actuation_worker.snapshot_ready.connect(self._handle_protocol_snapshot)
        self.actuation_worker.document_result_ready.connect(self._handle_document_result)
        self._protocol_snapshot = ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.IDLE,
            status_text="空闲",
            has_protocol=False,
            can_start=False,
            can_stop=False,
            can_advance=False,
        )
        self._pretest_sequence_completed.connect(self._handle_pretest_sequence_completed)
        self._startup_zero_completed.connect(self._handle_startup_zero_completed)
        self._session_finalized.connect(self._handle_session_finalized)
        self._breath_logger = logging.getLogger("breath_viz")
        self._protocol_tick_timer = QTimer(self)
        self._protocol_tick_timer.setInterval(50)
        self._protocol_tick_timer.timeout.connect(self.handle_protocol_executor_tick)

    def bind_view(self, view: MainWindow) -> None:
        self.view = view
        self._apply_previous_shutdown_status()
        self._apply_safety_check(initial=True)
        self.view.update_status(self.state.status_message)
        self.view.render_telemetry(self.state.telemetry)
        self.view.render_last_shutdown(self.state.last_shutdown_event)
        self._update_pretest_view_safety(
            SafetyState(
                state=self.state.telemetry.safety_state,
                airflow=self.state.telemetry.airflow,
                threshold=self.state.low_flow_threshold,
                updated_at=self.state.telemetry.timestamp,
                reason=self.state.telemetry.safety_reason,
            )
        )
        self._refresh_toolbar_state()
        self._render_session_snapshot()
        if hasattr(self.view, "pretest_view"):
            self.view.pretest_view.flow_sequence_requested.connect(self.handle_flow_sequence_request)
        if hasattr(self.view, "calibration_view"):
            self.view.calibration_view.breath_metrics.connect(self.handle_breath_metrics)
            self.view.calibration_view.threshold_changed.connect(self.update_breath_threshold)
            self.view.calibration_view.calibration_requested.connect(self.handle_calibration_request)
        if hasattr(self.view, "protocol_view"):
            self.view.protocol_view.set_quality_target_ms(
                float(self.config.get("actuation_jitter_target_ms", 20.0))
            )
            self._render_protocol_execution_state()
        if not self._protocol_tick_timer.isActive():
            self._protocol_tick_timer.start()
        if not self.state.has_active_valve_map():
            LOG.warning(
                "No valve mapping found for variant %s; PreTestView will be disabled",
                self.state.hardware_variant,
            )

    def start_worker(self) -> bool:
        if self._unsafe_shutdown_latched:
            self._block_for_unsafe_shutdown(
                "检测到未经人工确认的关闭失败；请点击连接以明确重试。"
            )
            return False
        restart_epoch = int(self.actuation_worker.protocol_state.execution_epoch)
        start_flow = not self.flow_worker.isRunning()
        start_actuation = not self.actuation_worker.isRunning()
        if start_flow:
            if not self.flow_worker.prepare_restart(execution_epoch=restart_epoch):
                self._latch_restart_failure("流量 owner 无法安全重绑定 execution epoch。")
                return False
        if start_actuation and not self.actuation_worker.prepare_restart():
            if start_flow:
                self.flow_worker.shutdown(timeout_ms=1)
            self._latch_restart_failure(
                "动作 owner 未完成 DO session 交接，已阻止硬件重启。"
            )
            return False
        if start_flow:
            LOG.info("Starting serial flow worker thread")
            self.flow_worker.start()
        if start_actuation:
            LOG.info("Starting actuation worker thread")
            self.actuation_worker.start(QThread.Priority.HighPriority)
        if not self.worker.isRunning():
            LOG.info("Starting hardware worker thread")
            self.worker.start(QThread.Priority.HighPriority)
        return True

    def _pause_flow_owner_for_self_check(self) -> bool:
        """Give hardware self-check exclusive, bounded access to the serial port."""
        timeout_ms = int(self.config.get("actuation_shutdown_timeout_ms", 2000))
        return self.flow_worker.shutdown(timeout_ms)

    def _resume_flow_owner_after_self_check(self) -> bool:
        restart_epoch = int(self.actuation_worker.protocol_state.execution_epoch)
        if not self.flow_worker.prepare_restart(execution_epoch=restart_epoch):
            return False
        if not self.flow_worker.isRunning():
            self.flow_worker.start()
        return True

    def shutdown(self) -> None:
        LOG.info("Shutting down worker thread")
        finalize_session = self._prepare_session_for_global_stop("app_exit")
        event = self.shutdown_service.shutdown(
            source="app_exit",
            reason="application_exit",
            force=True,
        )
        self._handle_shutdown_event(event, success_message="已安全关闭")
        if finalize_session:
            self._finish_session_after_global_stop(event)
            self.wait_for_session_finalization(
                SessionWriterConfig.from_mapping(self.config).close_timeout_ms
                / 1000.0
            )

    def shutdown_and_teardown(self) -> None:
        try:
            self.shutdown()
        finally:
            self.teardown(
                timeout_ms=SessionWriterConfig.from_mapping(
                    self.config
                ).close_timeout_ms
            )

    def teardown(self, *, timeout_ms: int = 2000) -> None:
        """Idempotently join every timer/thread owned by this Controller."""
        timeout = max(1, int(timeout_ms))
        if self._protocol_tick_timer.isActive():
            self._protocol_tick_timer.stop()
        recovery = self._recovery_scan_worker
        if recovery is not None and recovery.isRunning():
            recovery.cancel()
            if recovery.wait(timeout):
                self._recovery_scan_worker = None
            else:
                LOG.error("Recovery scan thread did not stop within teardown timeout")
        elif recovery is not None:
            self._recovery_scan_worker = None
        if self.session_state.status == SessionStatus.PREPARED:
            descriptor = self.session_state.descriptor
            if descriptor is not None:
                self.session_file_service.mark_inactive(
                    descriptor.paths.staging_dir
                )
            self.session_state.fail(
                "会话路径已锁定但记录尚未开始；请从恢复目录检查该会话。",
                recovery_required=True,
            )
        writer = self.session_writer
        if writer is not None and writer.isRunning():
            writer.fail_from_producer(
                stage="teardown",
                message="测试/生命周期 teardown 已终止未收尾会话。",
            )
            writer.wait(timeout)
        finalize_thread = self._session_finalize_thread
        if (
            finalize_thread is not None
            and finalize_thread is not threading.current_thread()
            and finalize_thread.is_alive()
        ):
            finalize_thread.join(timeout / 1000.0)
        pretest_thread = self._pretest_thread
        if (
            pretest_thread is not None
            and pretest_thread is not threading.current_thread()
            and pretest_thread.is_alive()
        ):
            pretest_thread.join(timeout / 1000.0)
        self.actuation_worker.shutdown(timeout)
        if self.worker.isRunning():
            self.worker.stop()
        self.flow_worker.shutdown(timeout)

    def lifecycle_stopped(self) -> bool:
        recovery = self._recovery_scan_worker
        writer = self.session_writer
        finalizer = self._session_finalize_thread
        pretest = self._pretest_thread
        return (
            (recovery is None or not recovery.isRunning())
            and (writer is None or not writer.isRunning())
            and (finalizer is None or not finalizer.is_alive())
            and (pretest is None or not pretest.is_alive())
            and not self.actuation_worker.isRunning()
            and not self.worker.isRunning()
            and not self.flow_worker.isRunning()
        )

    def _prepare_session_for_global_stop(self, reason: str) -> bool:
        if self.session_state.status == SessionStatus.RECORDING:
            if not self.session_state.begin_close(reason):
                return False
            self._session_close_pending_reason = reason
            self._session_global_stop_in_progress = True
            self.actuation_interlock.update(
                recording_ready=False,
                session_closing=True,
            )
            return True
        closing = self.session_state.status == SessionStatus.CLOSING
        if closing:
            self._session_global_stop_in_progress = True
            return True
        return False

    def _finish_session_after_global_stop(self, event: dict) -> None:
        safe_to_publish = (
            event.get("result") == "success"
            and self.actuation_worker.protocol_state.active_valve is None
            and not self.actuation_worker.protocol_state.possibly_open_valves
            and not self.actuation_worker.isRunning()
            and bool(getattr(self.actuation_worker, "_do_handed_off", False))
        )
        if not safe_to_publish:
            writer = self.session_writer
            if writer is not None:
                writer.fail_from_producer(
                    stage="unsafe_shutdown",
                    message=(
                        "硬件安全关闭、阀门状态或 owner handoff 未完整确认；"
                        "禁止发布 complete 会话。"
                    ),
                )
                if not self._session_finalize_started:
                    self._session_finalize_started = True
                    self._start_session_finalizer(name="session-finalize-abort")
            self._session_global_stop_in_progress = False
            return
        if self._session_controller_fenced:
            self._session_global_stop_in_progress = False
            return
        self._post_controller_session_event(
            event="shutdown",
            source=str(event.get("source") or "shutdown"),
            result=str(event.get("result") or "unknown"),
            message="硬件安全关闭完成。",
            payload=dict(event),
        )
        self._session_global_stop_in_progress = False
        self._begin_session_finalization()

    @Slot(bool, int)
    def handle_calibration_request(self, active: bool, duration: int) -> None:
        if active:
            # Start
            self.calibration_session.duration_sec = float(duration)
            self.calibration_session.start()
            LOG.info("Calibration started, duration=%ds", duration)
        else:
            # Stop / Interrupt
            self.calibration_session.stop()
            LOG.info("Calibration interrupted by user")
            if self.view and hasattr(self.view, "calibration_view"):
                self.view.calibration_view.set_calibration_state(False, "已中断")

    @Slot(dict)
    def handle_telemetry(self, payload: dict) -> None:
        previous_safety_state = self.state.telemetry.safety_state
        hardware_safety = payload.get("safety_state")
        self.state.update_telemetry(payload)
        if self.state.telemetry.connected:
            self._has_seen_connection = True

        LOG.debug(
            "Telemetry received | airflow=%.3f | safety=%s | ts=%.3f | connected=%s | hw=%s",
            self.state.telemetry.airflow,
            self.state.telemetry.safety_state,
            self.state.telemetry.timestamp,
            self.state.telemetry.connected,
            hardware_safety,
        )
        self._apply_safety_check(
            hardware_safety=hardware_safety,
            previous_state_override=previous_safety_state,
        )
        self._publish_interlock_from_state()
        self.actuation_worker.post_readiness_update(
            readiness=self._execution_readiness(),
            timestamp=self.state.telemetry.timestamp or time.time(),
        )
        self._drain_actuation_if_not_running()
        if self.view:
            self.view.render_telemetry(self.state.telemetry)
            self.view.update_status(self.state.status_message)
            if hasattr(self.view, "pretest_view"):
                self.view.pretest_view.update_airflow(self.state.telemetry.airflow)
        self._refresh_toolbar_state()

    @Slot(str)
    def handle_status(self, message: str) -> None:
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)

    @Slot(int, bool)
    def handle_valve_toggle_request(self, channel_id: int, desired_state: bool) -> None:
        if not self.state.has_active_valve_map():
            message = "未找到 20 通道映射，已阻断写入"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
                if hasattr(self.view, "pretest_view"):
                    self.view.pretest_view.apply_safety_state(
                        self.state.telemetry.safety_state,
                        message,
                        disabled=True,
                    )
                    self.view.pretest_view.show_warning(message)
            LOG.warning("Valve toggle blocked | channel=%s | reason=%s", channel_id, message)
            return

        safety_state = self._last_safety_state or SafetyState(
            state=self.state.telemetry.safety_state,
            airflow=self.state.telemetry.airflow,
            threshold=self.state.low_flow_threshold,
            updated_at=self.state.telemetry.timestamp,
            reason=self.state.telemetry.safety_reason,
        )
        success, plan_or_message = self.valve_service.plan_valve(
            channel_id,
            desired_state,
            safety_state=safety_state,
        )
        message = (
            self._submit_valve_plan(
                plan_or_message,
                category=ActuationCategory.MANUAL,
                ui_context={"kind": "manual", "channels": [channel_id]},
            )
            if success and isinstance(plan_or_message, ValveWritePlan)
            else str(plan_or_message)
        )
        self.state.update_status(message)
        if self.view:
            self.view.update_status(self.state.status_message)
            if hasattr(self.view, "pretest_view"):
                self.view.pretest_view.set_valve_state(
                    channel_id, self.valve_service.is_open(channel_id)
                )
                disabled = safety_state.state != "SAFE" or not self.state.has_active_valve_map()
                self.view.pretest_view.apply_safety_state(
                    safety_state.state,
                    safety_state.reason,
                    disabled=disabled,
                )
                self.view.pretest_view.set_master_state(self.valve_service.master_is_open())
                if not success:
                    self.view.pretest_view.show_warning(message)
                else:
                    self.view.pretest_view.show_warning("")
        if not success:
            LOG.warning("Valve toggle blocked | channel=%s | reason=%s", channel_id, message)

    @Slot(str, list)
    def handle_valve_sequence_request(self, mode: str, channels: list) -> None:
        """Open/close staged odor valves as part of the Start/Rest sequence."""
        if not channels:
            return
        if not self.state.has_active_valve_map():
            message = "未找到 20 通道映射，已阻断写入"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.warning("Valve sequence blocked | mode=%s | reason=%s", mode, message)
            return

        desired_state = mode == "stim_start"
        safety_state = self._last_safety_state or SafetyState(
            state=self.state.telemetry.safety_state,
            airflow=self.state.telemetry.airflow,
            threshold=self.state.low_flow_threshold,
            updated_at=self.state.telemetry.timestamp,
            reason=self.state.telemetry.safety_reason,
        )

        messages: list[str] = []
        success = True
        for channel in channels:
            channel_id = int(channel)
            ok, plan_or_message = self.valve_service.plan_valve(
                channel_id,
                desired_state,
                safety_state=safety_state,
            )
            message = (
                self._submit_valve_plan(
                    plan_or_message,
                    category=ActuationCategory.PRETEST,
                    ui_context={"kind": "sequence", "channels": [channel_id]},
                )
                if ok and isinstance(plan_or_message, ValveWritePlan)
                else str(plan_or_message)
            )
            messages.append(message)
            if not ok:
                success = False
                break

        status = (
            f"启动阀门序列完成：{', '.join(str(int(ch)) for ch in channels)}"
            if success and desired_state
            else f"关闭阀门序列完成：{', '.join(str(int(ch)) for ch in channels)}"
            if success
            else messages[-1]
        )
        self.state.update_status(status)
        if self.view:
            self.view.update_status(self.state.status_message)
            if hasattr(self.view, "pretest_view"):
                for channel in channels:
                    channel_id = int(channel)
                    self.view.pretest_view.set_valve_state(
                        channel_id,
                        self.valve_service.is_open(channel_id),
                    )
                self.view.pretest_view.set_master_state(self.valve_service.master_is_open())
                if not success:
                    self.view.pretest_view.show_warning(status)
                    if desired_state:
                        self.view.pretest_view.abort_flow_sequence(status)
                else:
                    self.view.pretest_view.show_warning("")

        if not success:
            LOG.warning(
                "Valve sequence failed | mode=%s | channels=%s | reason=%s",
                mode,
                channels,
                status,
            )

    @Slot(str, list, float, float, float)
    def handle_pretest_sequence_request(
        self,
        mode: str,
        channels: list,
        a: float,
        b: float,
        c: float,
    ) -> None:
        """Run flow + valve sequence off the UI thread to avoid hardware I/O stalls."""
        if self._pretest_sequence_in_progress:
            message = "上一条启动/停止序列仍在执行，请稍候"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            return

        self._pretest_sequence_in_progress = True
        pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None
        if pretest:
            pretest.set_applying(True)
            pretest.set_flow_message("正在执行硬件序列...")

        thread = threading.Thread(
            target=self._run_pretest_sequence,
            args=(mode, [int(ch) for ch in channels], float(a), float(b), float(c)),
            daemon=True,
        )
        self._pretest_thread = thread
        thread.start()

    def _run_pretest_sequence(
        self,
        mode: str,
        channels: list[int],
        a: float,
        b: float,
        c: float,
    ) -> None:
        telemetry = self.state.telemetry
        hardware_ready = self.state.hardware_ready or telemetry.connected or self.state.simulation_mode
        if not hardware_ready:
            result = FlowApplyResult(
                success=False,
                message=f"硬件未就绪，阻断 UI·pretest-seq-{mode}",
                a=a,
                b=b,
                c=c,
                a_comp=a + c,
                error="hardware_not_ready",
            )
            self._pretest_sequence_completed.emit(mode, channels, result, False, result.message)
            return

        source = f"pretest:{self._actuation_request_sequence + 1}"
        self._pending_flow_context[source] = {
            "kind": "pretest",
            "mode": mode,
            "channels": channels,
        }
        self._submit_flow_intent(mode=mode, a=a, b=b, c=c, source=source)

    @Slot(str, list, object, bool, str)
    def _handle_pretest_sequence_completed(
        self,
        mode: str,
        channels: list,
        result_obj: object,
        success: bool,
        status: str,
    ) -> None:
        result = result_obj if isinstance(result_obj, FlowApplyResult) else None
        self._pretest_sequence_in_progress = False
        pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None

        if result and result.success:
            self.state.applied_a = result.a
            self.state.applied_b = result.b
            self.state.applied_c = result.c
            self.state.applied_a_comp = result.a_comp
            self.state.flow_setpoints_ready = True
            if pretest:
                pretest.set_applied_values(
                    a=result.a,
                    b=result.b,
                    c=result.c,
                    a_comp=result.a_comp,
                )
        elif result:
            self.state.flow_setpoints_ready = False

        for channel in channels:
            channel_id = int(channel)
            if pretest:
                pretest.set_valve_state(channel_id, self.valve_service.is_open(channel_id))
        if pretest:
            pretest.set_master_state(self.valve_service.master_is_open())
            pretest.set_applying(False)
            pretest.set_flow_message(status)
            if success:
                pretest.show_warning("")
            else:
                pretest.show_warning(status)
                if mode == "stim_start":
                    pretest.abort_flow_sequence(status)

        self.state.update_status(status)
        if self.view:
            self.view.update_status(self.state.status_message)

    @Slot(float, float, float)
    def handle_apply_request(self, flow_a: float, flow_b: float, flow_c: float) -> FlowApplyResult:
        pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None
        if pretest:
            pretest.set_applying(True)

        telemetry = self.state.telemetry
        hardware_ready = self.state.hardware_ready or telemetry.connected or self.state.simulation_mode
        if not hardware_ready:
            message = "硬件未就绪，阻断 UI·apply-flow"
            result = FlowApplyResult(
                success=False,
                message=message,
                a=flow_a,
                b=flow_b,
                c=flow_c,
                a_comp=flow_a + flow_c,
                error="hardware_not_ready",
            )
            self._handle_apply_result(result, pretest=pretest)
            if pretest:
                pretest.apply_safety_state("DATA_STALE", message, disabled=True)
            return result

        source = f"manual:{self._actuation_request_sequence + 1}"
        self._pending_flow_context[source] = {"kind": "apply"}
        return self._submit_flow_intent(
            mode="rest", a=flow_a, b=flow_b, c=flow_c, source=source
        )

    @Slot(object)
    def handle_breath_samples(
        self,
        samples: list | BreathSampleBatch,
        timestamp: float | None = None,
    ) -> None:
        structured = samples if isinstance(samples, BreathSampleBatch) else None
        values = [sample.value for sample in structured.samples] if structured else list(samples)
        if structured:
            timestamp = structured.samples[-1].timestamp if structured.samples else timestamp
        if timestamp is None:
            return
        if self.view:
            self.view.ingest_breath_samples(values, timestamp=timestamp)

        # Calibration Session Update (Story 2.6)
        if self.calibration_session.is_active:
            for sample in values:
                self.calibration_session.update(sample)

            # Check for completion
            if self.calibration_session.is_finished():
                result = self.calibration_session.stop()
                if result and self.view and hasattr(self.view, "calibration_view"):
                    self.view.calibration_view.set_calibration_state(False, "校准完成")
                    self.view.calibration_view.update_calibration_stats(
                        result.max_val, result.min_val, result.offset, result.gain
                    )

                    # Apply results (AC3) & Persist (AC5)
                    self.state.signal_offset = result.offset
                    self.state.signal_gain = result.gain
                    self.view.calibration_view.set_signal_transform(result.offset, result.gain)
                    if hasattr(self.view, "pretest_view"):
                        self.view.pretest_view.set_signal_transform(result.offset, result.gain)

                    self._persist_config_values({
                        "signal_offset": result.offset,
                        "signal_gain": result.gain
                    })
                    LOG.info(
                        "Calibration applied: Offset=%.3f, Gain=%.3f",
                        result.offset,
                        result.gain,
                    )
            elif self.view and hasattr(self.view, "calibration_view"):
                 # Update progress/stats
                 progress = self.calibration_session.get_progress()
                 remaining = self.calibration_session.duration_sec * (1 - progress)
                 self.view.calibration_view.set_calibration_state(
                     True, f"正在校准... {remaining:.1f}s"
                 )
                 self.view.calibration_view.set_calibration_progress(int(progress * 100))
                 self.view.calibration_view.update_calibration_stats(
                     self.calibration_session.current_max,
                     self.calibration_session.current_min
                 )

        sample_count = len(values)
        if sample_count == 0:
            return
        direct_sink_running = (
            getattr(self.worker, "_actuation_sink", None) is self.actuation_worker
            and self.worker.isRunning()
        )
        if not direct_sink_running:
            if structured is None:
                dt = 0.01
                start_ts = timestamp - ((sample_count - 1) * dt)
                start_ns = time.perf_counter_ns() - ((sample_count - 1) * 10_000_000)
                structured = BreathSampleBatch.from_frames(
                    tuple(
                        AnalogInputFrame(
                            timestamp=start_ts + index * dt,
                            ai0=float(value),
                            monotonic_ns=start_ns + index * 10_000_000,
                            ai_epoch=1,
                            sample_sequence=index,
                        )
                        for index, value in enumerate(values)
                    )
                )
            self.actuation_worker.post_ai_batch(
                structured,
                readiness=self._execution_readiness(),
            )
            self._drain_actuation_if_not_running()

    def handle_breath_metrics(self, payload: dict) -> None:
        warning = bool(payload.get("warning_flag"))
        reason = payload.get("reason") or ""
        log_payload = {
            "ts": payload.get("ts"),
            "fps_avg": payload.get("fps_avg"),
            "fps_p95": payload.get("fps_p95"),
            "fps_p05": payload.get("fps_p05"),
            "window_s": payload.get("window_s"),
            "sample_count": payload.get("sample_count"),
            "warning_flag": warning,
            "reason": reason,
        }
        self._breath_logger.info("breath_viz | %s", log_payload)
        if warning:
            self._breath_logger.warning("breath_viz_warning | %s", log_payload)
            message = (
                "波形渲染 FPS 低于 30，已记录"
                if reason == "fps_low"
                else "呼吸数据过期，等待新样本"
            )
            self.state.update_status(message)
            if self.view:
                self.view.update_status(self.state.status_message)

    @Slot(list, bool)
    def handle_self_check(self, results: list, hardware_ready: bool) -> None:
        if hardware_ready and self._unsafe_shutdown_latched:
            LOG.warning("Ignoring self-check ready while unsafe shutdown latch is set")
            hardware_ready = False
        self.state.update_self_check(results, hardware_ready)
        self.state.telemetry.connected = hardware_ready
        if hardware_ready:
            self._has_seen_connection = True
        self._connect_in_progress = False
        if hardware_ready:
            status = "硬件自检通过，已连接 SAFE"
        else:
            failing = next((r for r in results if getattr(r, "status", "") != "PASS"), None)
            reason = getattr(failing, "reason", "自检失败")
            suggestion = getattr(failing, "suggestion", "检查连接后重试")
            status = f"硬件自检失败：{reason}；建议：{suggestion}"
        self.state.update_status(status)
        if self.view:
            self.view.update_status(status)
            self.view.render_self_check(self.state.self_check_results, hardware_ready)
        if hardware_ready:
            self.actuation_interlock.update(
                connected=True,
                hardware_ready=True,
                safety_state=self.state.telemetry.safety_state,
                ttl_input_ready=bool(getattr(self.worker, "ttl_input_ready", False)),
            )
            self._publish_interlock_from_state()
            self.actuation_worker.post_readiness_update(
                readiness=self._execution_readiness(),
                timestamp=time.time(),
            )
            self._drain_actuation_if_not_running()
            self._reset_startup_flows_to_zero_async()
        else:
            self.actuation_interlock.update(
                connected=False,
                hardware_ready=False,
                ttl_input_ready=False,
            )
            self._publish_interlock_from_state()
            self.actuation_worker.post_readiness_update(
                readiness=self._execution_readiness(),
                timestamp=time.time(),
            )
            self._drain_actuation_if_not_running()
        self._refresh_toolbar_state()

    def request_self_check(self) -> None:
        """Trigger self-check from UI, keep thread-safe."""
        if self._unsafe_shutdown_latched:
            self._unsafe_shutdown_latched = False
        self.state.update_status("正在重新自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
        if not self.worker.isRunning():
            if not self.start_worker():
                return
        self.worker.request_self_check()

    def connect_hardware(self) -> None:
        if self._connect_in_progress:
            return

        if self._unsafe_shutdown_latched:
            # Clicking Connect is the explicit operator confirmation/retry.
            self._unsafe_shutdown_latched = False
        self._connect_in_progress = True
        self.state.update_status("正在连接硬件并执行自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
        self._start_or_request_self_check()
        self._refresh_toolbar_state()

    def _session_boundary_rejection(self) -> str:
        if self._pending_protocol_load is not None:
            return "协议加载仍在等待动作 owner 的安全清理确认，请等待加载完成。"
        if self._protocol_start_pending or self._protocol_master_prepare_pending:
            return "协议启动或主阀预备仍在等待硬件确认，请等待安全收敛。"
        if self._pretest_sequence_in_progress:
            return "预检序列仍在执行，请等待阀门和流量安全收敛。"
        pending_kinds = {
            str(context.get("kind", ""))
            for context in self._pending_plan_ui.values()
        }
        if pending_kinds & {
            "protocol_master_prepare",
            "pretest",
            "manual",
            "sequence",
        }:
            return "阀门动作计划仍在等待回执，请等待安全收敛。"
        protocol_state = self.actuation_worker.protocol_state
        if (
            protocol_state.pending_open_command_id is not None
            or protocol_state.pending_close_command_id is not None
        ):
            return "阀门动作仍在等待回执，请等待安全收敛。"
        open_channels = [
            channel
            for channel in self.valve_service.active_map()
            if self.valve_service.is_open(channel)
        ]
        if self.valve_service.master_is_open() or open_channels:
            return "主阀或手动阀门仍处于开启状态，请先执行安全关闭。"
        return ""

    @Slot(str, str, object)
    def handle_session_start_requested(
        self,
        subject: str,
        condition: str,
        output_dir: str | Path,
    ) -> bool:
        prepared = self.session_state.status == SessionStatus.PREPARED
        previous_writer = self.session_writer
        if previous_writer is not None and previous_writer.isRunning():
            self._set_session_status(
                "上一会话 writer 尚未终止，禁止建立新 generation；请等待安全收尾。"
            )
            return False
        if self.session_state.status in {
            SessionStatus.RECORDING,
            SessionStatus.CLOSING,
        }:
            message = "当前会话仍在活动或关闭中，请先完成结束流程。"
            self._set_session_status(message)
            return False
        document = self.state.loaded_protocol
        if document is None:
            self._set_session_status("请先加载有效协议，再开始会话。")
            return False
        if (
            self.actuation_worker.protocol_state.active_valve is not None
            or self.actuation_worker.protocol_state.possibly_open_valves
        ):
            self._set_session_status(
                "仍有活动或可能开启的阀门，请先执行安全停止再新建会话。"
            )
            return False
        boundary_rejection = self._session_boundary_rejection()
        if boundary_rejection:
            self._set_session_status(boundary_rejection)
            return False
        if prepared:
            descriptor = self.session_state.descriptor
            if (
                descriptor is None
                or self._session_protocol_document is not document
                or descriptor.subject_original != str(subject)
                or descriptor.condition_original != str(condition)
                or descriptor.paths.output_dir
                != Path(output_dir).expanduser().resolve(strict=False)
            ):
                self._set_session_status(
                    "已锁定的会话身份或协议已变化，禁止开始记录。"
                )
                return False
        else:
            self._session_generation += 1
            try:
                preview = self.session_file_service.preview(
                    output_dir=output_dir,
                    subject=subject,
                    condition=condition,
                )
                descriptor = self.session_file_service.reserve(
                    output_dir=output_dir,
                    subject=subject,
                    condition=condition,
                    generation=self._session_generation,
                    protocol_source=document.source_name,
                    protocol_metadata=dict(document.metadata),
                    preview=preview,
                )
            except SessionFileError as exc:
                self._set_session_status(str(exc))
                return False
            requires_confirmation = (
                self.view is not None and hasattr(self.view, "session_view")
            )
            if requires_confirmation:
                if not self.session_state.prepare(descriptor):
                    self._set_session_status("会话路径锁定失败，请重新尝试。")
                    return False
                self._session_protocol_document = document
                self._session_preview = None
                self._set_session_status(
                    "会话路径已锁定，请核对最终路径后点击“确认开始记录”。"
                )
                return True

        metrics_config = self.actuation_worker.metrics.config
        recording_started = datetime.now().astimezone()
        started_payload = {
            "recording_started_at": recording_started.isoformat(
                timespec="milliseconds"
            ),
            "declared_trigger_mode": (
                None
                if self.protocol_executor.state.declared_mode is None
                else self.protocol_executor.state.declared_mode.value
            ),
            "current_trigger_mode": (
                None
                if self.protocol_executor.state.current_mode is None
                else self.protocol_executor.state.current_mode.value
            ),
            "inhale_threshold": self.state.inhale_threshold,
            "exhale_threshold": self.state.exhale_threshold,
            "low_flow_threshold": self.state.low_flow_threshold,
            "hardware_variant": self.state.hardware_variant,
            "hardware_mode": (
                "simulation" if self.state.simulation_mode else "real"
            ),
            "ai_epoch_available": bool(getattr(self.worker, "_last_ai_epoch", -1) > 0),
            "actuation_quality_config": {
                "target_ms": metrics_config.target_ms,
                "single_limit_ms": metrics_config.single_limit_ms,
                "window_size": metrics_config.window_size,
                "min_samples": metrics_config.min_samples,
            },
        }
        writer = SessionWriterWorker(
            descriptor=descriptor,
            config=SessionWriterConfig.from_mapping(self.config),
            expected_producers=("hardware", "actuation", "controller"),
            session_started_payload=started_payload,
            readiness_latch=self.recorder_readiness,
            failure_callback=self._wake_actuation_for_recorder_failure,
        )
        ingress = SessionRecorderIngress(writer, self.recorder_readiness)
        writer.failure_ready.connect(self._handle_session_writer_failure)
        writer.finished.connect(
            lambda staging=descriptor.paths.staging_dir: (
                self.session_file_service.mark_inactive(staging)
            )
        )
        self.session_writer = writer
        if not writer.start_and_wait():
            failure = writer.failure
            message = (
                failure.message
                if failure is not None
                else "会话文件初始化失败，请检查输出目录。"
            )
            stopped = writer.wait(
                SessionWriterConfig.from_mapping(self.config).close_timeout_ms
            )
            if stopped:
                self.session_file_service.mark_inactive(
                    descriptor.paths.staging_dir
                )
            self.session_state.fail_start(
                descriptor,
                message,
                recovery_required=True,
            )
            self._session_preview = None
            self._set_session_status(message)
            return False
        boundary_rejection = self._session_boundary_rejection()
        if boundary_rejection:
            writer.fail_from_producer(
                stage="session_boundary_changed",
                message=boundary_rejection,
            )
            stopped = writer.wait(
                SessionWriterConfig.from_mapping(self.config).close_timeout_ms
            )
            if stopped:
                self.session_file_service.mark_inactive(
                    descriptor.paths.staging_dir
                )
            self.session_state.fail_start(
                descriptor,
                boundary_rejection,
                recovery_required=True,
            )
            self._session_preview = None
            self._set_session_status(boundary_rejection)
            return False
        if not self.actuation_worker.bind_session_recorder(
            ingress,
            generation=descriptor.generation,
            timeout_ms=SessionWriterConfig.from_mapping(
                self.config
            ).close_timeout_ms,
        ):
            writer.fail_from_producer(
                stage="actuation_recorder_bind",
                message="动作 owner 未确认 recorder bind，已拒绝开始会话。",
            )
            stopped = writer.wait(
                SessionWriterConfig.from_mapping(self.config).close_timeout_ms
            )
            if stopped:
                self.session_file_service.mark_inactive(
                    descriptor.paths.staging_dir
                )
            self.session_state.fail_start(
                descriptor,
                "动作 owner 未确认 recorder bind，已拒绝开始会话。",
                recovery_required=True,
            )
            self._session_preview = None
            self._set_session_status(self.session_state.failure_message)
            return False
        if not self.worker.bind_session_recorder(
            ingress,
            generation=descriptor.generation,
            timeout_ms=SessionWriterConfig.from_mapping(
                self.config
            ).close_timeout_ms,
        ):
            self.actuation_worker.post_recorder_fence(wait=True, timeout_ms=1000)
            writer.fail_from_producer(
                stage="hardware_recorder_bind",
                message="采集 owner 未确认 recorder bind，已拒绝开始会话。",
            )
            stopped = writer.wait(
                SessionWriterConfig.from_mapping(self.config).close_timeout_ms
            )
            if stopped:
                self.session_file_service.mark_inactive(
                    descriptor.paths.staging_dir
                )
            self.session_state.fail_start(
                descriptor,
                "采集 owner 未确认 recorder bind，已拒绝开始会话。",
                recovery_required=True,
            )
            self._session_preview = None
            self._set_session_status(self.session_state.failure_message)
            return False
        if not self.session_state.begin(descriptor):
            self.actuation_worker.post_recorder_fence(wait=True, timeout_ms=1000)
            self.worker.post_session_fence()
            writer.fail_from_producer(
                stage="state_transition",
                message="会话状态转换失败，未进入 recording。",
            )
            writer.wait(1000)
            self._set_session_status("会话状态转换失败，未进入 recording。")
            return False

        self.session_ingress = ingress
        self._session_protocol_document = document
        self._session_controller_sequence = 0
        self._session_controller_fenced = False
        self._session_close_pending_reason = ""
        self._session_finalize_started = False
        self._session_global_stop_in_progress = False
        self._session_finalize_event.clear()
        self._session_finalize_result = None
        self._session_preview = None
        self.actuation_interlock.update(
            recording_ready=True,
            recorder_generation=descriptor.generation,
            session_closing=False,
        )
        self.actuation_worker.post_recorder_ready(descriptor.generation)
        self._drain_actuation_if_not_running()
        if not self._post_controller_session_event(
            event="protocol_bound",
            source="session",
            result="success",
            message="当前协议已绑定到会话。",
            payload={
                "protocol_source": document.source_name,
                "protocol_metadata": dict(document.metadata),
            },
        ):
            return False
        self._render_protocol_execution_state()
        self._set_session_status(f"会话记录已开始：{descriptor.stem}")
        return True

    @Slot(str, str, str)
    def handle_session_preview_requested(
        self,
        subject: str,
        condition: str,
        output_dir: str,
    ) -> None:
        if self.session_state.status in {
            SessionStatus.PREPARED,
            SessionStatus.RECORDING,
            SessionStatus.CLOSING,
        }:
            self._render_session_snapshot()
            return
        if not output_dir:
            self._session_preview = None
            self._render_session_snapshot("请选择本地输出目录。")
            return
        output_path = Path(output_dir).expanduser().resolve(strict=False)
        if output_path.is_dir():
            self._start_recovery_scan(output_path)
        try:
            self._session_preview = self.session_file_service.preview(
                output_dir=output_path,
                subject=subject,
                condition=condition,
            )
        except SessionFileError as exc:
            self._session_preview = None
            self._render_session_snapshot(str(exc))
            return
        self._render_session_snapshot("文件名与路径预览已更新，尚未创建会话文件。")

    def _start_recovery_scan(self, output_path: Path) -> None:
        worker = self._recovery_scan_worker
        if worker is not None:
            self._pending_recovery_output = output_path
            return
        self._recovery_scan_request += 1
        worker = RecoveryScanWorker(
            self.session_file_service,
            output_path,
            self._recovery_scan_request,
        )
        self._recovery_scan_worker = worker
        worker.completed.connect(self._handle_recovery_scan_completed)
        worker.start()

    @Slot(object, object, object)
    def _handle_recovery_scan_completed(
        self,
        request_id: int,
        output_path: Path,
        result,
    ) -> None:
        completed_worker = self.sender()
        if not isinstance(completed_worker, RecoveryScanWorker):
            completed_worker = None
        if completed_worker is not None:
            completed_worker.wait(1000)
            completed_worker.deleteLater()
        if completed_worker is not self._recovery_scan_worker:
            return
        self._recovery_scan_worker = None
        if isinstance(result, Exception):
            self._session_recovery_messages = (
                f"恢复扫描失败：{result}。下一步：请检查输出目录权限。",
            )
        elif int(request_id) == self._recovery_scan_request:
            self._session_recovery_messages = tuple(
                (
                    f"发现未完成会话：{item.original_path}；原因：{item.reason}；"
                    + (
                        f"最后成功序号：{item.last_sequence}；"
                        if item.last_sequence is not None
                        else "最后成功序号：不可用；"
                    )
                    + (
                        f"已隔离至 {item.quarantined_path}。"
                        if item.quarantined_path is not None
                        else "无法移动，已原地保留 .session.part。"
                    )
                    + " 下一步：打开恢复目录或另存分析。"
                )
                for item in result
            )
            self._last_recovery_output = output_path
            self._last_recovery_location = None
            if result:
                latest = result[-1]
                self._last_recovery_location = (
                    latest.quarantined_path
                    if latest.quarantined_path is not None
                    else latest.original_path
                )
        self._render_session_snapshot()
        pending = self._pending_recovery_output
        self._pending_recovery_output = None
        if pending is not None:
            self._start_recovery_scan(pending)

    @Slot()
    def handle_session_recovery_requested(self) -> None:
        if self._last_recovery_location is not None:
            if self._last_recovery_location.exists():
                self._open_with_system(self._last_recovery_location)
                return
            self._set_session_status("记录的恢复位置已不存在，请重新扫描输出目录。")
            return
        if self._last_recovery_output is None:
            self._set_session_status("当前没有可打开的恢复目录。")
            return
        recovery = self._last_recovery_output / "recovery"
        if not recovery.is_dir():
            self._set_session_status("恢复目录不存在；不完整数据可能仍在原 .session.part。")
            return
        self._open_with_system(recovery)

    @Slot(str)
    def handle_session_end_requested(self, reason: str = "user_end") -> bool:
        if self.session_state.status == SessionStatus.PREPARED:
            descriptor = self.session_state.descriptor
            if descriptor is None:
                return False
            self.session_file_service.mark_inactive(
                descriptor.paths.staging_dir
            )
            message = (
                "已取消尚未开始记录的会话准备；未完成工作目录不会冒充完整会话，"
                "请从恢复位置检查或另存分析。"
            )
            if not self.session_state.cancel_prepared(message):
                return False
            self._session_protocol_document = None
            self._session_preview = None
            self._set_session_status(message)
            self._start_recovery_scan(descriptor.paths.output_dir)
            return True
        if not self.session_state.begin_close(reason):
            return False
        self._session_close_pending_reason = str(reason)
        self.actuation_interlock.update(
            recording_ready=False,
            session_closing=True,
        )
        self._set_session_status("会话正在安全结束并等待全部 producer fence。")
        active = self.protocol_executor.state.status in {
            ProtocolExecutionStatus.WAITING_TRIGGER,
            ProtocolExecutionStatus.WAITING_EXHALE,
            ProtocolExecutionStatus.TRIGGERED,
            ProtocolExecutionStatus.PAUSED,
            ProtocolExecutionStatus.BLOCKED,
        }
        boundary_rejection = self._session_boundary_rejection()
        if active or boundary_rejection:
            self.actuation_worker.post_stop(
                message=(
                    "会话结束请求已停止协议执行并等待全部阀门计划安全收敛。"
                )
            )
            self._drain_actuation_if_not_running()
        self._maybe_begin_session_finalization()
        return True

    def _maybe_begin_session_finalization(self) -> None:
        if (
            self.session_state.status != SessionStatus.CLOSING
            or self._session_global_stop_in_progress
            or self._session_boundary_rejection()
            or self.actuation_worker.protocol_state.active_valve is not None
            or self.actuation_worker.protocol_state.possibly_open_valves
            or self.protocol_executor.state.status
            in {
                ProtocolExecutionStatus.WAITING_TRIGGER,
                ProtocolExecutionStatus.WAITING_EXHALE,
                ProtocolExecutionStatus.TRIGGERED,
                ProtocolExecutionStatus.PAUSED,
            }
        ):
            return
        self._begin_session_finalization()

    def wait_for_session_finalization(self, timeout: float = 5.0):
        wait_seconds = max(0.0, float(timeout))
        started = time.monotonic()
        self._session_finalize_event.wait(wait_seconds)
        thread = self._session_finalize_thread
        if thread is not None and thread is not threading.current_thread():
            remaining = max(0.0, wait_seconds - (time.monotonic() - started))
            thread.join(remaining)
        return self._session_finalize_result

    def _post_controller_session_event(
        self,
        *,
        event: str,
        source: str,
        result: str,
        message: str,
        payload: dict | None = None,
    ) -> bool:
        ingress = self.session_ingress
        if ingress is None or self._session_controller_fenced:
            return False
        self._session_controller_sequence += 1
        return ingress.post_session_event(
            event=event,
            producer_sequence=self._session_controller_sequence,
            source=source,
            result=result,
            message=message,
            payload=payload,
        )

    def _begin_session_finalization(self) -> None:
        if self._session_finalize_started or self.session_writer is None:
            return
        self._session_finalize_started = True
        self._post_controller_session_event(
            event="session_ending",
            source="controller",
            result="success",
            message="会话已停止普通提交，正在等待 producer fence。",
            payload={"reason": self._session_close_pending_reason},
        )
        self.worker.post_session_fence()
        self.actuation_worker.post_recorder_fence()
        self._drain_actuation_if_not_running()
        if not self.actuation_worker.isRunning():
            self.actuation_worker.finalize_recorder_after_owner_stopped()
        if self.session_ingress is not None:
            self._session_controller_fenced = self.session_ingress.post_fence(
                "controller",
                producer_sequence=self._session_controller_sequence,
            )
            if not self._session_controller_fenced:
                self.session_writer.fail_from_producer(
                    stage="controller_fence",
                    message="Controller producer fence 提交失败，禁止发布会话。",
                )
                self._start_session_finalizer(
                    name="session-finalize-fence-failure"
                )
                return
        self._start_session_finalizer(name="session-finalize-waiter")

    def _start_session_finalizer(self, *, name: str) -> None:
        writer = self.session_writer
        descriptor = self.session_state.descriptor
        if writer is None or descriptor is None:
            return
        reason = self._session_close_pending_reason or "closed"
        thread = threading.Thread(
            target=self._finalize_session_writer,
            kwargs={
                "writer": writer,
                "descriptor": descriptor,
                "reason": reason,
            },
            name=name,
            daemon=False,
        )
        self._session_finalize_thread = thread
        thread.start()

    def _finalize_session_writer(
        self,
        *,
        writer: SessionWriterWorker,
        descriptor,
        reason: str,
    ) -> None:
        result = writer.close(
            reason=reason,
        )
        current = self.session_state.descriptor
        identity_matches = (
            self.session_writer is writer
            and current is not None
            and current.session_id == descriptor.session_id
            and current.generation == descriptor.generation
        )
        if not writer.isRunning():
            self.session_file_service.mark_inactive(
                descriptor.paths.staging_dir
            )
        if not identity_matches:
            LOG.warning(
                "Ignoring stale session finalizer result for %s/%s",
                descriptor.session_id,
                descriptor.generation,
            )
            return
        self._session_finalize_result = result
        if result.complete:
            self.session_state.mark_closed(descriptor.paths.final_dir)
        else:
            self.session_state.fail(result.message, recovery_required=True)
        self.actuation_interlock.update(
            recording_ready=False,
            session_closing=False,
        )
        self._session_finalized.emit(result)
        self._session_finalize_event.set()

    def _wake_actuation_for_recorder_failure(self, failure) -> None:
        self.actuation_interlock.update(
            recording_ready=False,
            recorder_failed=True,
            recorder_generation=failure.session_generation,
        )
        self.session_state.fail(failure.message, recovery_required=True)
        self.actuation_worker.post_recorder_failed(failure.message)
        self.actuation_worker.post_stop(
            message="会话写入失败，Controller 已请求安全停止。"
        )
        self.worker.post_session_fence()
        self.actuation_worker.post_recorder_fence()

    @Slot(object)
    def _handle_session_writer_failure(self, failure) -> None:
        self._set_session_status(failure.message)
        if self.view and hasattr(self.view, "render_actuation_alert"):
            self.view.render_actuation_alert(failure.message, severe=True)

    @Slot(object)
    def _handle_session_finalized(self, result) -> None:
        message = (
            f"会话已完整发布：{result.final_dir}"
            if result.complete
            else result.message
        )
        self._set_session_status(message)
        self._render_protocol_execution_state()

    def _set_session_status(self, message: str) -> None:
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
        self._render_session_snapshot(message)

    def _render_session_snapshot(self, message: str | None = None) -> None:
        if self.view is None or not hasattr(self.view, "session_view"):
            return
        status = self.session_state.status
        descriptor = self.session_state.descriptor
        active = status in {SessionStatus.RECORDING, SessionStatus.CLOSING}
        inputs_locked = active or status == SessionStatus.PREPARED
        preview = self._session_preview
        if descriptor is not None and (
            inputs_locked or preview is None
        ):
            subject_original = descriptor.subject_original
            subject_clean = descriptor.subject_clean
            condition_original = descriptor.condition_original
            condition_clean = descriptor.condition_clean
            stem = descriptor.stem
            staging_path = str(descriptor.paths.staging_dir)
            final_path = str(descriptor.paths.final_dir)
            raw_path = str(descriptor.paths.final_raw_path)
            log_path = str(descriptor.paths.final_log_path)
            session_id = descriptor.session_id
            generation = descriptor.generation
        elif preview is not None:
            subject_original = preview.subject_original
            subject_clean = preview.subject_clean
            condition_original = preview.condition_original
            condition_clean = preview.condition_clean
            stem = preview.stem
            staging_path = str(preview.staging_dir)
            final_path = str(preview.final_dir)
            raw_path = str(preview.final_raw_path)
            log_path = str(preview.final_log_path)
            session_id = ""
            generation = 0
        else:
            subject_original = subject_clean = ""
            condition_original = condition_clean = ""
            stem = staging_path = final_path = raw_path = log_path = ""
            session_id = ""
            generation = 0
        has_protocol = self.state.loaded_protocol is not None
        safe_to_start = (
            (
                status == SessionStatus.PREPARED
                and descriptor is not None
                or preview is not None
            )
            and has_protocol
            and not active
            and not self._session_boundary_rejection()
            and self.actuation_worker.protocol_state.active_valve is None
            and not self.actuation_worker.protocol_state.possibly_open_valves
            and not self._unsafe_shutdown_latched
        )
        if message is not None:
            self._session_display_message = message
        elif self._session_display_message:
            message = self._session_display_message
        else:
            message = {
                SessionStatus.IDLE: "请填写会话信息并预览路径。",
                SessionStatus.PREPARED: "会话路径已锁定，请确认开始记录。",
                SessionStatus.RECORDING: "会话正在记录；协议与路径已冻结。",
                SessionStatus.CLOSING: "会话正在等待安全关闭与 producer fence。",
                SessionStatus.CLOSED: "会话已完整发布，可以预览并新建会话。",
                SessionStatus.FAILED: self.session_state.failure_message,
                SessionStatus.RECOVERY_REQUIRED: self.session_state.failure_message,
            }[status]
        self.view.session_view.render_snapshot(
            SessionViewSnapshot(
                status=status,
                status_text=message,
                session_id=session_id,
                generation=generation,
                subject_original=subject_original,
                subject_clean=subject_clean,
                condition_original=condition_original,
                condition_clean=condition_clean,
                stem=stem,
                staging_path=staging_path,
                final_path=final_path,
                raw_path=raw_path,
                log_path=log_path,
                can_start=safe_to_start,
                can_end=status
                in {SessionStatus.PREPARED, SessionStatus.RECORDING},
                inputs_enabled=not inputs_locked,
                has_protocol=has_protocol,
                recovery_messages=self._session_recovery_messages,
            )
        )

    @Slot(str)
    def handle_protocol_file_selected(self, path: str | Path) -> bool:
        if (
            self.session_state.descriptor is not None
            and self.session_state.status
            not in {SessionStatus.IDLE, SessionStatus.CLOSED}
        ):
            message = "活动或失败会话仍绑定当前协议，请先安全结束当前会话。"
            self._set_session_status(message)
            return False
        try:
            document = parse_protocol_file(path, valve_map=self.state.get_active_valve_map())
        except ProtocolParseError as exc:
            message = f"协议解析失败：{exc}"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
                if hasattr(self.view, "protocol_view"):
                    self.view.protocol_view.render_error(exc)
            LOG.warning("Protocol parse failed | path=%s | error=%s", path, exc)
            return False

        if not self.actuation_worker.isRunning() and not self._allow_test_actuation_bridge:
            message = "动作 worker 未运行，协议未加载。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.error("ActuationWorker unavailable; refusing protocol load")
            return False

        if self._pending_protocol_load is not None:
            message = "已有协议正在等待安全清理确认，请稍后再加载。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            return False
        self._pending_protocol_load = document
        self._last_document_load_success = None
        self.actuation_worker.post_load(document)
        self._drain_actuation_if_not_running()
        if self._last_document_load_success is not None:
            return self._last_document_load_success
        message = f"协议已解析，正在等待安全清理后加载：{document.source_name}"
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
        return True

    @Slot()
    def handle_protocol_start_requested(self) -> None:
        if self._unsafe_shutdown_latched:
            self._block_for_unsafe_shutdown(
                "上次关闭失败尚未人工确认；禁止预备主阀或启动协议。"
            )
            return
        session_ready = (
            self.session_state.status == SessionStatus.RECORDING
            and self.recorder_readiness.read().recording_ready
            and self._session_protocol_document is not None
            and self._session_protocol_document is self.state.loaded_protocol
        )
        if not session_ready and not (
            self._allow_test_actuation_bridge
            and self.session_state.descriptor is None
        ):
            message = (
                "协议开始被拒绝：请先成功建立 recording 会话，"
                "并确认当前协议已绑定；在此之前 owner worker 不会接单。"
            )
            self._set_session_status(message)
            return
        if not self.actuation_worker.isRunning() and not self._allow_test_actuation_bridge:
            message = "动作 worker 未运行，已保守阻止协议开始。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.error("ActuationWorker unavailable; refusing protocol start")
            return
        if not self.flow_worker.isRunning() and not self._allow_test_actuation_bridge:
            message = "流量 worker 未运行，已保守阻止协议开始。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.error("FlowWorker unavailable; refusing protocol start")
            return
        if self._protocol_start_pending or self._protocol_master_prepare_pending:
            message = "协议启动或主阀预备仍在等待硬件确认，请勿重复提交。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            return
        open_channels = [
            channel
            for channel in self.valve_service.active_map()
            if self.valve_service.is_open(channel)
        ]
        if open_channels:
            message = (
                "检测到手动/预检阀仍处于开启状态，已阻止协议开始；"
                f"请先安全关闭阀门：{open_channels}"
            )
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.warning("Protocol start blocked by open manual valves: %s", open_channels)
            return
        document = (
            self.state.loaded_protocol
            if not self._protocol_snapshot.has_protocol
            else None
        )
        readiness = self._execution_readiness()
        reason = readiness.rejection_reason(
            has_protocol=bool(document or self._protocol_snapshot.has_protocol)
        )
        if reason:
            self.state.update_status(reason)
            if self.view:
                self.view.update_status(reason)
            return
        if (
            self.actuation_worker.protocol_state.active_valve is None
            and not self.actuation_worker.protocol_state.possibly_open_valves
        ):
            self.actuation_interlock.clear_unsafe_latch()
        ok, plan_or_message = self.valve_service.plan_master_prepare(
            safety_state=self._build_current_safety_state()
        )
        if not ok:
            message = str(plan_or_message)
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.warning("Protocol start blocked by master preparation guard: %s", message)
            return
        assert isinstance(plan_or_message, ValveWritePlan)
        if plan_or_message.steps:
            self._protocol_master_prepare_pending = True
            message = "正在预备主阀；仅在硬件成功回执后布防协议。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            self._submit_valve_plan(
                plan_or_message,
                category=ActuationCategory.WARMUP,
                ui_context={
                    "kind": "protocol_master_prepare",
                    "document": document,
                    "session_id": (
                        None
                        if self.session_state.descriptor is None
                        else self.session_state.descriptor.session_id
                    ),
                    "session_generation": (
                        0
                        if self.session_state.descriptor is None
                        else self.session_state.descriptor.generation
                    ),
                },
            )
            return
        self._begin_protocol_start(document=document)

    def _begin_protocol_start(self, *, document) -> None:
        """Acquire the serial lease and arm only after master preparation is confirmed."""
        session_reason = self._protocol_start_session_rejection(
            document=document,
            session_id=(
                None
                if self.session_state.descriptor is None
                else self.session_state.descriptor.session_id
            ),
            session_generation=(
                0
                if self.session_state.descriptor is None
                else self.session_state.descriptor.generation
            ),
        )
        if session_reason:
            self._set_session_status(session_reason)
            return
        readiness = self._execution_readiness()
        reason = readiness.rejection_reason(
            has_protocol=bool(document or self._protocol_snapshot.has_protocol)
        )
        if reason:
            self.state.update_status(reason)
            if self.view:
                self.view.update_status(reason)
            return
        lease_epoch = int(self._protocol_snapshot.execution_epoch)
        if not self.flow_worker.acquire_protocol_lease(lease_epoch):
            message = "仍有流量命令正在写入硬件，已阻止协议开始；请等待串口确认后重试。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.warning("Protocol start blocked by in-flight flow command")
            return
        self._protocol_lease_epoch = lease_epoch
        self._protocol_start_pending = True
        self._publish_interlock_from_state(device_lease="protocol")
        self.actuation_worker.post_start(
            document=document,
            readiness=self._execution_readiness(),
            lease_epoch=lease_epoch,
        )
        self._drain_actuation_if_not_running()

    def _protocol_start_session_rejection(
        self,
        *,
        document,
        session_id: str | None,
        session_generation: int,
    ) -> str:
        if self._allow_test_actuation_bridge and self.session_state.descriptor is None:
            return ""
        descriptor = self.session_state.descriptor
        readiness = self.recorder_readiness.read()
        if (
            descriptor is None
            or self.session_state.status != SessionStatus.RECORDING
            or not readiness.recording_ready
            or readiness.failed
            or descriptor.session_id != session_id
            or descriptor.generation != int(session_generation)
            or readiness.session_id != descriptor.session_id
            or readiness.generation != descriptor.generation
            or self._session_protocol_document is not self.state.loaded_protocol
            or (
                document is not None
                and document is not self._session_protocol_document
            )
        ):
            return (
                "主阀预备完成后会话身份、generation、协议或 recording readiness "
                "已变化，协议未布防。"
            )
        return ""

    @Slot()
    def handle_protocol_manual_trigger_requested(self) -> None:
        self.actuation_worker.post_manual_trigger(readiness=self._execution_readiness())
        self._drain_actuation_if_not_running()

    @Slot(str)
    def handle_protocol_trigger_mode_requested(self, mode: str) -> None:
        self.actuation_worker.post_mode(mode)
        self._drain_actuation_if_not_running()

    @Slot(object)
    def handle_ttl_pulse(self, pulse: object) -> None:
        if not isinstance(pulse, TtlPulse):
            LOG.warning("忽略无效 TTL pulse payload：%r", pulse)
            return
        if not (
            getattr(self.worker, "_actuation_sink", None) is self.actuation_worker
            and self.worker.isRunning()
        ):
            self.actuation_worker.post_ttl_pulse(
                pulse,
                readiness=self._execution_readiness(),
            )
            self._drain_actuation_if_not_running()

    @Slot(str)
    def handle_ttl_input_error(self, message: str) -> None:
        if not (
            getattr(self.worker, "_actuation_sink", None) is self.actuation_worker
            and self.worker.isRunning()
        ):
            self.actuation_worker.post_input_error(message)
            self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_rearm_requested(self) -> None:
        self.actuation_worker.post_rearm()
        self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_pause_requested(self) -> None:
        self.actuation_worker.post_pause()
        self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_resume_requested(self) -> None:
        self.actuation_worker.post_resume()
        self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_stop_requested(self) -> None:
        if self.session_state.status == SessionStatus.RECORDING:
            self.handle_session_end_requested("protocol_stopped")
            return
        self.actuation_worker.post_stop()
        self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_next_requested(self) -> None:
        self.actuation_worker.post_skip()
        self._drain_actuation_if_not_running()

    @Slot()
    def handle_protocol_executor_tick(self) -> None:
        self._render_protocol_execution_state()

    def reset_hardware(self) -> None:
        # 允许在未 ready 时执行 reset，用于恢复异常状态；仍需安全检查。
        if not self.ensure_safe_command("Reset", source="UI"):
            self._refresh_toolbar_state()
            return

        self.state.update_status("正在重置硬件：先安全关阀，再自检重联")
        if self.view:
            self.view.update_status(self.state.status_message)

        finalize_session = self._prepare_session_for_global_stop("reset")
        self.actuation_worker.post_stop(message="硬件重置请求已停止门控流程。")
        self._drain_actuation_if_not_running()
        event = self.shutdown_service.shutdown(
            source="reset",
            reason="reset_request",
            force=True,
        )
        self._handle_shutdown_event(event, success_message="重置完成：阀门关闭，准备重新自检")
        if finalize_session:
            self._finish_session_after_global_stop(event)

        if event.get("result") != "success":
            self._block_for_unsafe_shutdown(
                "重置关闭失败，物理阀状态未知；请人工检查后点击连接明确重试。"
            )
            self._refresh_toolbar_state()
            return

        if hasattr(self, "valve_service"):
            self.valve_service.reset_cached_state()
        if self.view and hasattr(self.view, "pretest_view"):
            self.view.pretest_view.reset_valve_selection()
        if hasattr(self.worker, "mark_disconnected"):
            self.worker.mark_disconnected()
        self.state.telemetry.connected = False
        self.state.hardware_ready = False
        self._connect_in_progress = True
        self.state.update_status("重置完成：正在重新初始化硬件并自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
            self.view.render_telemetry(self.state.telemetry)
        self._start_or_request_self_check()
        self._refresh_toolbar_state()

    def _start_or_request_self_check(self) -> None:
        was_running = self.worker.isRunning()
        if not self.start_worker():
            self._connect_in_progress = False
            self._refresh_toolbar_state()
            return
        if was_running:
            self.worker.request_self_check()

    def stop_hardware(self) -> None:
        finalize_session = self._prepare_session_for_global_stop("global_stop")
        self.actuation_worker.post_stop(message="用户停止硬件，门控流程已停止。")
        self._drain_actuation_if_not_running()
        self.state.update_status("正在安全停止，关闭阀门并释放资源...")
        if self.view:
            self.view.update_status(self.state.status_message)
        event = self.shutdown_service.shutdown(
            source="stop",
            reason="user_stop",
            force=True,
        )
        self._handle_shutdown_event(event, success_message="已停止/已关闭阀门")
        if finalize_session:
            self._finish_session_after_global_stop(event)
        if hasattr(self, "valve_service"):
            self.valve_service.reset_cached_state()
        self._connect_in_progress = False
        self.state.telemetry.connected = False
        self.state.hardware_ready = False
        if self.view:
            self.view.render_telemetry(self.state.telemetry)
        self._refresh_toolbar_state()

    def open_help_manual(self) -> None:
        manual_path_value = self.state.manual_path
        if not manual_path_value:
            message = "未配置本地手册路径，请在配置文件设置 manual_path。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            return

        manual_path = (
            manual_path_value if isinstance(manual_path_value, Path) else Path(manual_path_value)
        )
        if not manual_path.is_absolute() and self.state.config_path:
            manual_path = Path(self.state.config_path).parent.parent / manual_path

        if not manual_path.exists():
            message = "未找到本地手册，请检查安装包或 docs 目录。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            return

        try:
            self._open_with_system(manual_path)
            message = f"已打开帮助文档：{manual_path}"
        except Exception as exc:  # pragma: no cover - defensive
            message = f"打开手册失败: {exc}"

        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
        self._refresh_toolbar_state()

    def _open_with_system(self, path: Path) -> None:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return
        subprocess.Popen(["xdg-open", str(path)])

    def _resolve_record_path(self, config: dict) -> Path | None:
        path_value = config.get("shutdown_record_path")
        if not path_value:
            return None
        candidate = Path(path_value)
        if not candidate.is_absolute() and self.state.config_path:
            anchor = Path(self.state.config_path).parent
            if anchor.name == "config":
                anchor = anchor.parent
            candidate = anchor / path_value
        return candidate

    def ensure_hardware_ready(self, action: str) -> bool:
        """Gate hardware commands based on self-check readiness."""
        if self.state.hardware_ready:
            return True
        warning = f"硬件自检未通过，阻断 {action}"
        self.state.update_status(warning)
        if self.view:
            self.view.update_status(warning)
        LOG.warning(warning)
        return False

    def _build_current_safety_state(self) -> SafetyState:
        telemetry = self.state.telemetry
        return SafetyState(
            state=telemetry.safety_state,
            airflow=telemetry.airflow,
            threshold=self.state.low_flow_threshold,
            updated_at=telemetry.timestamp,
            reason=telemetry.safety_reason,
        )

    @staticmethod
    def _reject_synchronous_protocol_write(
        channel_id: int, open_state: bool
    ) -> tuple[bool, str]:
        del channel_id, open_state
        raise RuntimeError("生产协议动作只能由 ActuationWorker 异步执行。")

    def _publish_protocol_result(
        self,
        result: ProtocolExecutorResult,
        *,
        render_even_without_events: bool = False,
    ) -> None:
        if result.transitions:
            last_transition = result.transitions[-1]
            self.state.telemetry.gating_state = last_transition.state
            if self.view:
                self.view.update_gating_state(last_transition.state)
            for transition in result.transitions:
                self._breath_logger.info(
                    {
                        "event": "threshold_cross",
                        "ts": transition.timestamp,
                        "monotonic_ns": transition.monotonic_ns,
                        "sample_value": transition.sample_value,
                        "gate_state": transition.state,
                        "inhale": self.state.inhale_threshold,
                        "exhale": self.state.exhale_threshold,
                        "safety_state": transition.safety_state,
                    }
                )
        if result.events:
            for event in result.events:
                self._protocol_logger.info("protocol_gate | %s", event.as_dict())
                if event.warning and event.message and self.view and hasattr(
                    self.view, "render_actuation_alert"
                ):
                    self.view.render_actuation_alert(event.message, severe=event.severe)
            message = result.events[-1].message
            if message:
                self.state.update_status(message)
                if self.view:
                    self.view.update_status(self.state.status_message)
        if result.events or render_even_without_events:
            self._render_protocol_execution_state()
        self._publish_interlock_from_state()

    @Slot(object)
    def _handle_protocol_start_result(self, ack) -> None:
        """Settle only the explicit start acknowledgement, never unrelated results."""
        if not self._protocol_start_pending or self._protocol_lease_epoch is None:
            return
        if ack.lease_epoch != self._protocol_lease_epoch:
            LOG.warning(
                "Ignoring stale protocol start ack: held=%s ack_lease=%s",
                self._protocol_lease_epoch,
                ack.lease_epoch,
            )
            return
        if (
            bool(ack.accepted)
            and ack.execution_epoch > self._protocol_lease_epoch
        ):
            if self.flow_worker.acquire_protocol_lease(ack.execution_epoch):
                self._protocol_lease_epoch = ack.execution_epoch
            else:
                message = "流量 owner 无法同步协议 execution epoch；协议已请求安全停止。"
                LOG.critical(
                    "%s target=%s",
                    message,
                    ack.execution_epoch,
                )
                self.state.update_status(message)
                released = self.flow_worker.release_protocol_lease(
                    self._protocol_lease_epoch,
                    next_execution_epoch=ack.execution_epoch,
                )
                if not released:
                    LOG.critical("Failed to release the unsynchronized FlowWorker lease")
                self._protocol_lease_epoch = None
                self.actuation_interlock.update(
                    connected=False,
                    flow_setpoints_ready=False,
                    device_lease="idle" if released else "protocol",
                )
                self.actuation_worker.post_interlock_changed(timestamp=time.time())
                self.actuation_worker.post_stop(message=message)
        else:
            self.flow_worker.release_protocol_lease(
                self._protocol_lease_epoch,
                next_execution_epoch=ack.execution_epoch,
            )
            self._protocol_lease_epoch = None
        self._protocol_start_pending = False
        self._publish_interlock_from_state()

    def _execution_readiness(self) -> ProtocolExecutionReadiness:
        return ProtocolExecutionReadiness(
            connected=bool(self.state.telemetry.connected),
            hardware_ready=bool(self.state.hardware_ready),
            flow_setpoints_ready=bool(self.state.flow_setpoints_ready),
            safety_state=self.state.telemetry.safety_state,
            ttl_input_ready=bool(getattr(self.worker, "ttl_input_ready", False)),
        )

    def _publish_interlock_from_state(self, *, device_lease: str | None = None) -> None:
        current = self.actuation_interlock.read()[1]
        lease = device_lease or (
            "protocol" if self._protocol_lease_epoch is not None else "idle"
        )
        self.actuation_interlock.update(
            has_protocol=bool(
                self.state.loaded_protocol or self._protocol_snapshot.has_protocol
            ),
            device_lease=lease or current.device_lease,
        )

    def _drain_actuation_if_not_running(self) -> None:
        if self.actuation_worker.isRunning():
            return
        if self.worker.isRunning():
            message = "动作 worker 未运行，已保守阻止请求；请停止并重新连接硬件。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.error("ActuationWorker unavailable; refusing UI-thread DO ownership")
            return
        if self._allow_test_actuation_bridge:
            self.actuation_worker.process_ready_with_do_ownership()

    def _submit_flow_intent(
        self,
        *,
        mode: str,
        a: float,
        b: float,
        c: float,
        source: str,
    ) -> FlowApplyResult:
        self._actuation_request_sequence += 1
        self._last_flow_result = None
        effective_c = 0.0 if mode == "stim_start" else float(c)
        self.actuation_worker.post_flow_intent(
            mode=mode,
            a=float(a),
            b=float(b),
            c=effective_c,
            source=source,
        )
        self._drain_actuation_if_not_running()
        if not self.flow_worker.isRunning() and self._allow_test_actuation_bridge:
            self.flow_worker.process_ready()
            self._drain_actuation_if_not_running()
        elif not self.flow_worker.isRunning():
            return FlowApplyResult(
                False,
                "流量 worker 未运行，已保守阻止串口命令。",
                float(a),
                float(b),
                effective_c,
                float(a) + (effective_c if mode in {"rest", "zero"} else 0.0),
                "worker_unavailable",
            )
        if self._last_flow_result is not None:
            return self._last_flow_result
        return FlowApplyResult(
            True,
            "流量变更已提交，等待串口确认。",
            float(a),
            float(b),
            effective_c,
            float(a) + (effective_c if mode in {"rest", "zero"} else 0.0),
        )

    def _submit_valve_plan(
        self,
        plan: ValveWritePlan,
        *,
        category: ActuationCategory,
        ui_context: dict,
    ) -> str:
        self._actuation_request_sequence += 1
        request_id = f"valve-plan-{self._actuation_request_sequence}"
        self._pending_plan_ui[request_id] = dict(ui_context)
        self.actuation_worker.post_valve_plan(
            plan,
            category=category,
            request_id=request_id,
        )
        self._drain_actuation_if_not_running()
        return "阀门动作已提交，等待硬件确认。"

    @Slot(object)
    def _handle_flow_command_result(self, wrapped) -> None:
        result = wrapped.result
        context = self._pending_flow_context.pop(wrapped.command.source, {})
        kind = context.get("kind")
        if getattr(wrapped, "stale", False):
            message = result.message or "旧 execution epoch 的流量命令已取消。"
            if kind == "startup_zero" and result.success:
                # A confirmed zero-flow write remains a valid conservative
                # startup outcome even if protocol readiness advanced while
                # the serial device was completing the bounded write.
                self._startup_zero_completed.emit(result)
                return
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            if kind == "apply" and self.view and hasattr(self.view, "pretest_view"):
                self.view.pretest_view.set_applying(False)
                self.view.pretest_view.set_flow_message(message)
            elif kind == "pretest":
                self._pretest_sequence_completed.emit(
                    str(context.get("mode", "")),
                    list(context.get("channels", [])),
                    result,
                    False,
                    message,
                )
            return
        self._last_flow_result = result
        self.state.flow_setpoints_ready = bool(result.success)
        if kind == "startup_zero":
            self._startup_zero_completed.emit(result)
            return
        if kind == "apply":
            pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None
            self._handle_apply_result(result, pretest=pretest)
            return
        if kind != "pretest":
            return
        mode = str(context["mode"])
        channels = [int(channel) for channel in context["channels"]]
        if not result.success:
            self._pretest_sequence_completed.emit(mode, channels, result, False, result.message)
            return
        desired_open = mode == "stim_start"
        safety_state = self._build_current_safety_state()
        combined_steps = []
        master_planned = False
        for channel_id in channels:
            ok, plan_or_message = self.valve_service.plan_valve(
                channel_id,
                desired_open,
                safety_state=safety_state,
            )
            if not ok:
                self._pretest_sequence_completed.emit(
                    mode, channels, result, False, str(plan_or_message)
                )
                return
            assert isinstance(plan_or_message, ValveWritePlan)
            for step in plan_or_message.steps:
                if step.role == "master_prepare" and master_planned:
                    continue
                if step.role == "master_prepare":
                    master_planned = True
                combined_steps.append(step)
        if not combined_steps:
            self._pretest_sequence_completed.emit(mode, channels, result, True, result.message)
            return
        self._submit_valve_plan(
            ValveWritePlan(
                requested_valve=channels[0],
                requested_state=desired_open,
                safety_close=False,
                steps=tuple(combined_steps),
            ),
            category=ActuationCategory.PRETEST,
            ui_context={
                "kind": "pretest",
                "mode": mode,
                "channels": channels,
                "flow_result": result,
            },
        )

    @Slot(object)
    def _handle_actuation_receipt(self, receipt) -> None:
        self.valve_service.commit_receipt(receipt)
        self._protocol_logger.info(
            "actuation_receipt | %s",
            {
                "command_id": receipt.command_id,
                "execution_epoch": receipt.execution_epoch,
                "arm_epoch": receipt.arm_epoch,
                "sequence": receipt.sequence,
                "trial_id": receipt.trial_id,
                "trial_index": receipt.trial_index,
                "valve": receipt.valve,
                "action": receipt.action.value,
                "category": receipt.category.value,
                "expected_ns": receipt.expected_ns,
                "started_ns": receipt.started_ns,
                "actual_ns": receipt.actual_ns,
                "offset_ms": receipt.offset_ms,
                "jitter_ms": receipt.jitter_ms,
                "result": receipt.result.value,
                "measurement_point": receipt.measurement_point,
                "stale": receipt.stale,
            },
        )
        self._render_protocol_execution_state()
        if self.view and hasattr(self.view, "render_actuation_alert"):
            severe = bool(
                receipt.jitter_ms is not None
                and receipt.jitter_ms
                > self.actuation_worker.metrics.config.single_limit_ms
            )
            if severe or receipt.result.value != "success":
                message = receipt.message or (
                    "阀门时序严重超限；确认全部阀门关闭后重新布防。"
                    if severe
                    else "阀门动作失败；请检查硬件并执行安全停止。"
                )
                self.view.render_actuation_alert(message, severe=True)

    @Slot(object)
    def _handle_actuation_plan_result(self, result: dict) -> None:
        context = self._pending_plan_ui.pop(str(result.get("request_id", "")), {})
        message = str(result.get("message", ""))
        if context.get("kind") == "pretest":
            flow_result = context.get("flow_result")
            self._pretest_sequence_completed.emit(
                str(context.get("mode", "")),
                list(context.get("channels", [])),
                flow_result,
                bool(result.get("success")),
                message,
            )
        elif context.get("kind") == "protocol_master_prepare":
            self._protocol_master_prepare_pending = False
            if bool(result.get("success")):
                rejection = self._protocol_start_session_rejection(
                    document=context.get("document"),
                    session_id=context.get("session_id"),
                    session_generation=int(context.get("session_generation", 0)),
                )
                if rejection:
                    message = (
                        f"{rejection}；晚到的主阀预备成功已触发补偿安全关闭。"
                    )
                    self.actuation_worker.post_stop(message=message)
                    self._drain_actuation_if_not_running()
                else:
                    self._begin_protocol_start(document=context.get("document"))
            else:
                message = message or "主阀预备失败，协议未布防。"
        elif context.get("kind") in {"manual", "sequence"}:
            pretest = (
                self.view.pretest_view
                if self.view and hasattr(self.view, "pretest_view")
                else None
            )
            if pretest is not None:
                for channel in context.get("channels", []):
                    channel_id = int(channel)
                    pretest.set_valve_state(
                        channel_id,
                        self.valve_service.is_open(channel_id),
                    )
                pretest.set_master_state(self.valve_service.master_is_open())
                pretest.show_warning("" if bool(result.get("success")) else message)
        if message:
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
        self._maybe_begin_session_finalization()

    @Slot(object)
    def _handle_protocol_snapshot(self, snapshot) -> None:
        session_ready = (
            self.session_state.status == SessionStatus.RECORDING
            and self.recorder_readiness.read().recording_ready
            and self._session_protocol_document is self.state.loaded_protocol
        )
        if snapshot.can_start and not session_ready:
            snapshot = replace(
                snapshot,
                can_start=False,
                readiness_reason="请先在“文件”页成功建立 recording 会话。",
            )
        self._protocol_snapshot = snapshot
        active = snapshot.status.value in {
            "waiting_trigger",
            "waiting_exhale",
            "triggered",
            "paused",
            "blocked",
        }
        if active:
            # The controller token names the epoch actually held by FlowWorker.
            # A later protocol snapshot cannot transfer that lease by itself.
            pass
        elif (
            self._protocol_lease_epoch is not None
            and not self._protocol_start_pending
            and snapshot.execution_epoch >= self._protocol_lease_epoch
        ):
            released = self.flow_worker.release_protocol_lease(
                self._protocol_lease_epoch,
                next_execution_epoch=snapshot.execution_epoch,
            )
            if released:
                self._protocol_lease_epoch = None
            else:
                message = "流量 owner 租约释放失败；设备保持保守阻断。"
                LOG.critical(
                    "%s held=%s terminal=%s",
                    message,
                    self._protocol_lease_epoch,
                    snapshot.execution_epoch,
                )
                self.state.update_status(message)
                self.actuation_interlock.update(
                    connected=False,
                    flow_setpoints_ready=False,
                    device_lease="protocol",
                )
        self._publish_interlock_from_state()
        if self.view and hasattr(self.view, "protocol_view"):
            self.view.protocol_view.render_execution_state(snapshot)
        self._render_session_snapshot()
        if (
            snapshot.status == ProtocolExecutionStatus.COMPLETED
            and self.session_state.status == SessionStatus.RECORDING
        ):
            self.handle_session_end_requested("protocol_completed")
        else:
            self._maybe_begin_session_finalization()

    @Slot(object)
    def _handle_document_result(self, result: dict) -> None:
        document = result.get("document")
        success = bool(result.get("success"))
        pending = self._pending_protocol_load
        if document is not pending:
            message = "已忽略迟到或不属于当前请求的协议加载回执。"
            LOG.warning(
                "%s pending=%s result=%s",
                message,
                getattr(pending, "source_name", None),
                getattr(document, "source_name", None),
            )
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            self._render_session_snapshot()
            return

        self._last_document_load_success = success
        self._pending_protocol_load = None
        active_session = self.session_state.status in {
            SessionStatus.PREPARED,
            SessionStatus.RECORDING,
            SessionStatus.CLOSING,
        }
        if success and active_session:
            self._last_document_load_success = False
            bound_document = self._session_protocol_document
            message = "活动会话已锁定协议；已拒绝迟到的协议加载成功回执。"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
            LOG.error(
                "%s bound=%s rejected=%s",
                message,
                getattr(bound_document, "source_name", None),
                getattr(document, "source_name", None),
            )
            if bound_document is not None and bound_document is not document:
                self.actuation_worker.post_load(bound_document)
                self._drain_actuation_if_not_running()
            self._render_session_snapshot()
            return
        if success:
            self.state.loaded_protocol = document
            self._publish_interlock_from_state()
            message = f"协议加载成功：{document.source_name}，共 {len(document.trials)} 个 trial"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(message)
                if hasattr(self.view, "protocol_view"):
                    self.view.protocol_view.render_protocol(document)
                    self._render_protocol_execution_state()
            LOG.info(
                "Protocol loaded | path=%s | trials=%d",
                document.source_path,
                len(document.trials),
            )
            self._render_session_snapshot()
            return
        self._render_session_snapshot()
        message = str(result.get("message") or "安全清理未确认，协议未替换。")
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
            if hasattr(self.view, "render_actuation_alert"):
                self.view.render_actuation_alert(message, severe=True)
        LOG.error("Protocol load rejected after cleanup failure: %s", message)

    def _render_protocol_execution_state(self) -> None:
        if self.view and hasattr(self.view, "protocol_view"):
            self.view.protocol_view.render_execution_state(self._protocol_snapshot)
        self.actuation_worker.post_snapshot_request()
        self._drain_actuation_if_not_running()

    def ensure_safe_command(self, action: str, source: str | None = None) -> bool:
        """统一安全守卫：硬件就绪 + 安全状态 SAFE 才放行。"""
        if not self.safety_manager:
            return True

        telemetry = self.state.telemetry
        if not telemetry.connected:
            reason = f"硬件未连接，阻断 {action}"
            self.state.update_status(reason)
            if self.view:
                self.view.update_status(reason)
            LOG.warning(reason)
            return False

        now_ts = time.time()
        hardware_state = (
            telemetry.safety_state
            if telemetry.safety_state not in {"SAFE", "LOW_FLOW", "DATA_STALE"}
            else None
        )
        safety_state = self.safety_manager.evaluate_state(
            airflow=telemetry.airflow,
            timestamp=now_ts,
            hardware_state=hardware_state,
            previous=self._last_safety_state,
        )
        self._last_safety_state = safety_state
        allowed, reason = self.safety_manager.guard_command(
            safety_state=safety_state,
            hardware_ready=self.state.hardware_ready,
            action=action,
            source=source,
        )
        if allowed:
            return True

        self.state.update_status(reason)
        if self.view:
            self.view.update_status(reason)
        LOG.warning(reason)
        return False

    def _refresh_toolbar_state(self) -> None:
        if not self.view:
            return

        connected = self.state.telemetry.connected
        safety_state = self.state.telemetry.safety_state
        reset_blockers: list[str] = []
        if not connected:
            reset_blockers.append("未连接")
        if not self.state.hardware_ready:
            reset_blockers.append("自检未通过")
        if safety_state != "SAFE":
            reset_blockers.append(f"安全状态 {safety_state}")

        reset_enabled = not reset_blockers
        stop_enabled = connected
        connect_enabled = not self._connect_in_progress

        connect_tooltip = "运行自检并初始化硬件" if connect_enabled else "正在连接/自检中..."
        if connected and connect_enabled:
            connect_tooltip = "重新运行自检并刷新硬件状态"

        reset_tooltip = (
            "重置硬件，关闭阀门并重新自检"
            if reset_enabled
            else "不可用：" + "；".join(reset_blockers) + "。需已连接+自检通过+SAFE"
        )
        stop_tooltip = (
            "安全停止并释放硬件资源"
            if stop_enabled
            else "未连接硬件，无法安全停止"
        )
        tooltips = {
            "connect": connect_tooltip,
            "reset": reset_tooltip,
            "stop": stop_tooltip,
            "help": "打开本地中文手册",
        }
        self.view.update_toolbar(
            connect_enabled=connect_enabled,
            reset_enabled=reset_enabled,
            stop_enabled=stop_enabled,
            tooltips=tooltips,
        )

    def _handle_apply_result(
        self,
        result: FlowApplyResult,
        *,
        pretest,
    ) -> None:
        if result.success:
            self.state.applied_a = result.a
            self.state.applied_b = result.b
            self.state.applied_c = result.c
            self.state.applied_a_comp = result.a_comp
            self.state.flow_setpoints_ready = True
            self.state.update_status(result.message)
            if pretest:
                pretest.set_applied_values(
                    a=result.a,
                    b=result.b,
                    c=result.c,
                    a_comp=result.a_comp,
                )
                pretest.set_flow_message(result.message)
        else:
            self.state.flow_setpoints_ready = False
            self.state.update_status(result.message)
            if pretest:
                pretest.set_flow_message(result.message)
        if self.view:
            self.view.update_status(self.state.status_message)
        if pretest:
            pretest.set_applying(False)

    def _reset_startup_flows_to_zero_async(self) -> None:
        """Ensure opening/connecting the app leaves Alicat setpoints at no-flow."""
        self.state.flow_setpoints_ready = False
        self.state.update_status("硬件自检通过，正在清零 A/B/C...")
        if self.view:
            self.view.update_status(self.state.status_message)
            if hasattr(self.view, "pretest_view"):
                self.view.pretest_view.set_flow_message("正在清零 A/B/C...")

        thread = threading.Thread(target=self._run_startup_zero, daemon=True)
        thread.start()

    def _run_startup_zero(self) -> None:
        self._pending_flow_context["safety:startup-zero"] = {"kind": "startup_zero"}
        self._submit_flow_intent(
            mode="zero", a=0.0, b=0.0, c=0.0, source="safety:startup-zero"
        )

    @Slot(object)
    def _handle_startup_zero_completed(self, result_obj: object) -> None:
        result = result_obj if isinstance(result_obj, FlowApplyResult) else None
        pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None
        self.state.flow_setpoints_ready = False
        if result and result.success:
            self.state.applied_a = 0.0
            self.state.applied_b = 0.0
            self.state.applied_c = 0.0
            self.state.applied_a_comp = 0.0
            self.state.update_status("硬件自检通过，默认无气流：A/B/C 已清零")
            if pretest:
                pretest.set_applied_values(a=0.0, b=0.0, c=0.0, a_comp=0.0)
                pretest.set_flow_message("默认无气流：A/B/C 已清零")
                pretest.show_warning("")
        else:
            message = result.message if result else "未知错误"
            self.state.update_status(f"硬件自检通过，但流量清零失败：{message}")
            if pretest:
                pretest.set_flow_message(message)
                pretest.show_warning(self.state.status_message)
        if self.view:
            self.view.update_status(self.state.status_message)

    @Slot(str, float, float, float)
    def handle_flow_sequence_request(self, mode: str, a: float, b: float, c: float) -> FlowApplyResult:
        telemetry = self.state.telemetry
        hardware_ready = self.state.hardware_ready or telemetry.connected or self.state.simulation_mode
        pretest = self.view.pretest_view if self.view and hasattr(self.view, "pretest_view") else None
        if not hardware_ready:
            reason = f"硬件未就绪，阻断 UI·flow-seq-{mode}"
            result = FlowApplyResult(
                success=False,
                message=reason,
                a=a,
                b=b,
                c=c,
                a_comp=a + c,
                error="hardware_not_ready",
            )
            self._handle_apply_result(result, pretest=pretest)
            if pretest:
                pretest.apply_safety_state("DATA_STALE", reason, disabled=True)
            return result

        source = f"manual-sequence:{self._actuation_request_sequence + 1}"
        self._pending_flow_context[source] = {"kind": "apply"}
        return self._submit_flow_intent(mode=mode, a=a, b=b, c=c, source=source)

    def _apply_previous_shutdown_status(self) -> None:
        """Show last shutdown summary when app starts."""
        event = self.state.last_shutdown_event or {}
        if not event:
            return

        result = event.get("result")
        reason = event.get("error") or event.get("reason") or ""
        source = event.get("source", "shutdown")
        ts = event.get("ts") or event.get("timestamp")
        if ts:
            self.state.telemetry.timestamp = ts
        if result and result != "success":
            self._unsafe_shutdown_latched = True
            self.state.hardware_ready = False
            self.state.telemetry.connected = False
            message = f"上次关闭未完成（{source}）：{reason or '请重新自检确认安全'}"
        else:
            message = "上次已安全关闭，可以重新连接"
        self.state.update_status(message)

    def _handle_shutdown_event(self, event: dict, *, success_message: str) -> None:
        success = event.get("result") == "success"
        self._unsafe_shutdown_latched = not success
        self.state.flow_setpoints_ready = False
        message = (
            success_message
            if success
            else f"关闭未完成：{event.get('error') or '请人工检查并重新自检'}"
        )
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
            self.view.render_telemetry(self.state.telemetry)
            self.view.render_last_shutdown(event)

    def _latch_restart_failure(self, reason: str) -> None:
        self._unsafe_shutdown_latched = True
        self._block_for_unsafe_shutdown(reason)
        LOG.critical(reason)

    def _block_for_unsafe_shutdown(self, message: str) -> None:
        self.state.hardware_ready = False
        self.state.telemetry.connected = False
        self.state.flow_setpoints_ready = False
        self._connect_in_progress = False
        self.actuation_interlock.update(
            connected=False,
            hardware_ready=False,
            flow_setpoints_ready=False,
            ttl_input_ready=False,
        )
        self.actuation_worker.post_readiness_update(
            readiness=self._execution_readiness(),
            timestamp=time.time(),
        )
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)
            self.view.render_telemetry(self.state.telemetry)

    def update_breath_threshold(self, name: str, value: float) -> None:
        old_val = 0.0
        new_val = float(value)

        if name == "inhale":
            old_val = self.state.inhale_threshold
            self.state.inhale_threshold = new_val
        elif name == "exhale":
            old_val = self.state.exhale_threshold
            self.state.exhale_threshold = new_val
        else:
            return

        # GatingService belongs to ActuationWorker; update it through an owner intent.
        self.actuation_worker.post_gating_thresholds(
            inhale_threshold=self.state.inhale_threshold,
            exhale_threshold=self.state.exhale_threshold,
        )
        self._drain_actuation_if_not_running()

        # Log event (AC5)
        log_entry = {
            "ts": time.time(),
            "source": "drag/spin", # Generalized as UI interaction
            "old": old_val,
            "new": new_val,
            "unit": "V", # Assuming Voltage as per AC1/2.2 default
            "result": "success",
            "safety_state": self.state.telemetry.safety_state,
        }
        self._breath_logger.info("threshold_update | %s", log_entry)
        if self.session_state.status == SessionStatus.RECORDING:
            self._post_controller_session_event(
                event="threshold_changed",
                source="controller",
                result="success",
                message="呼吸阈值已更新。",
                payload={
                    "threshold": name,
                    "old": old_val,
                    "new": new_val,
                    "unit": "V",
                },
            )

        self._persist_config_values(
            {
                "inhale_threshold": self.state.inhale_threshold,
                "exhale_threshold": self.state.exhale_threshold,
            }
        )

        if self.view and hasattr(self.view, "pretest_view"):
            self.view.pretest_view.set_thresholds(
                self.state.inhale_threshold,
                self.state.exhale_threshold,
            )

    def set_low_flow_threshold(self, value: float) -> bool:
        """Options 阈值更新：校验、更新内存并持久化配置。"""
        if not self.safety_manager:
            return False

        valid, msg = self.safety_manager.validate_threshold(value)
        if not valid:
            self.state.update_status(msg)
            if self.view:
                self.view.update_status(msg)
            LOG.warning(msg)
            return False

        # 更新运行时
        new_value = float(value)
        self.safety_manager.low_flow_threshold = new_value
        self.state.low_flow_threshold = new_value
        self.state.update_status(f"阈值已更新为 {new_value:.2f}")
        if self.view:
            self.view.update_status(self.state.status_message)

        # 持久化配置
        self._persist_threshold(new_value)
        if self.session_state.status == SessionStatus.RECORDING:
            self._post_controller_session_event(
                event="threshold_changed",
                source="controller",
                result="success",
                message="低流量阈值已更新。",
                payload={
                    "threshold": "low_flow",
                    "new": new_value,
                },
            )
        return True

    def _persist_threshold(self, value: float) -> None:
        self._persist_config_values({"low_flow_threshold": value})

    def _persist_config_values(self, updates: dict) -> None:
        config_path = self.state.config_path
        if not config_path:
            return
        config_path = Path(config_path)
        candidate_paths = [config_path]

        # If the bundled config is read-only (PyInstaller), fall back to a user-writable copy.
        fallback_path = Path.home() / ".olfactorypilot" / config_path.name
        if fallback_path not in candidate_paths:
            candidate_paths.append(fallback_path)

        for path in candidate_paths:
            try:
                if path.exists():
                    with path.open(encoding="utf-8") as handle:
                        config = json.load(handle)
                else:
                    # Seed with existing config contents when creating a new copy.
                    try:
                        with config_path.open(encoding="utf-8") as handle:
                            config = json.load(handle)
                        path.parent.mkdir(parents=True, exist_ok=True)
                    except Exception:
                        config = {}
                config.update(updates)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)
                self.state.config_path = path
                return
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("Failed to persist config to %s: %s", path, exc)

    def _apply_safety_check(
        self,
        *,
        initial: bool = False,
        hardware_safety: str | None = None,
        previous_state_override: str | None = None,
    ) -> None:
        if not self.safety_manager:
            return

        telemetry = self.state.telemetry
        if not telemetry.connected:
            if initial:
                return

            if (
                not self._has_seen_connection
                and not self.state.hardware_ready
                and not self._connect_in_progress
            ):
                # Still idle, no prior connection; keep waiting message instead of raising emergency.
                return

            if self._is_expected_disconnect():
                self._refresh_toolbar_state()
                return

            now_ts = telemetry.timestamp or time.time()
            safety_state = SafetyState(
                state="DATA_STALE",
                airflow=telemetry.airflow,
                threshold=self.safety_manager.low_flow_threshold,
                updated_at=now_ts,
                reason="硬件断开，默认阻断命令",
            )
            self._last_safety_state = safety_state
            telemetry.safety_state = safety_state.state
            telemetry.safety_reason = safety_state.reason
            telemetry.timestamp = safety_state.updated_at
            source_label = "disconnect"
            if hardware_safety and hardware_safety not in {"SAFE"}:
                source_label = hardware_safety
            self.state.last_shutdown_event = {
                "state": safety_state.state,
                "airflow": telemetry.airflow,
                "threshold": self.safety_manager.low_flow_threshold,
                "timestamp": safety_state.updated_at,
                "reason": safety_state.reason,
                "source": source_label,
            }
            self.state.update_status("紧急关闭：硬件断开，默认阻断命令")
            if self.view:
                self.view.update_status(self.state.status_message)
                self.view.render_telemetry(telemetry)
                self.view.render_last_shutdown(self.state.last_shutdown_event)
                self._update_pretest_view_safety(safety_state)
            self._refresh_toolbar_state()
            return

        airflow = telemetry.airflow
        previous_state = self._last_safety_state
        timestamp = telemetry.timestamp

        safety_state = self.safety_manager.evaluate_state(
            airflow=airflow,
            timestamp=timestamp,
            previous=previous_state,
            hardware_state=hardware_safety,
        )
        self._last_safety_state = safety_state
        telemetry.safety_state = safety_state.state
        telemetry.safety_reason = safety_state.reason
        telemetry.timestamp = safety_state.updated_at
        LOG.info(
            "Safety update | state=%s | airflow=%.3f | reason=%s | hw=%s",
            safety_state.state,
            airflow,
            safety_state.reason,
            hardware_safety,
        )

        previous_label = previous_state.state if previous_state else previous_state_override
        should_update_status = safety_state.state != previous_label or (
            initial and safety_state.state == "LOW_FLOW"
        )
        if not should_update_status:
            return

        if safety_state.state in {"LOW_FLOW", "DATA_STALE"} or (
            safety_state.state not in {"SAFE"} and hardware_safety
        ):
            self.state.last_shutdown_event = {
                "state": safety_state.state,
                "airflow": airflow,
                "threshold": self.safety_manager.low_flow_threshold,
                "timestamp": safety_state.updated_at,
                "reason": safety_state.reason,
                "source": hardware_safety or "flow_monitor",
            }

        if safety_state.state == "SAFE":
            self.state.update_status("气流恢复正常")
        elif safety_state.state == "LOW_FLOW":
            reason = (
                f"气流低于阈值（当前 {airflow:.2f} < {self.safety_manager.low_flow_threshold:.2f}）"
            )
            self.state.update_status(f"紧急关闭：{reason}")
        elif safety_state.state == "DATA_STALE":
            self.state.update_status("紧急关闭：气流过期，默认阻断命令")
        else:
            self.state.update_status(f"紧急关闭：硬件上报安全状态 {safety_state.state}")
        if self.view and self.state.last_shutdown_event:
            self.view.render_last_shutdown(self.state.last_shutdown_event)
        self._refresh_toolbar_state()
        self._update_pretest_view_safety(safety_state)

    def _update_pretest_view_safety(self, safety_state: SafetyState) -> None:
        if self.view and hasattr(self.view, "pretest_view"):
            # LOW_FLOW must still permit an idle MFC recovery write.  Valve
            # OPEN remains independently blocked by ActuationWorker/ValveService.
            disabled = safety_state.state not in {"SAFE", "LOW_FLOW"}
            self.view.pretest_view.apply_safety_state(
                safety_state.state,
                safety_state.reason,
                disabled=disabled,
            )

    def _is_expected_disconnect(self) -> bool:
        event = self.state.last_shutdown_event or {}
        return (
            event.get("result") == "success"
            and event.get("source") in {"stop", "reset", "app_exit"}
        )
