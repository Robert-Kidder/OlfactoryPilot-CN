from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models import ActuationReceipt, ActuationResult, AppState, SafetyState

if TYPE_CHECKING:
    from app.services import SafetyManager
    from app.workers import HardwareWorker


@dataclass(frozen=True, slots=True)
class ValvePlanStep:
    logical_valve: int
    device: str | None
    line: str
    state: bool
    role: str


@dataclass(frozen=True, slots=True)
class ValveWritePlan:
    requested_valve: int
    requested_state: bool
    safety_close: bool
    steps: tuple[ValvePlanStep, ...]


class ValveService:
    """阀门控制服务：封装安全检查、映射解析与主阀联动。"""

    def __init__(
        self,
        *,
        state: AppState,
        safety_manager: SafetyManager,
        worker: HardwareWorker,
        valve_variants: dict[str, dict[int, str]],
        hardware_variant: str,
        master_valve_line: str = "",
    ) -> None:
        self.state = state
        self.safety_manager = safety_manager
        self.worker = worker
        self.valve_variants = valve_variants or {}
        self.hardware_variant = hardware_variant
        self.master_valve_line = master_valve_line
        self._master_always_on = bool(master_valve_line)
        self._states: dict[int | str, bool] = {}
        self._state_lock = threading.RLock()
        self._logger = logging.getLogger("valve_events")

    def reset_cached_state(self) -> None:
        """Clear cached valve states so master will be re-driven after reconnect."""
        with self._state_lock:
            self._states.clear()

    def active_map(self) -> dict[int, str]:
        """当前硬件变体的通道 -> 线路映射。"""
        return self.valve_variants.get(self.hardware_variant, {})

    def is_open(self, channel_id: int) -> bool:
        with self._state_lock:
            return bool(self._states.get(int(channel_id), False))

    def master_is_open(self) -> bool:
        with self._state_lock:
            return bool(self._states.get("master", False))

    def resolve_target(self, channel_id: int) -> tuple[str | None, str]:
        channel_id = int(channel_id)
        target = self.master_valve_line if channel_id == 0 else self.active_map().get(channel_id)
        if not target:
            raise ValueError(f"阀门 {channel_id} 未配置映射")
        return self._split_target(target)

    def emergency_close_steps(self) -> tuple[ValvePlanStep, ...]:
        """Return every configured DO target once, including the master valve."""
        steps: list[ValvePlanStep] = []
        seen: set[tuple[str | None, str]] = set()
        if self.master_valve_line:
            device, line = self._split_target(self.master_valve_line)
            seen.add((device, line))
            steps.append(ValvePlanStep(0, device, line, False, "master_safety_close"))
        for logical_valve, target in sorted(self.active_map().items()):
            device, line = self._split_target(target)
            if (device, line) in seen:
                continue
            seen.add((device, line))
            steps.append(
                ValvePlanStep(int(logical_valve), device, line, False, "odor_safety_close")
            )
        return tuple(steps)

    def plan_master_prepare(
        self,
        *,
        safety_state: SafetyState | None = None,
    ) -> tuple[bool, ValveWritePlan | str]:
        """Build the owner-thread plan that must complete before protocol arming."""
        if not self.master_valve_line or self.master_is_open():
            return True, ValveWritePlan(0, True, False, ())
        if not self.state.flow_setpoints_ready:
            return False, "MFC 流量设定尚未建立，已阻止主阀预备"
        safety = safety_state or self._build_safety_state()
        allowed, reason = self.safety_manager.guard_command(
            safety_state=safety,
            hardware_ready=self._is_hardware_ready(),
            action="master-prepare",
            source="protocol-start",
        )
        if not allowed:
            return False, reason
        device, line = self._split_target(self.master_valve_line)
        return True, ValveWritePlan(
            requested_valve=0,
            requested_state=True,
            safety_close=False,
            steps=(ValvePlanStep(0, device, line, True, "master_prepare"),),
        )

    def plan_valve(
        self,
        channel_id: int,
        state: bool,
        *,
        safety_state: SafetyState | None = None,
        safety_close: bool = False,
    ) -> tuple[bool, ValveWritePlan | str]:
        channel_id = int(channel_id)
        mapping = self.active_map()
        if not mapping:
            return False, "未找到 20 通道映射，已阻断写入"
        target = mapping.get(channel_id)
        if not target:
            return False, f"阀门 {channel_id} 未配置映射"
        if safety_close and state:
            return False, "安全关闭参数不能用于打开阀门，已阻断写入"
        safety = safety_state or self._build_safety_state()
        if state and not self.state.flow_setpoints_ready:
            return False, "MFC 流量设定尚未建立，已阻断阀门打开"
        if not safety_close:
            allowed, reason = self.safety_manager.guard_command(
                safety_state=safety,
                hardware_ready=self._is_hardware_ready(),
                action=f"valve-{channel_id}",
                source="actuation-plan",
            )
            if not allowed:
                return False, reason
        steps: list[ValvePlanStep] = []
        if state and self.master_valve_line and not self.master_is_open():
            device, line = self._split_target(self.master_valve_line)
            steps.append(ValvePlanStep(0, device, line, True, "master_prepare"))
        device, line = self._split_target(target)
        steps.append(ValvePlanStep(channel_id, device, line, bool(state), "odor"))
        return True, ValveWritePlan(
            requested_valve=channel_id,
            requested_state=bool(state),
            safety_close=bool(safety_close),
            steps=tuple(steps),
        )

    def commit_receipt(self, receipt: ActuationReceipt) -> None:
        if receipt.result != ActuationResult.SUCCESS:
            return
        with self._state_lock:
            key: int | str = "master" if receipt.valve == 0 else receipt.valve
            self._states[key] = receipt.action.value == "open"

    def set_valve(
        self,
        channel_id: int,
        state: bool,
        *,
        safety_state: SafetyState | None = None,
        safety_close: bool = False,
    ) -> tuple[bool, str]:
        """按映射写入阀门状态，并在安全状态不满足时拦截。"""
        channel_id = int(channel_id)
        mapping = self.active_map()
        if not mapping:
            return False, "未找到 20 通道映射，已阻断写入"
        target = mapping.get(channel_id)
        if not target:
            return False, f"阀门 {channel_id} 未配置映射"

        if safety_close and state:
            return False, "安全关闭参数不能用于打开阀门，已阻断写入"

        safety = safety_state or self._build_safety_state()
        if state and not self.state.flow_setpoints_ready:
            return False, "MFC 流量设定尚未建立，已阻断阀门打开"
        if not safety_close:
            allowed, reason = self.safety_manager.guard_command(
                safety_state=safety,
                hardware_ready=self._is_hardware_ready(),
                action=f"valve-{channel_id}",
                source="manual-toggle",
            )
            if not allowed:
                return False, reason

        if state and self.master_valve_line and not self._ensure_master_open():
            return False, "主阀切换失败，已阻断阀门写入"

        device, line = self._split_target(target)
        if not self.worker.write_digital(device=device, line=line, state=state):
            return False, f"阀门 {channel_id} 写入失败"

        self._states[channel_id] = state
        master_success, master_opened = self._apply_master_valve(state)
        if not master_success:
            self._states[channel_id] = False
            self.worker.write_digital(device=device, line=line, state=False)
            return False, f"阀门 {channel_id} 已写入，但主阀控制失败"
        self._log_event(channel_id, state, target, safety)
        suffix = "（主阀保持开启）" if master_opened else ""
        if safety_close and not state:
            return True, f"阀门 {channel_id} 已安全关闭{suffix}"
        return True, f"阀门 {channel_id} 已切换为 {'打开' if state else '关闭'}{suffix}"

    def _apply_master_valve(self, state: bool) -> tuple[bool, bool]:
        """主阀常开：设备上电即开启，不随通道开关切换。"""
        if not self.master_valve_line:
            return True, False
        if not state:
            return True, bool(self._states.get("master", False))
        opened = self._ensure_master_open()
        return opened, opened

    def _ensure_master_open(self) -> bool:
        """Drive the configured master valve line high once; cache state to avoid churn."""
        if not self.master_valve_line:
            return True
        if self._states.get("master"):
            return True
        device, line = self._split_target(self.master_valve_line)
        success = self.worker.write_digital(device=device, line=line, state=True)
        if success:
            self._states["master"] = True
        return bool(success)

    def _split_target(self, target: str) -> tuple[str | None, str]:
        if "/" in target:
            device, line = target.split("/", 1)
            return device, line
        return None, target

    def _is_hardware_ready(self) -> bool:
        if self.state.hardware_ready:
            return True
        if self.state.telemetry.connected:
            return True
        is_connected = getattr(self.worker, "is_connected", False)
        return bool(is_connected)

    def _build_safety_state(self) -> SafetyState:
        telemetry = self.state.telemetry
        return SafetyState(
            state=telemetry.safety_state,
            airflow=telemetry.airflow,
            threshold=self.state.low_flow_threshold,
            updated_at=telemetry.timestamp,
            reason=telemetry.safety_reason,
        )

    def _log_event(
        self,
        channel_id: int,
        state: bool,
        target: str,
        safety_state: SafetyState,
    ) -> None:
        payload = {
            "ts": time.time(),
            "channel": channel_id,
            "state": "open" if state else "closed",
            "target": target,
            "variant": self.hardware_variant,
            "master_valve": self.master_valve_line,
            "safety_state": safety_state.state,
            "airflow": safety_state.airflow,
        }
        self._logger.info("valve_event | %s", payload)
