from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Slot

from app.models import AppState, SafetyState
from app.services import GatingService, ShutdownService, CalibrationSession
from app.workers import HardwareWorker

if TYPE_CHECKING:
    from app.views import MainWindow

LOG = logging.getLogger(__name__)


class MainController(QObject):
    def __init__(
        self,
        state: AppState,
        worker: HardwareWorker,
        safety_manager=None,
        config: dict | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.worker = worker
        self.safety_manager = safety_manager
        shutdown_cfg = config or {}
        if "shutdown_record_path" not in shutdown_cfg:
            shutdown_cfg["shutdown_record_path"] = str(Path.cwd() / "logs" / "last_shutdown_event.json")
        self.shutdown_service = ShutdownService(
            state=state,
            worker=worker,
            safety_manager=safety_manager,
            retry_limit=int(shutdown_cfg.get("shutdown_retry_limit", 2)),
            retry_interval=float(shutdown_cfg.get("shutdown_retry_interval_s", 0.2)),
            record_path=self._resolve_record_path(shutdown_cfg),
        )
        self.gating_service = GatingService(
            inhale_threshold=state.inhale_threshold,
            exhale_threshold=state.exhale_threshold,
        )
        self.calibration_session = CalibrationSession() # Story 2.6
        self._last_safety_state: SafetyState | None = None
        self._has_seen_connection = False
        self._connect_in_progress = False
        self.view: MainWindow | None = None
        self.worker.telemetry_ready.connect(self.handle_telemetry)
        self.worker.status_message.connect(self.handle_status)
        self.worker.self_check_completed.connect(self.handle_self_check)
        if hasattr(self.worker, "breath_samples"):
            self.worker.breath_samples.connect(self.handle_breath_samples)
        self._breath_logger = logging.getLogger("breath_viz")

    def bind_view(self, view: MainWindow) -> None:
        self.view = view
        self._apply_previous_shutdown_status()
        self._apply_safety_check(initial=True)
        self.view.update_status(self.state.status_message)
        self.view.render_telemetry(self.state.telemetry)
        self.view.render_last_shutdown(self.state.last_shutdown_event)
        self._refresh_toolbar_state()
        if hasattr(self.view, "calibration_view"):
            self.view.calibration_view.breath_metrics.connect(self.handle_breath_metrics)
            self.view.calibration_view.threshold_changed.connect(self.update_breath_threshold)
            self.view.calibration_view.calibration_requested.connect(self.handle_calibration_request)

    def start_worker(self) -> None:
        if not self.worker.isRunning():
            LOG.info("Starting hardware worker thread")
            self.worker.start()

    def shutdown(self) -> None:
        LOG.info("Shutting down worker thread")
        event = self.shutdown_service.shutdown(
            source="app_exit",
            reason="application_exit",
            force=True,
        )
        self._handle_shutdown_event(event, success_message="已安全关闭")

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
        if self.view:
            self.view.render_telemetry(self.state.telemetry)
            self.view.update_status(self.state.status_message)
        self._refresh_toolbar_state()

    @Slot(str)
    def handle_status(self, message: str) -> None:
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)

    @Slot(list, float)
    def handle_breath_samples(self, samples: list, timestamp: float) -> None:
        if self.view:
            self.view.ingest_breath_samples(samples, timestamp=timestamp)

        # Calibration Session Update (Story 2.6)
        if self.calibration_session.is_active:
            for sample in samples:
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
                    
                    self._persist_config_values({
                        "signal_offset": result.offset,
                        "signal_gain": result.gain
                    })
                    LOG.info("Calibration applied: Offset=%.3f, Gain=%.3f", result.offset, result.gain)
            elif self.view and hasattr(self.view, "calibration_view"):
                 # Update progress/stats
                 progress = self.calibration_session.get_progress()
                 remaining = self.calibration_session.duration_sec * (1 - progress)
                 self.view.calibration_view.set_calibration_state(True, f"正在校准... {remaining:.1f}s")
                 self.view.calibration_view.set_calibration_progress(int(progress * 100))
                 self.view.calibration_view.update_calibration_stats(
                     self.calibration_session.current_max,
                     self.calibration_session.current_min
                 )

        # Process gating logic (100Hz resolution)
        # Calculate sample interval (assuming 100Hz from worker, or derive from timestamp)
        # We'll assume strict 100Hz (0.01s) as per requirement FR3.1
        dt = 0.01
        start_ts = timestamp - (len(samples) * dt)
        
        transitions = self.gating_service.process_batch(
            samples,
            self.state.telemetry.safety_state,
            timestamp_start=start_ts,
            dt=dt
        )
        
        if transitions:
            # Update current state
            last_transition = transitions[-1]
            self.state.telemetry.gating_state = last_transition.state
            if self.view:
                self.view.update_gating_state(last_transition.state)
            
            # Log transitions
            for t in transitions:
                log_entry = {
                    "ts": t.timestamp,
                    "sample_value": t.sample_value,
                    "gate_state": t.state,
                    "inhale": self.gating_service.inhale_threshold,
                    "exhale": self.gating_service.exhale_threshold,
                    "safety_state": t.safety_state,
                }
                self._breath_logger.info("threshold_cross | %s", log_entry)

    def handle_breath_metrics(self, payload: dict) -> None:
        warning = bool(payload.get("warning_flag"))
        reason = payload.get("reason") or ""
        log_payload = {
            "ts": payload.get("ts"),
            "fps_avg": payload.get("fps_avg"),
            "fps_p95": payload.get("fps_p95"),
            "window_s": payload.get("window_s"),
            "sample_count": payload.get("sample_count"),
            "warning_flag": warning,
            "reason": reason,
        }
        self._breath_logger.info("breath_viz | %s", log_payload)
        if warning:
            self._breath_logger.warning("breath_viz_warning | %s", log_payload)
            message = "波形渲染 FPS 低于 30，已记录" if reason == "fps_low" else "呼吸数据过期，等待新样本"
            self.state.update_status(message)
            if self.view:
                self.view.update_status(self.state.status_message)

    @Slot(list, bool)
    def handle_self_check(self, results: list, hardware_ready: bool) -> None:
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
        self._refresh_toolbar_state()

    def request_self_check(self) -> None:
        """Trigger self-check from UI, keep thread-safe."""
        self.state.update_status("正在重新自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
        if not self.worker.isRunning():
            self.start_worker()
        self.worker.request_self_check()

    def connect_hardware(self) -> None:
        if self._connect_in_progress:
            return

        self._connect_in_progress = True
        self.state.update_status("正在连接硬件并执行自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
        self.start_worker()
        self.worker.request_self_check()
        self._refresh_toolbar_state()

    def reset_hardware(self) -> None:
        if not self.ensure_hardware_ready("Reset"):
            self._refresh_toolbar_state()
            return
        if not self.ensure_safe_command("Reset", source="UI"):
            self._refresh_toolbar_state()
            return

        self.state.update_status("正在重置硬件，重新握手并刷新自检...")
        self.state.hardware_ready = False
        self.state.telemetry.connected = False
        self.state.last_shutdown_event = {
            "state": self.state.telemetry.safety_state,
            "airflow": self.state.telemetry.airflow,
            "threshold": self.state.low_flow_threshold,
            "timestamp": time.time(),
            "reason": "reset_request",
            "source": "reset",
        }
        if self.view:
            self.view.update_status(self.state.status_message)
            self.view.render_telemetry(self.state.telemetry)
            self.view.render_last_shutdown(self.state.last_shutdown_event)
        if hasattr(self.worker, "mark_disconnected"):
            self.worker.mark_disconnected()
        self.worker.request_self_check()
        self._refresh_toolbar_state()

    def stop_hardware(self) -> None:
        self.state.update_status("正在安全停止，关闭阀门并释放资源...")
        if self.view:
            self.view.update_status(self.state.status_message)
        event = self.shutdown_service.shutdown(
            source="stop",
            reason="user_stop",
            force=True,
        )
        self._handle_shutdown_event(event, success_message="已停止/已关闭阀门")
        self._connect_in_progress = False
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
            self.state.hardware_ready = False
            self.state.telemetry.connected = False
            message = f"上次关闭未完成（{source}）：{reason or '请重新自检确认安全'}"
        else:
            message = "上次已安全关闭，可以重新连接"
        self.state.update_status(message)

    def _handle_shutdown_event(self, event: dict, *, success_message: str) -> None:
        success = event.get("result") == "success"
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

        # Update service
        self.gating_service.set_thresholds(
            self.state.inhale_threshold,
            self.state.exhale_threshold
        )

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

        self._persist_config_values(
            {
                "inhale_threshold": self.state.inhale_threshold,
                "exhale_threshold": self.state.exhale_threshold,
            }
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

            if not self._has_seen_connection and not self.state.hardware_ready and not self._connect_in_progress:
                # Still idle, no prior connection; keep waiting message instead of raising emergency.
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
