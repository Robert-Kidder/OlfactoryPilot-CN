from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.models import (
    AppState,
    MaintenanceLeaseReleaseEvidence,
    SafeStopPlan,
    SafetyState,
    SelectorConfig,
)
from app.services.safety_manager import SafetyManager

if TYPE_CHECKING:
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
        actuation_worker=None,
        flow_worker=None,
        actuation_timeout_ms: int = 2000,
        emergency_close_timeout_ms: int = 500,
        selector: SelectorConfig | None = None,
        maintenance_handoff: Callable[[], bool] | None = None,
    ) -> None:
        self.state = state
        self.worker = worker
        self.safety_manager = safety_manager
        self.retry_limit = max(0, retry_limit)
        self.retry_interval = retry_interval
        self.record_path = record_path or DEFAULT_RECORD_PATH
        self._now = time_func or time.time
        self._sleep = sleep_func or time.sleep
        self.actuation_worker = actuation_worker
        self.flow_worker = flow_worker
        self.actuation_timeout_ms = max(1, int(actuation_timeout_ms))
        self.emergency_close_timeout_ms = max(1, int(emergency_close_timeout_ms))
        self.selector = selector or state.selector
        self.maintenance_handoff = maintenance_handoff
        if self.selector is None and state.master_valve_line:
            self.selector = SelectorConfig(state.master_valve_line)
        self._safe_stop_sequence = 0
        self._last_safe_stop_plan: SafeStopPlan | None = None
        self._last_a_zero_confirmed = False

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
            "safe_stop_status": "not_started",
            "a_zero_confirmed": False,
            "selector_safe_confirmed": False,
            "recovery_required": False,
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
            plan = self._last_safe_stop_plan
            if plan is not None:
                event["safe_stop_status"] = plan.status.value
                event["a_zero_confirmed"] = plan.a_zero_confirmed
                event["selector_safe_confirmed"] = plan.selector_confirmed
                event["recovery_required"] = plan.status.value == "recovery_required"
                if plan.recovery_reason and plan.recovery_reason not in errors:
                    errors.append(plan.recovery_reason)
            else:
                # Missing owners, a missing selector, or an unconfirmed owner
                # fence leaves the gas route unknowable.  Legacy direct-HAL
                # close cannot satisfy Story 4.5 evidence.
                event["safe_stop_status"] = "recovery_required"
                event["a_zero_confirmed"] = self._last_a_zero_confirmed
                event["recovery_required"] = True
            event["valves_closed"] = valves_closed
            event["heaters_off"] = heaters_off
            if errors:
                event["error"] = "; ".join(errors)
            if valves_closed and heaters_off and not errors:
                event["result"] = "success"
                break

            if attempt >= self.retry_limit:
                break
            if self.actuation_worker is not None:
                # Owner handoff is terminal for this process lifecycle.  A
                # second attempt would target stopped owners and cannot add evidence.
                break
            retries += 1
            event["retries"] = retries
            LOG.warning(
                "Shutdown retry %s/%s triggered: %s", retries, self.retry_limit, errors
            )
            self._sleep(self.retry_interval)

        if event["result"] != "success":
            event["result"] = (
                "recovery_required"
                if event.get("recovery_required")
                else "unsafe"
            )
            if not event["error"]:
                event["error"] = guard_reason or "关闭未完成，需人工检查"
            if errors and not guard_reason:
                LOG.error("Shutdown incomplete: %s", errors)

        self.state.hardware_ready = False
        self.state.telemetry.connected = False
        self.state.telemetry.timestamp = ts
        self.state.telemetry.safety_state = (
            "SAFE" if event["result"] == "success" else "RECOVERY_REQUIRED"
        )
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

        if self.actuation_worker is None:
            if guard_allowed or force:
                self.worker.stop()
            else:
                LOG.warning("Shutdown guard blocked: %s", guard_reason)

        return event

    def _attempt_shutdown_once(self) -> tuple[bool, bool, list[str]]:
        errors: list[str] = []
        self._last_a_zero_confirmed = False
        if self.actuation_worker is None:
            valves_closed = False
            errors.append(
                "RECOVERY_REQUIRED：ActuationWorker/FlowWorker safe-stop owners 不可用；"
                "未执行无 receipt 的 legacy 直接关阀。"
            )
        else:
            self._safe_stop_sequence += 1
            operation_id = f"safe-stop-{self._safe_stop_sequence}"
            try:
                identity = self.actuation_worker.fence_for_safe_stop(
                    operation_id=operation_id,
                    generation=self._safe_stop_sequence,
                    reason="全局安全停止已阻止新动作并失效旧 epoch。",
                    timeout_ms=self.actuation_timeout_ms,
                )
            except Exception as exc:  # pragma: no cover - defensive owner boundary
                identity = None
                errors.append(f"动作 owner safe-stop fence 异常：{exc}")
            plan = None
            if identity is None or self.selector is None:
                if identity is None:
                    errors.append("动作 owner 未确认 safe-stop fence/epoch 失效。")
                if self.selector is None:
                    errors.append("A 路三通 selector 未配置，状态不确定。")
            else:
                plan = SafeStopPlan(identity, self.selector)
            self._last_safe_stop_plan = plan

            a_receipt = None
            if identity is not None and self.flow_worker is not None:
                try:
                    a_receipt = self.flow_worker.zero_a_for_safe_stop(
                        identity,
                        self.actuation_timeout_ms,
                    )
                    if plan is not None and a_receipt is None:
                        plan.timeout("A 清零 receipt")
                    elif plan is not None and not a_receipt.command_id:
                        plan.require_recovery("A 清零 receipt command_id 无效。")
                    elif plan is not None:
                        plan.expect_a_zero(a_receipt.command_id)
                        plan.accept_a_zero(a_receipt)
                    self._last_a_zero_confirmed = bool(
                        a_receipt is not None
                        and a_receipt.success
                        and not a_receipt.stale
                        and abs(float(a_receipt.confirmed_a)) <= 1e-9
                        and a_receipt.identity == identity
                    )
                    if not self._last_a_zero_confirmed and plan is None:
                        errors.append("A 清零 receipt 未确认。")
                except Exception as exc:  # pragma: no cover - defensive owner boundary
                    if plan is not None:
                        plan.require_recovery(f"A 清零 receipt 异常或无效：{exc}")
                    else:
                        errors.append(f"A 清零 receipt 异常或无效：{exc}")
            elif identity is not None:
                if plan is not None:
                    plan.require_recovery("FlowWorker 不可用，A 清零未确认。")
                else:
                    errors.append("FlowWorker 不可用，A 清零未确认。")

            if plan is not None and plan.selector_allowed:
                try:
                    selector_receipt = self.actuation_worker.route_selector_safe(
                        plan,
                        self.emergency_close_timeout_ms,
                    )
                    if selector_receipt is None:
                        plan.timeout("selector 安全路线 receipt")
                    else:
                        plan.accept_selector(selector_receipt)
                except Exception as exc:  # pragma: no cover - defensive owner boundary
                    plan.require_recovery(f"selector 安全路线 receipt 异常或无效：{exc}")

            odors_closed = False
            if identity is not None:
                odors_closed = self._call_bool(
                    lambda: self.actuation_worker.close_odors_for_safe_stop(
                        identity,
                        self.emergency_close_timeout_ms,
                    ),
                    "ActuationWorker 气味阀 1-20 安全关闭",
                    errors,
                )
            flows_zero = False
            if identity is not None and self.flow_worker is not None:
                flows_zero = self._call_bool(
                    lambda: self.flow_worker.zero_all_for_safe_stop(
                        identity,
                        self.actuation_timeout_ms,
                    ),
                    "FlowWorker B/C 与终态流量清零",
                    errors,
                )
            if not flows_zero:
                errors.append("终态 A/B/C 清零未完整确认。")
            maintenance_handoff = (
                self.maintenance_handoff
                or self.actuation_worker.handoff_maintenance_for_safe_stop
            )
            maintenance_handed_off = self._call_bool(
                maintenance_handoff,
                "maintenance recorder/owner handoff",
                errors,
            )
            if not maintenance_handed_off:
                errors.append("maintenance recorder/owner 未交还。")
            do_handed_off = self._call_bool(
                lambda: self.actuation_worker.shutdown(self.actuation_timeout_ms),
                "停止 ActuationWorker/交还 DO ownership",
                errors,
            )
            if not do_handed_off:
                errors.append("DO ownership 未在超时内交还；禁止跨线程复用旧 task，需人工确认。")
            elif not odors_closed:
                odors_closed = self._call_bool(
                    self.actuation_worker.fallback_close_all_after_handoff,
                    "DO ownership 交还后的气味阀兜底关闭",
                    errors,
                )
            lease_released = False
            if identity is not None and flows_zero and self.flow_worker is not None:
                lease_released = self._call_bool(
                    lambda: self.flow_worker.release_lease_for_safe_stop(
                        identity,
                        MaintenanceLeaseReleaseEvidence(
                            operation_terminal=True,
                            all_targets_closed=odors_closed and flows_zero,
                            owner_handoff=maintenance_handed_off and do_handed_off,
                        ),
                    ),
                    "释放 FlowWorker device lease",
                    errors,
                )
            if not lease_released:
                errors.append("FlowWorker device lease 未交还。")
            valves_closed = False
        heaters_off = self._call_bool(self.worker.stop_heaters, "停止加热", errors)
        self._call_optional(self.worker.flush_logs, "flush 日志", errors)
        if self.actuation_worker is None:
            resources_released = self._call_bool(
                self.worker.release_resources,
                "释放 HardwareWorker AI 资源",
                errors,
            )
            if not resources_released:
                errors.append("HardwareWorker AI 资源未在超时内释放。")
        else:
            ai_released = self._call_bool(
                self.worker.release_ai_resources,
                "释放 AI",
                errors,
            )
            if not ai_released:
                errors.append("HardwareWorker AI 资源未在超时内释放。")
            if self.flow_worker is not None:
                serial_stopped = self._call_bool(
                    lambda: self.flow_worker.shutdown(self.actuation_timeout_ms),
                    "释放 serial owner",
                    errors,
                )
                if not serial_stopped:
                    errors.append("serial owner 未在超时内停止。")
            else:
                serial_stopped = False
            if plan is not None:
                plan.complete(
                    odors_closed=odors_closed and flows_zero,
                    owners_handed_off=(
                        do_handed_off
                        and maintenance_handed_off
                        and lease_released
                        and ai_released
                        and serial_stopped
                    ),
                )
                valves_closed = bool(odors_closed and plan.safe_terminal)
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
