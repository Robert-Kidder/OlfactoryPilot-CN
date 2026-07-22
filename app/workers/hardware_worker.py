from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QThread, Signal

from app.models import SelfCheckResult
from app.services.hal import AnalogInputFrame, BreathSampleBatch, HalInterface
from app.services.hardware_check_service import HardwareCheckService
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlTriggerConfig, TtlTriggerService

LOG = logging.getLogger(__name__)


class HardwareWorker(QThread):
    telemetry_ready = Signal(dict)
    status_message = Signal(str)
    self_check_completed = Signal(list, bool)
    breath_samples = Signal(object)
    ttl_pulse = Signal(object)
    ttl_input_error = Signal(str)
    ttl_readiness_changed = Signal(bool)
    ttl_arm_ack = Signal(int, bool)
    ttl_disarm_ack = Signal(bool)

    def __init__(
        self,
        telemetry_hz: int = 5,
        check_service: HardwareCheckService | None = None,
        breath_hz: int = 100,
        ttl_poll_hz: int = 1000,
        ttl_config: dict | TtlTriggerConfig | None = None,
        hal: HalInterface | None = None,
        simulation: bool = False,
    ) -> None:
        super().__init__()
        self._running = False
        self.telemetry_interval_ms = self._compute_interval_ms(telemetry_hz)
        self.breath_interval_ms = self._compute_interval_ms(breath_hz)
        config = (
            ttl_config
            if isinstance(ttl_config, TtlTriggerConfig)
            else TtlTriggerConfig.from_mapping(ttl_config)
        )
        if ttl_config is None:
            config = TtlTriggerConfig.from_mapping({"ttl_poll_hz": ttl_poll_hz})
        self.ttl_service = TtlTriggerService(config)
        self.ttl_interval_ms = self._compute_interval_ms(config.poll_hz)
        self._breath_emit_every = max(1, round(config.poll_hz / max(1, breath_hz)))
        self._ai_sample_count = 0
        self._last_ai_epoch = -1
        self._last_ai_sequence = -1
        self._last_ai_monotonic_ns = -1
        self.check_service = check_service
        self.hal: HalInterface = hal or MockHAL()
        self._ttl_runtime_ready = bool(getattr(self.hal, "ttl_input_ready", False))
        self._ai_error_latched = False
        self._ai_retry_not_before = 0.0
        self._ai_error_backoff_s = 1.0
        self.simulation_mode = simulation
        self._self_check_requested = False
        self._connected = False
        self._actuation_sink = None
        self._interlock_ingress = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def run(self) -> None:  # noqa: D401
        """Worker loop emits telemetry placeholders over signals."""
        self._running = True
        mode_label = "（模拟模式）" if self.simulation_mode else "（占位）"
        self.status_message.emit(f"硬件线程已启动{mode_label}")
        self._run_self_check()
        next_telemetry = time.time()
        next_ai = time.time()
        while self._running:
            now = time.time()
            if self._self_check_requested:
                self._self_check_requested = False
                self._run_self_check()

            if now >= next_ai:
                self._emit_ai_frame(now)
                next_ai += max(self.ttl_interval_ms, 1) / 1000.0

            if now >= next_telemetry:
                payload: dict[str, object] = {
                    "connected": self._connected,
                    "airflow": self._read_flow(),
                    "safety_state": "SAFE",
                    "timestamp": now,
                }
                if self._interlock_ingress is not None:
                    current = self._interlock_ingress.read()[1]
                    self._interlock_ingress.publish_raw_telemetry(
                        airflow=float(payload["airflow"]),
                        timestamp=now,
                        hardware_state=str(payload["safety_state"]),
                        connected=bool(payload["connected"]),
                        hardware_ready=bool(self._connected),
                        flow_setpoints_ready=current.flow_setpoints_ready,
                        ttl_input_ready=self.ttl_input_ready,
                        has_protocol=current.has_protocol,
                        device_lease=current.device_lease,
                    )
                self.telemetry_ready.emit(payload)
                next_telemetry += max(self.telemetry_interval_ms, 1) / 1000.0

            sleep_ms = min(self.telemetry_interval_ms, self.ttl_interval_ms)
            self.msleep(max(1, sleep_ms))
        try:
            self.hal.reset_ai_input()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HardwareWorker 线程释放 AI 资源失败")
        self.status_message.emit("硬件线程已停止")

    def stop(self) -> None:
        self._self_check_requested = False
        if not self.isRunning():
            self._connected = False
            return
        self._running = False
        self._connected = False
        self.wait(2000)

    def request_self_check(self) -> None:
        """Allow controller/UI to trigger another self-check without blocking UI."""
        self._self_check_requested = True

    def set_actuation_sink(self, sink, *, interlock_ingress=None) -> None:
        self._actuation_sink = sink
        self._interlock_ingress = interlock_ingress

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        """数字输出由 HAL 处理；记录日志并更新连接状态。"""
        LOG.info(
            "digital_write | device=%s | line=%s | state=%s",
            device or "N/A",
            line,
            state,
        )
        if not self.hal:
            self._connected = True
            return True
        try:
            result = bool(self.hal.write_digital(device=device, line=line, state=state))
            self._connected = self._connected or result
            return result
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL digital write failed")
            return False

    def mark_disconnected(self) -> None:
        """Explicitly mark hardware as disconnected for telemetry loop."""
        self._self_check_requested = False
        self._connected = False
        self.disarm_ttl()

    @property
    def ttl_input_ready(self) -> bool:
        return bool(self._ttl_runtime_ready and getattr(self.hal, "ttl_input_ready", False))

    def arm_ttl(self, *, arm_epoch: int) -> None:
        try:
            self.ttl_service.arm(arm_epoch=arm_epoch)
            self.ttl_arm_ack.emit(int(arm_epoch), True)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("TTL 布防失败")
            self.ttl_arm_ack.emit(int(arm_epoch), False)

    def disarm_ttl(self) -> None:
        try:
            self.ttl_service.disarm()
            self.ttl_disarm_ack.emit(True)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("TTL 解除布防失败")
            self.ttl_disarm_ack.emit(False)

    # Shutdown helpers for safe-stop scenarios.
    def close_all_channels(self) -> bool:
        """Close valves/actuators; placeholder returns success for mock worker."""
        self._connected = False
        if not self.hal:
            return True
        try:
            return bool(self.hal.close_all())
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL close_all failed")
            return False

    def stop_heaters(self) -> bool:
        """Stop heaters/pumps; placeholder returns success for mock worker."""
        if not self.hal:
            return True
        try:
            return bool(self.hal.stop_heaters())
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL stop_heaters failed")
            return False

    def flush_logs(self) -> None:
        """Flush data logger/session file handles if any."""
        if not self.hal:
            return None
        try:
            self.hal.flush_logs()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL flush_logs failed")
            return None

    def release_resources(self) -> None:
        """Release NI/RS232 handles and stop worker loop."""
        try:
            if self.hal:
                self.hal.close_all()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL release resources failed")
        self.stop()

    def release_ai_resources(self) -> None:
        """Release only HardwareWorker-owned AI resources; DO belongs elsewhere."""
        self.stop()

    def _run_self_check(self) -> None:
        results: list[SelfCheckResult] = []
        ready = False
        try:
            if self.simulation_mode and hasattr(self.hal, "self_check"):
                results, ready = self.hal.self_check()
            elif self.check_service:
                self._release_hal_handles_for_self_check()
                results, ready = self.check_service.run_checks()
            elif hasattr(self.hal, "self_check"):
                results, ready = self.hal.self_check()
            elif self.simulation_mode:
                ready = True
            else:
                ready = False
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("Hardware self-check raised an exception")
            results = [
                SelfCheckResult(
                    name="self_check",
                    type="unknown",
                    status="FAIL",
                    reason=str(exc),
                    suggestion="检查硬件连接与驱动，重试自检",
                    checked_at=time.time(),
                )
            ]
            ready = False
        self._connected = ready
        self.self_check_completed.emit(results, ready)
        if self.simulation_mode and ready:
            status = "模拟模式：自检通过"
        else:
            status = "硬件自检通过" if ready else "硬件自检失败，请检查连接"
        self.status_message.emit(status)
        LOG.info("硬件自检完成 | ready=%s | 项目数=%s", ready, len(results))

    def _release_hal_handles_for_self_check(self) -> None:
        """Release HAL-held serial handles before HardwareCheckService opens COM ports."""
        if not self.hal:
            return
        try:
            self.hal.flush_logs()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL handle release before self-check failed")

    def _emit_breath_sample(self, timestamp: float) -> None:
        """Generate breath sample via HAL (mock or real)."""
        try:
            value = float(self.hal.read_ai0(timestamp)) if self.hal else 0.0
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL read_ai0 failed")
            value = 0.0
        frame = AnalogInputFrame(
            timestamp=timestamp,
            ai0=value,
            monotonic_ns=time.perf_counter_ns(),
            ai_epoch=0,
            sample_sequence=self._ai_sample_count,
        )
        self._ai_sample_count += 1
        self.breath_samples.emit(BreathSampleBatch.from_frames((frame,)))

    def _emit_ai_frame(self, timestamp: float) -> None:
        attempt_started = time.monotonic()
        if self._ai_error_latched and attempt_started < self._ai_retry_not_before:
            return
        try:
            frames = self.hal.read_ai_frames(timestamp)
            if not frames:
                return
            normalized_frames: list[AnalogInputFrame] = []
            for frame in frames:
                frame_timestamp = float(frame.timestamp)
                monotonic_ns = int(frame.monotonic_ns)
                ai_epoch = int(frame.ai_epoch)
                sample_sequence = int(frame.sample_sequence)
                ai0 = float(frame.ai0)
                ai6 = None if frame.ai6 is None else float(frame.ai6)
                if (
                    not math.isfinite(frame_timestamp)
                    or not math.isfinite(ai0)
                    or monotonic_ns <= 0
                    or ai_epoch <= 0
                    or sample_sequence < 0
                ):
                    raise ValueError("共享 AI 帧包含非有限的时间戳或 AI0 样本")
                if ai6 is not None and not math.isfinite(ai6):
                    raise ValueError("共享 AI 帧包含非有限的 AI6 样本")
                if ai_epoch < self._last_ai_epoch:
                    raise ValueError("共享 AI epoch 倒退")
                if ai_epoch == self._last_ai_epoch and (
                    sample_sequence <= self._last_ai_sequence
                    or monotonic_ns <= self._last_ai_monotonic_ns
                ):
                    raise ValueError("共享 AI 样本 identity 重复或倒退")
                self._last_ai_epoch = ai_epoch
                self._last_ai_sequence = sample_sequence
                self._last_ai_monotonic_ns = monotonic_ns
                normalized_frames.append(
                    AnalogInputFrame(
                        timestamp=frame_timestamp,
                        ai0=ai0,
                        ai6=ai6,
                        monotonic_ns=monotonic_ns,
                        ai_epoch=ai_epoch,
                        sample_sequence=sample_sequence,
                        origin_uncertainty_ns=int(frame.origin_uncertainty_ns),
                    )
                )

            pulses = []
            breath_frames: list[AnalogInputFrame] = []
            for frame in normalized_frames:
                if self._ai_sample_count % self._breath_emit_every == 0:
                    breath_frames.append(frame)
                self._ai_sample_count += 1
                if frame.ai6 is not None:
                    pulse = self.ttl_service.process_sample(
                        frame.ai6,
                        timestamp=frame.timestamp,
                        monotonic_ns=frame.monotonic_ns,
                    )
                    if pulse is not None:
                        pulses.append(pulse)

            if self._ai_error_latched:
                self._ai_error_latched = False
                restored = bool(getattr(self.hal, "ttl_input_ready", False))
                if restored != self._ttl_runtime_ready:
                    self._ttl_runtime_ready = restored
                    self.ttl_readiness_changed.emit(restored)
            if breath_frames:
                batch = BreathSampleBatch.from_frames(tuple(breath_frames))
                if self._actuation_sink is not None:
                    self._actuation_sink.post_ai_batch(batch)
                self.breath_samples.emit(batch)
            for pulse in pulses:
                if self._actuation_sink is not None:
                    self._actuation_sink.post_ttl_pulse(pulse)
                self.ttl_pulse.emit(pulse)
        except Exception as exc:  # pragma: no cover - hardware boundary
            first_failure = not self._ai_error_latched
            self._ai_error_latched = True
            self._ai_retry_not_before = attempt_started + self._ai_error_backoff_s
            try:
                self.hal.reset_ai_input()
            except Exception:  # pragma: no cover - defensive
                LOG.exception("释放失效的共享 AI task 失败")
            if self._ttl_runtime_ready:
                self._ttl_runtime_ready = False
                self.ttl_readiness_changed.emit(False)
            if self._interlock_ingress is not None:
                self._interlock_ingress.update(
                    hardware_ready=False,
                    ttl_input_ready=False,
                    safety_state="DATA_STALE",
                )
            self.disarm_ttl()
            if not first_failure:
                LOG.debug("共享 AI 读取仍未恢复，保持故障锁存并延后重试：%s", exc)
                return
            message = f"TTL/共享 AI 读取失败：{exc}；协议执行已请求安全阻断。"
            if self._actuation_sink is not None:
                self._actuation_sink.post_input_error(message)
            LOG.exception(message)
            self.ttl_input_error.emit(message)

    def _read_flow(self) -> float:
        if not self.hal:
            return 0.0
        try:
            return float(self.hal.read_flow())
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL read_flow failed")
            return 0.0

    @staticmethod
    def _compute_interval_ms(telemetry_hz: int) -> int:
        if telemetry_hz <= 0:
            LOG.warning("Invalid telemetry_hz=%s, falling back to 1 Hz", telemetry_hz)
            return 1000
        interval = int(1000 / telemetry_hz)
        if interval <= 0:
            LOG.warning("telemetry_hz=%s too high, clamping interval to 1 ms", telemetry_hz)
            return 1
        return interval
