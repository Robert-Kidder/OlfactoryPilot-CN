from __future__ import annotations

import math
from typing import Callable

from app.models import SafetyState


class SafetyManager:
    """安全管理器：判定气流安全状态并提供命令前置守卫。"""

    def __init__(
        self,
        low_flow_threshold: float = 0.2,
        recovery_margin: float = 0.05,
        stale_after_s: float = 1.0,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        self.low_flow_threshold = low_flow_threshold
        self.recovery_margin = recovery_margin
        self.stale_after_s = stale_after_s
        self._now = now_func

    def is_safe(self, airflow: float) -> bool:
        """简化检查，保留向后兼容。"""
        return airflow >= self.low_flow_threshold

    def validate_threshold(self, value: float) -> tuple[bool, str]:
        """阈值校验：必须为正、有穷、不过大。"""
        if value is None:
            return False, "无效阈值：为空"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, "无效阈值：不是数字"
        if math.isnan(v) or math.isinf(v):
            return False, "无效阈值：不是有限数"
        if v <= 0:
            return False, "无效阈值：必须大于 0"
        if v > 1000:
            return False, "无效阈值：过大"
        return True, "阈值有效"

    def evaluate(
        self,
        airflow: float,
        *,
        timestamp: float | None = None,
        previous_state: str = "SAFE",
        hardware_state: str | None = None,
    ) -> str:
        """兼容 API，仅返回状态字符串。"""
        state = self.evaluate_state(
            airflow=airflow,
            timestamp=timestamp if timestamp is not None else (self._now() if self._now else 0.0),
            previous=self._from_string(previous_state, timestamp),
            hardware_state=hardware_state,
        )
        return state.state

    def guard_command(
        self,
        *,
        safety_state: SafetyState,
        hardware_ready: bool,
        action: str,
        source: str | None = None,
    ) -> tuple[bool, str]:
        """命令前置守卫：返回是否允许和原因。"""
        prefix = f"{source}·{action}" if source else action
        if not hardware_ready:
            return False, f"硬件未就绪，阻断 {prefix}"
        if safety_state.state == "SAFE":
            return True, "允许执行"
        if safety_state.state == "DATA_STALE":
            return False, f"气流数据过期，阻断 {prefix}"
        return False, f"安全阻断：{safety_state.reason or safety_state.state}"

    def evaluate_state(
        self,
        *,
        airflow: float,
        timestamp: float,
        previous: SafetyState | None = None,
        hardware_state: str | None = None,
    ) -> SafetyState:
        """综合阈值、滞后、过期判定。"""
        prev_state_value = (
            previous.state if previous and previous.state in {"SAFE", "LOW_FLOW"} else "LOW_FLOW"
        )
        prev_timestamp = previous.updated_at if previous else timestamp

        safe_airflow = self._coerce_airflow(airflow)
        current_airflow = 0.0 if safe_airflow is None else safe_airflow

        if hardware_state and hardware_state != "SAFE":
            return SafetyState(
                state=hardware_state,
                airflow=current_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason=f"硬件上报安全状态 {hardware_state}",
            )

        if safe_airflow is None:
            return SafetyState(
                state="DATA_STALE",
                airflow=0.0,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason="气流读数异常，默认低流/阻断",
            )

        if (
            previous
            and previous.state not in {"SAFE", "LOW_FLOW", "DATA_STALE"}
            and hardware_state is None
        ):
            return SafetyState(
                state=previous.state,
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason=previous.reason or f"硬件上报安全状态 {previous.state}",
            )

        if timestamp - prev_timestamp > self.stale_after_s:
            return SafetyState(
                state="DATA_STALE",
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason="气流数据过期，保持安全阻断",
            )

        if safe_airflow < self.low_flow_threshold:
            base_state = SafetyState(
                state="LOW_FLOW",
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason=f"气流低于阈值 {self.low_flow_threshold:.2f}",
            )
        elif safe_airflow >= self.low_flow_threshold + self.recovery_margin:
            base_state = SafetyState(
                state="SAFE",
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason="气流正常",
            )
        else:
            base_state = SafetyState(
                state=prev_state_value,
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason="稳定观察，继续工作悬停",
            )

        if hardware_state and hardware_state != "SAFE":
            return SafetyState(
                state=hardware_state,
                airflow=safe_airflow,
                threshold=self.low_flow_threshold,
                updated_at=timestamp,
                reason=f"硬件上报安全状态 {hardware_state}",
            )

        return base_state

    @staticmethod
    def _coerce_airflow(value: float) -> float | None:
        try:
            if value is None:
                return None
            if isinstance(value, (int, float)) and not (math.isnan(value) or math.isinf(value)):
                return float(value)
            return None
        except (TypeError, ValueError):
            return None

    def _from_string(self, state: str, timestamp: float | None) -> SafetyState:
        return SafetyState(
            state=state if state in {"SAFE", "LOW_FLOW"} else "LOW_FLOW",
            airflow=0.0,
            threshold=self.low_flow_threshold,
            updated_at=timestamp if timestamp is not None else 0.0,
            reason="传统交互接口",
        )
