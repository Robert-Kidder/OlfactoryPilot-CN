from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from app.models import AppState, SafetyState
from app.services.safety_manager import SafetyManager
from app.workers import HardwareWorker

LOG = logging.getLogger(__name__)
DEFAULT_RECORD_PATH = Path.home() / ".olfactorypilot" / "last_shutdown_event.json"


class ShutdownService:
    """统一安全关闭流程，操作 HardwareWorker 并记录事件。"""

    def __init__(
        self,
        *,
        state: AppState,
        worker: HardwareWorker,
        safety_manager: SafetyManager | None = None,
        retry_limit: int = 2,
        retry_interval: float = 0.2,
        record_path: Path | None = None,
        time_func: Callable[[], float] | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self.state = state
        self.worker = worker
        self.safety_manager = safety_manager
        self.retry_limit = max(0, retry_limit)
        self.retry_interval = retry_interval
        self.record_path = record_path or DEFAULT_RECORD_PATH
        self._now = time_func or time.time
        self._sleep = sleep_func or time.sleep

    def shutdown(
        self,
        *,
        source: str,
        reason: str | None = None,
        airflow: float | None = None,
        force: bool = True,
    ) -> dict:
        """执行控制链路关闭，将结果持久化到文件和 AppState。"""
        ts = self._now()
        airflow_value = airflow if airflow is not None else self.state.telemetry.airflow
        event = {
            "ts": ts,
            "timestamp": ts,
            "source": source,
            "reason": reason or "",
            "state": self.state.telemetry.safety_state,
            "airflow": float(airflow_value),
            "threshold": float(getattr(self.safety_manager, "low_flow_threshold", 0.0)),
            "valves_closed": False,
            "heaters_off": False,
            "retries": 0,
            "result": "pending",
            "error": "",
        }

        guard_allowed, guard_reason = True, ""
        if self.safety_manager:
            telemetry = self.state.telemetry
            safety_state = SafetyState(
                state=telemetry.safety_state,
                airflow=telemetry.airflow,
                threshold=self.safety_manager.low_flow_threshold,
                updated_at=telemetry.timestamp or ts,
                reason=telemetry.safety_reason or "",
            )
            guard_allowed, guard_reason = self.safety_manager.guard_command(
                safety_state=safety_state,
                hardware_ready=self.state.hardware_ready,
                action="Shutdown",
                source=source,
            )
            event["guard_allowed"] = guard_allowed
            event["guard_reason"] = guard_reason

        errors: list[str] = []
        retries = 0
        for attempt in range(self.retry_limit + 1):
            valves_closed, heaters_off, errors = self._attempt_shutdown_once()
            event["valves_closed"] = valves_closed
            event["heaters_off"] = heaters_off
            if errors:
                event["error"] = "; ".join(errors)
            if valves_closed and heaters_off and not errors:
                event["result"] = "success"
                break

            if attempt >= self.retry_limit:
                break
            retries += 1
            event["retries"] = retries
            LOG.warning(
                "Shutdown retry %s/%s triggered: %s", retries, self.retry_limit, errors
            )
            self._sleep(self.retry_interval)

        if event["result"] != "success":
            event["result"] = "unsafe"
            if not event["error"]:
                event["error"] = guard_reason or "关闭未完成，需人工检查"
            if errors and not guard_reason:
                LOG.error("Shutdown incomplete: %s", errors)

        self.state.hardware_ready = False
        self.state.telemetry.connected = False
        self.state.telemetry.timestamp = ts
        self.state.telemetry.safety_state = "SAFE" if event["result"] == "success" else "DATA_STALE"
        self.state.telemetry.safety_reason = (
            "已安全关闭" if event["result"] == "success" else "关闭未完成，请重新自检"
        )
        self.state.last_shutdown_event = event
        self._persist_event(event)
        LOG.info(
            "Shutdown event | source=%s | result=%s | valves_closed=%s | heaters_off=%s | retries=%s | error=%s",
            source,
            event["result"],
            event["valves_closed"],
            event["heaters_off"],
            event["retries"],
            event["error"],
        )

        if guard_allowed or force:
            self.worker.stop()
        else:
            LOG.warning("Shutdown guard blocked: %s", guard_reason)

        return event

    def _attempt_shutdown_once(self) -> tuple[bool, bool, list[str]]:
        errors: list[str] = []
        valves_closed = self._call_bool(self.worker.close_all_channels, "关闭阀门", errors)
        heaters_off = self._call_bool(self.worker.stop_heaters, "停止加热", errors)
        self._call_optional(self.worker.flush_logs, "flush 日志", errors)
        self._call_optional(self.worker.release_resources, "释放NI/RS232", errors)
        return valves_closed, heaters_off, errors

    def _call_bool(self, func: Callable[[], bool], label: str, errors: list[str]) -> bool:
        try:
            return bool(func())
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{label} 失败: {exc}")
            return False

    def _call_optional(self, func: Callable[[], object], label: str, errors: list[str]) -> None:
        try:
            func()
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{label} 异常: {exc}")

    def _persist_event(self, event: dict) -> None:
        try:
            path = self.record_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(event, handle, ensure_ascii=False, indent=2)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to persist shutdown event to %s", self.record_path)

    @classmethod
    def load_last_event(cls, record_path: Path | None = None) -> dict | None:
        path = record_path or DEFAULT_RECORD_PATH
        try:
            if not path.exists():
                return None
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            LOG.warning("Failed to load shutdown record from %s", path)
            return None
