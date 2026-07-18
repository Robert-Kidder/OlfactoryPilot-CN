from __future__ import annotations

import logging
import time

from PySide6.QtCore import QThread, Signal

from app.models import SelfCheckResult
from app.services.hal import HalInterface
from app.services.hardware_check_service import HardwareCheckService
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlTriggerConfig, TtlTriggerService

LOG = logging.getLogger(__name__)


class HardwareWorker(QThread):
    telemetry_ready = Signal(dict)
    status_message = Signal(str)
    self_check_completed = Signal(list, bool)
    breath_samples = Signal(list, float)
    ttl_pulse = Signal(object)
    ttl_input_error = Signal(str)
    ttl_readiness_changed = Signal(bool)

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
        self.check_service = check_service
        self.hal: HalInterface = hal or MockHAL()
        self.simulation_mode = simulation
        self._self_check_requested = False
        self._connected = False

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
                self.telemetry_ready.emit(payload)
                next_telemetry += max(self.telemetry_interval_ms, 1) / 1000.0

            sleep_ms = min(self.telemetry_interval_ms, self.ttl_interval_ms)
            self.msleep(max(1, sleep_ms))
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
        return bool(getattr(self.hal, "ttl_input_ready", False))

    def arm_ttl(self, *, arm_epoch: int) -> None:
        self.ttl_service.arm(arm_epoch=arm_epoch)

    def disarm_ttl(self) -> None:
        self.ttl_service.disarm()

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
        self.breath_samples.emit([value], timestamp)

    def _emit_ai_frame(self, timestamp: float) -> None:
        try:
            frame = self.hal.read_ai_frame(timestamp)
            if self._ai_sample_count % self._breath_emit_every == 0:
                self.breath_samples.emit([float(frame.ai0)], float(frame.timestamp))
            self._ai_sample_count += 1
            if frame.ai6 is not None:
                pulse = self.ttl_service.process_sample(float(frame.ai6), timestamp=float(frame.timestamp))
                if pulse is not None:
                    self.ttl_pulse.emit(pulse)
        except Exception as exc:  # pragma: no cover - hardware boundary
            message = f"TTL/共享 AI 读取失败：{exc}；协议执行已请求安全阻断。"
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
