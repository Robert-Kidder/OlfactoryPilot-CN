from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QThread, Signal

from app.models import SelfCheckResult
from app.services.hardware_check_service import HardwareCheckService

LOG = logging.getLogger(__name__)


class HardwareWorker(QThread):
    telemetry_ready = Signal(dict)
    status_message = Signal(str)
    self_check_completed = Signal(list, bool)
    breath_samples = Signal(list, float)

    def __init__(
        self,
        telemetry_hz: int = 5,
        check_service: HardwareCheckService | None = None,
        breath_hz: int = 100,
    ) -> None:
        super().__init__()
        self._running = False
        self.telemetry_interval_ms = self._compute_interval_ms(telemetry_hz)
        self.breath_interval_ms = self._compute_interval_ms(breath_hz)
        self.check_service = check_service
        self._self_check_requested = False
        self._connected = False
        self._breath_phase = 0.0

    def run(self) -> None:  # noqa: D401
        """Worker loop emits telemetry placeholders over signals."""
        self._running = True
        self.status_message.emit("硬件线程已启动（占位）")
        self._run_self_check()
        next_telemetry = time.time()
        next_breath = time.time()
        while self._running:
            now = time.time()
            if self._self_check_requested:
                self._self_check_requested = False
                self._run_self_check()

            if now >= next_breath:
                self._emit_breath_sample(now)
                next_breath += max(self.breath_interval_ms, 1) / 1000.0

            if now >= next_telemetry:
                payload: dict[str, object] = {
                    "connected": self._connected,
                    "airflow": 0.0,
                    "safety_state": "SAFE",
                    "timestamp": now,
                }
                self.telemetry_ready.emit(payload)
                next_telemetry += max(self.telemetry_interval_ms, 1) / 1000.0

            sleep_ms = min(self.telemetry_interval_ms, self.breath_interval_ms)
            self.msleep(max(1, sleep_ms))
        self.status_message.emit("硬件线程已停止")

    def stop(self) -> None:
        if not self.isRunning():
            self._connected = False
            return
        self._running = False
        self._connected = False
        self.wait(2000)

    def request_self_check(self) -> None:
        """Allow controller/UI to trigger another self-check without blocking UI."""
        self._self_check_requested = True

    def mark_disconnected(self) -> None:
        """Explicitly mark hardware as disconnected for telemetry loop."""
        self._connected = False

    # Shutdown helpers for safe-stop scenarios.
    def close_all_channels(self) -> bool:
        """Close valves/actuators; placeholder returns success for mock worker."""
        self._connected = False
        return True

    def stop_heaters(self) -> bool:
        """Stop heaters/pumps; placeholder returns success for mock worker."""
        return True

    def flush_logs(self) -> None:
        """Flush data logger/session file handles if any."""
        return None

    def release_resources(self) -> None:
        """Release NI/RS232 handles and stop worker loop."""
        self.stop()

    def _run_self_check(self) -> None:
        if not self.check_service:
            return
        try:
            results, ready = self.check_service.run_checks()
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
        status = "硬件自检通过" if ready else "硬件自检失败，请检查连接"
        self.status_message.emit(status)
        LOG.info("硬件自检完成 | ready=%s | 项目数=%s", ready, len(results))

    def _emit_breath_sample(self, timestamp: float) -> None:
        """Generate a placeholder breath sample (sine wave) for the calibration view."""
        value = 0.5 * math.sin(2 * math.pi * 0.2 * timestamp + self._breath_phase)
        self._breath_phase += 0.05
        self.breath_samples.emit([value], timestamp)

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
