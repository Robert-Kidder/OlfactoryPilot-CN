from __future__ import annotations

import logging
import time

from PySide6.QtCore import QThread, Signal

from app.services.hardware_check_service import HardwareCheckService

LOG = logging.getLogger(__name__)


class HardwareWorker(QThread):
    telemetry_ready = Signal(dict)
    status_message = Signal(str)
    self_check_completed = Signal(list, bool)

    def __init__(
        self,
        telemetry_hz: int = 5,
        check_service: HardwareCheckService | None = None,
    ) -> None:
        super().__init__()
        self._running = False
        self.telemetry_interval_ms = self._compute_interval_ms(telemetry_hz)
        self.check_service = check_service
        self._self_check_requested = False
        self._connected = False

    def run(self) -> None:  # noqa: D401
        """Worker loop emits telemetry placeholders over signals."""
        self._running = True
        self.status_message.emit("硬件线程已启动（占位）")
        self._run_self_check()
        while self._running:
            payload: dict[str, object] = {
                "connected": self._connected,
                "airflow": 0.0,
                "safety_state": "SAFE",
                "timestamp": time.time(),
            }
            self.telemetry_ready.emit(payload)
            if self._self_check_requested:
                self._self_check_requested = False
                self._run_self_check()
            self.msleep(self.telemetry_interval_ms)
        self.status_message.emit("硬件线程已停止")

    def stop(self) -> None:
        if not self.isRunning():
            return
        self._running = False
        self.wait(2000)

    def request_self_check(self) -> None:
        """Allow controller/UI to trigger another self-check without blocking UI."""
        self._self_check_requested = True

    def _run_self_check(self) -> None:
        if not self.check_service:
            return
        results, ready = self.check_service.run_checks()
        self._connected = ready
        self.self_check_completed.emit(results, ready)
        status = "硬件自检通过" if ready else "硬件自检失败，请检查连接"
        self.status_message.emit(status)
        LOG.info("硬件自检完成 | ready=%s | 项目数=%s", ready, len(results))

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
