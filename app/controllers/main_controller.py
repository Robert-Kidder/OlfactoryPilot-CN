from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Slot

from app.models import AppState
from app.workers import HardwareWorker

if TYPE_CHECKING:
    from app.views import MainWindow

LOG = logging.getLogger(__name__)


class MainController(QObject):
    def __init__(self, state: AppState, worker: HardwareWorker, safety_manager=None) -> None:
        super().__init__()
        self.state = state
        self.worker = worker
        self.safety_manager = safety_manager
        self.view: MainWindow | None = None
        self.worker.telemetry_ready.connect(self.handle_telemetry)
        self.worker.status_message.connect(self.handle_status)
        self.worker.self_check_completed.connect(self.handle_self_check)

    def bind_view(self, view: MainWindow) -> None:
        self.view = view
        self._apply_safety_check(initial=True)
        self.view.update_status(self.state.status_message)
        self.view.render_telemetry(self.state.telemetry)

    def start_worker(self) -> None:
        if not self.worker.isRunning():
            LOG.info("Starting hardware worker thread")
            self.worker.start()

    def shutdown(self) -> None:
        LOG.info("Shutting down worker thread")
        self.worker.stop()

    @Slot(dict)
    def handle_telemetry(self, payload: dict) -> None:
        previous_safety_state = self.state.telemetry.safety_state
        hardware_safety = payload.get("safety_state")
        self.state.update_telemetry(payload)

        self._apply_safety_check(
            hardware_safety=hardware_safety,
            previous_state_override=previous_safety_state,
        )
        if self.view:
            self.view.render_telemetry(self.state.telemetry)
            self.view.update_status(self.state.status_message)

    @Slot(str)
    def handle_status(self, message: str) -> None:
        self.state.update_status(message)
        if self.view:
            self.view.update_status(message)

    @Slot(list, bool)
    def handle_self_check(self, results: list, hardware_ready: bool) -> None:
        self.state.update_self_check(results, hardware_ready)
        status = "硬件自检通过" if hardware_ready else "硬件自检失败，请检查连接"
        self.state.update_status(status)
        if self.view:
            self.view.update_status(status)
            self.view.render_self_check(self.state.self_check_results, hardware_ready)

    def request_self_check(self) -> None:
        """Trigger self-check from UI, keep thread-safe."""
        self.state.update_status("正在重新自检...")
        if self.view:
            self.view.update_status(self.state.status_message)
        self.worker.request_self_check()

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
            return

        airflow = telemetry.airflow
        previous_state = previous_state_override or telemetry.safety_state
        base_prev_flow_state = getattr(self, "_last_flow_state", None)
        if base_prev_flow_state is None:
            if hardware_safety and hardware_safety != "SAFE":
                base_prev_flow_state = "LOW_FLOW"
            else:
                base_prev_flow_state = (
                    previous_state if previous_state in {"SAFE", "LOW_FLOW"} else "LOW_FLOW"
                )
        flow_state = self.safety_manager.evaluate(airflow, base_prev_flow_state)
        self._last_flow_state = flow_state

        combined_state = (
            hardware_safety if hardware_safety and hardware_safety != "SAFE" else flow_state
        )
        telemetry.safety_state = combined_state

        should_update_status = combined_state != previous_state or (
            initial and combined_state == "LOW_FLOW"
        )
        if not should_update_status:
            return

        if combined_state == "SAFE":
            self.state.update_status("气流恢复正常")
        elif combined_state == "LOW_FLOW":
            self.state.update_status(
                f"警告：气流低于阈值（当前 {airflow:.2f} < "
                f"{self.safety_manager.low_flow_threshold:.2f}）"
            )
        else:
            self.state.update_status(f"警告：硬件报告安全状态 {combined_state}")
