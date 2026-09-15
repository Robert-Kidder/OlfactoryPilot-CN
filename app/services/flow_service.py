from __future__ import annotations

import logging
import math
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
    a_setpoint_readback_sccm: float | None = None
    b_setpoint_readback_sccm: float | None = None
    c_setpoint_readback_sccm: float | None = None
    first_nonzero_tx_monotonic_ns: int | None = None
    zero_confirmed: bool = False
    recovery_required: bool = False


class FlowService:
    """封装 MFC 流量写入与日志记录。"""

    def __init__(
        self,
        hal: HalInterface,
        *,
        logger_name: str = "flow_events",
        master_target: str | None = None,
        master_writer=None,
        zero_setpoint_tolerance_sccm: float = 1e-9,
    ) -> None:
        self.hal = hal
        self._logger = logging.getLogger(logger_name)
        self.master_target = master_target
        self.master_writer = master_writer
        tolerance = float(zero_setpoint_tolerance_sccm)
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("zero setpoint tolerance must be finite and non-negative")
        self._zero_setpoint_tolerance_sccm = tolerance

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

        if self._serial_desynchronized():
            return self._failure(
                "Alicat serial 已失去同步；禁止继续 TX，零流量无法确认，请现场停止或断电。",
                a_target,
                b_target,
                c_target,
                a_comp,
                "serial_desync",
                mode,
                recovery_required=True,
            )

        applied_channels: list[tuple[str, float, bool]] = []
        readbacks: dict[str, float | None] = {"A": None, "B": None, "C": None}
        try:
            applied_channels.append(("B", float(b_target), False))
            if not self.hal.set_flow("B", float(b_target), comp=False):
                rollback_confirmed = self._rollback_flows(applied_channels)
                return self._write_failure("B", a_target, b_target, c_target, a_comp, mode, rollback_confirmed)
            readbacks["B"] = self._setpoint_readback("B")
            applied_channels.append(("C", float(c_target), False))
            if not self.hal.set_flow("C", float(c_target), comp=False):
                rollback_confirmed = self._rollback_flows(applied_channels)
                return self._write_failure("C", a_target, b_target, c_target, a_comp, mode, rollback_confirmed)
            readbacks["C"] = self._setpoint_readback("C")
            applied_channels.append(("A", a_comp, mode == "rest"))
            if not self.hal.set_flow("A", a_comp, comp=(mode == "rest")):
                rollback_confirmed = self._rollback_flows(applied_channels)
                return self._write_failure("A", a_target, b_target, c_target, a_comp, mode, rollback_confirmed)
            readbacks["A"] = self._setpoint_readback("A")
        except TimeoutError:
            rollback_confirmed = self._rollback_flows(applied_channels)
            return self._failure(
                "串口超时，未能写入流量",
                a_target,
                b_target,
                c_target,
                a_comp,
                "timeout",
                mode,
                recovery_required=self._serial_desynchronized() or not rollback_confirmed,
            )
        except Exception as exc:  # pragma: no cover - defensive
            rollback_confirmed = self._rollback_flows(applied_channels)
            return self._failure(
                f"串口不可用或写入异常：{exc}",
                a_target,
                b_target,
                c_target,
                a_comp,
                "exception",
                mode,
                recovery_required=self._serial_desynchronized() or not rollback_confirmed,
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
            a_setpoint_readback_sccm=readbacks["A"],
            b_setpoint_readback_sccm=readbacks["B"],
            c_setpoint_readback_sccm=readbacks["C"],
            first_nonzero_tx_monotonic_ns=self._first_nonzero_tx(
                {"A": a_comp, "B": float(b_target), "C": float(c_target)}
            ),
            zero_confirmed=self._readbacks_confirm_zero(readbacks),
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
        *,
        recovery_required: bool = False,
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
            zero_confirmed=False,
            recovery_required=recovery_required,
        )

    def _write_failure(
        self,
        channel: str,
        a_target: float,
        b_target: float,
        c_target: float,
        a_comp: float,
        mode: str,
        rollback_confirmed: bool,
    ) -> FlowApplyResult:
        desynchronized = self._serial_desynchronized()
        message = (
            "Alicat serial 已失去同步；禁止 rollback/zero 新 TX，零流量无法确认，请现场停止或断电。"
            if desynchronized
            else f"{channel} 通道 setpoint 未确认，请检查 Alicat unit ID/响应"
        )
        return self._failure(
            message,
            a_target,
            b_target,
            c_target,
            a_comp,
            "serial_desync" if desynchronized else "write_failed",
            mode,
            recovery_required=desynchronized or not rollback_confirmed,
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
        if self._serial_desynchronized():
            return self._failure(
                "Alicat serial 已失去同步；禁止发送 A=0，零流量无法确认，请现场停止或断电。",
                0.0,
                0.0,
                0.0,
                0.0,
                "serial_desync",
                "safe_stop_a_zero",
                recovery_required=True,
            )
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
                (
                    "Alicat serial 已失去同步；禁止继续 TX，A=0 无法确认，请现场停止或断电。"
                    if self._serial_desynchronized()
                    else "A 通道 setpoint=0 未确认。"
                ),
                0.0,
                0.0,
                0.0,
                0.0,
                "serial_desync" if self._serial_desynchronized() else "write_failed",
                "safe_stop_a_zero",
                recovery_required=self._serial_desynchronized(),
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
        readback = self._setpoint_readback("A")
        return FlowApplyResult(
            True,
            "A 流量已清零",
            0.0,
            0.0,
            0.0,
            0.0,
            a_setpoint_readback_sccm=readback,
        )

    def _setpoint_readback(self, channel: str) -> float | None:
        reader = getattr(self.hal, "last_setpoint_readback_sccm", None)
        if not callable(reader):
            return None
        try:
            value = reader(channel)
            return None if value is None else float(value)
        except (TypeError, ValueError, OverflowError):
            return None

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

    def _rollback_flows(self, applied: list[tuple[str, float, bool]]) -> bool:
        """Best-effort rollback to zero on partial failure."""
        if self._serial_desynchronized():
            self._logger.critical(
                "serial desync latched; rollback suppressed and physical zero is uncertain"
            )
            return False
        confirmed = True
        for channel, _value, _comp in reversed(applied):
            try:
                if not self.hal.set_flow(channel, 0.0, comp=False):
                    confirmed = False
                    if self._serial_desynchronized():
                        break
                elif not self._readback_confirms_zero(channel):
                    confirmed = False
            except Exception:  # pragma: no cover - defensive
                self._logger.warning("rollback failed for channel %s", channel)
                confirmed = False
                if self._serial_desynchronized():
                    break
        return confirmed

    def _readback_confirms_zero(self, channel: str) -> bool:
        value = self._setpoint_readback(channel)
        return bool(
            value is not None
            and math.isfinite(value)
            and abs(value) <= self._zero_setpoint_tolerance_sccm
        )

    def _readbacks_confirm_zero(self, readbacks: dict[str, float | None]) -> bool:
        return all(
            value is not None
            and math.isfinite(float(value))
            and abs(float(value)) <= self._zero_setpoint_tolerance_sccm
            for value in (readbacks["A"], readbacks["B"], readbacks["C"])
        )

    def _serial_desynchronized(self) -> bool:
        return bool(getattr(self.hal, "serial_desynchronized", False))

    def _first_nonzero_tx(self, targets: dict[str, float]) -> int | None:
        reader = getattr(self.hal, "last_setpoint_tx_monotonic_ns", None)
        if not callable(reader):
            return None
        timestamps = []
        for channel, target in targets.items():
            if abs(float(target)) <= 1e-9:
                continue
            try:
                timestamp = reader(channel)
            except Exception:
                timestamp = None
            if timestamp is not None:
                timestamps.append(int(timestamp))
        return min(timestamps) if timestamps else None

    @staticmethod
    def _split_target(target: str) -> tuple[str | None, str]:
        if "/" in target:
            return tuple(target.split("/", 1))  # type: ignore[return-value]
        return None, target
