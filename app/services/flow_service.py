from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.services.hal import HalInterface


@dataclass
class FlowApplyResult:
    success: bool
    message: str
    a: float
    b: float
    c: float
    a_comp: float
    error: str | None = None


class FlowService:
    """封装 MFC 流量写入与日志记录。"""

    def __init__(
        self,
        hal: HalInterface,
        *,
        logger_name: str = "flow_events",
        master_target: str | None = None,
        master_writer=None,
    ) -> None:
        self.hal = hal
        self._logger = logging.getLogger(logger_name)
        self.master_target = master_target
        self.master_writer = master_writer

    def apply_flows(
        self,
        *,
        a_target: float,
        b_target: float,
        c_target: float,
        mode: str = "rest",
    ) -> FlowApplyResult:
        """设置 A/B/C 目标流量并记录事件。

        Rest 模式下 A_comp = A + C，按 B -> C -> A_comp 顺序写入。
        """
        a_comp = float(a_target) + float(c_target) if mode == "rest" else float(a_target)
        payload = {
            "ts": time.time(),
            "mode": mode,
            "a_target": float(a_target),
            "b_target": float(b_target),
            "c_target": float(c_target),
            "a_comp": a_comp,
        }

        applied_channels: list[tuple[str, float, bool]] = []
        try:
            if not self.hal.set_flow("B", float(b_target), comp=False):
                self._rollback_flows(applied_channels)
                return self._failure("B 通道 setpoint 未确认，请检查 Alicat unit ID/响应", a_target, b_target, c_target, a_comp, "write_failed", mode)
            applied_channels.append(("B", float(b_target), False))
            if not self.hal.set_flow("C", float(c_target), comp=False):
                self._rollback_flows(applied_channels)
                return self._failure("C 通道 setpoint 未确认，请检查 Alicat unit ID/响应", a_target, b_target, c_target, a_comp, "write_failed", mode)
            applied_channels.append(("C", float(c_target), False))
            if not self.hal.set_flow("A", a_comp, comp=(mode == "rest")):
                self._rollback_flows(applied_channels)
                return self._failure("A 通道 setpoint 未确认，请检查 Alicat unit ID/响应", a_target, b_target, c_target, a_comp, "write_failed", mode)
            applied_channels.append(("A", a_comp, mode == "rest"))
        except TimeoutError:
            self._rollback_flows(applied_channels)
            return self._failure("串口超时，未能写入流量", a_target, b_target, c_target, a_comp, "timeout", mode)
        except Exception as exc:  # pragma: no cover - defensive
            self._rollback_flows(applied_channels)
            return self._failure(
                f"串口不可用或写入异常：{exc}",
                a_target,
                b_target,
                c_target,
                a_comp,
                "exception",
                mode,
            )

        payload["result"] = "success"
        self._logger.info("flow_event | %s", payload)
        return FlowApplyResult(
            success=True,
            message="流量已应用",
            a=float(a_target),
            b=float(b_target),
            c=float(c_target),
            a_comp=a_comp,
        )

    def _failure(
        self,
        message: str,
        a_target: float,
        b_target: float,
        c_target: float,
        a_comp: float,
        error: str,
        mode: str = "rest",
    ) -> FlowApplyResult:
        payload = {
            "ts": time.time(),
            "mode": mode,
            "a_target": float(a_target),
            "b_target": float(b_target),
            "c_target": float(c_target),
            "a_comp": a_comp,
            "result": "failure",
            "error": error,
            "message": message,
        }
        self._logger.warning("flow_event | %s", payload)
        return FlowApplyResult(
            success=False,
            message=message,
            a=float(a_target),
            b=float(b_target),
            c=float(c_target),
            a_comp=a_comp,
            error=error,
        )

    def apply_rest(
        self,
        *,
        a_target: float,
        b_target: float,
        c_target: float,
    ) -> FlowApplyResult:
        """Rest：写入静息流量，主阀保持常开。"""
        result = self.apply_flows(a_target=a_target, b_target=b_target, c_target=c_target, mode="rest")
        return result

    def apply_stim_start(
        self,
        *,
        a_target: float,
        b_target: float,
    ) -> FlowApplyResult:
        """Stim 开始：B 保持，C=0，A=A_target，主阀保持常开。"""
        result = self.apply_flows(a_target=a_target, b_target=b_target, c_target=0.0, mode="stim_start")
        return result

    def apply_stim_end(
        self,
        *,
        a_target: float,
        b_target: float,
        c_target: float,
    ) -> FlowApplyResult:
        """Stim 结束：恢复 Rest 顺序，主阀保持常开。"""
        result = self.apply_rest(a_target=a_target, b_target=b_target, c_target=c_target)
        return result

    def apply_zero(self) -> FlowApplyResult:
        """Startup/idle hard reset: set all MFC setpoints to zero."""
        result = self.apply_flows(a_target=0.0, b_target=0.0, c_target=0.0, mode="zero")
        if result.success:
            result.message = "流量已清零"
        return result

    def apply_a_zero(self) -> FlowApplyResult:
        """Confirm only MFC A=0; SafeStopPlan gates selector routing on this ack."""
        try:
            success = bool(self.hal.set_flow("A", 0.0, comp=False))
        except TimeoutError:
            return self._failure(
                "A 通道清零超时，未确认 setpoint。",
                0.0,
                0.0,
                0.0,
                0.0,
                "timeout",
                "safe_stop_a_zero",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return self._failure(
                f"A 通道清零异常：{exc}",
                0.0,
                0.0,
                0.0,
                0.0,
                "exception",
                "safe_stop_a_zero",
            )
        if not success:
            return self._failure(
                "A 通道 setpoint=0 未确认。",
                0.0,
                0.0,
                0.0,
                0.0,
                "write_failed",
                "safe_stop_a_zero",
            )
        self._logger.info(
            "flow_event | %s",
            {
                "ts": time.time(),
                "mode": "safe_stop_a_zero",
                "a_target": 0.0,
                "result": "success",
            },
        )
        return FlowApplyResult(True, "A 流量已清零", 0.0, 0.0, 0.0, 0.0)

    def _set_master(self, state: bool) -> None:
        """主阀常开：流量写入不切换主阀。保留占位返回 True。"""
        return True

    def _ensure_master_open(self) -> bool:
        device, line = self._split_target(self.master_target)  # type: ignore[arg-type]
        try:
            return bool(self.master_writer(device=device, line=line, state=True))  # type: ignore[misc]
        except Exception:  # pragma: no cover - defensive
            self._logger.exception("master valve write failed")
            return False

    def _rollback_flows(self, applied: list[tuple[str, float, bool]]) -> None:
        """Best-effort rollback to zero on partial failure."""
        for channel, _value, _comp in reversed(applied):
            try:
                self.hal.set_flow(channel, 0.0, comp=False)
            except Exception:  # pragma: no cover - defensive
                self._logger.warning("rollback failed for channel %s", channel)

    @staticmethod
    def _split_target(target: str) -> tuple[str | None, str]:
        if "/" in target:
            return tuple(target.split("/", 1))  # type: ignore[return-value]
        return None, target
