from __future__ import annotations

from collections.abc import Callable

from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    normalize_digital_target,
)
from app.services.hal import HalInterface

TargetResolver = Callable[[int], tuple[str | None, str]]
PhysicalLevelResolver = Callable[[int, bool], bool]


class ActuationDOAdapter:
    """Translate immutable actions to HAL writes without changing HAL timestamps."""

    def __init__(
        self,
        *,
        hal: HalInterface,
        target_resolver: TargetResolver,
        selector_target: str | None = None,
        selector_odor_level: bool = True,
        physical_level_resolver: PhysicalLevelResolver | None = None,
        write_timeout_ms: int = 100,
    ) -> None:
        self.hal = hal
        self.target_resolver = target_resolver
        self.selector_target = selector_target
        self.selector_odor_level = bool(selector_odor_level)
        owner = getattr(target_resolver, "__self__", None)
        inferred_resolver = getattr(owner, "physical_level", None)
        self.physical_level_resolver = physical_level_resolver or (
            inferred_resolver if callable(inferred_resolver) else None
        )
        self.write_timeout_ms = max(1, int(write_timeout_ms))

    def rebind_selector(self, *, target: str, odor_level: bool) -> None:
        normalized = normalize_digital_target(target)
        self.selector_target = normalized
        self.selector_odor_level = bool(odor_level)

    def execute(self, command: ActuationCommand) -> ActuationReceipt:
        selector_safety_route = bool(
            command.category == ActuationCategory.SAFETY
            and command.valve == 0
            and command.step_id == "selector_safe"
            and command.operation_id
            and command.generation is not None
            and command.target_line is not None
            and command.action_kind == command.action
            and self.selector_target
            and normalize_digital_target(command.target or "")
            == normalize_digital_target(self.selector_target)
        )
        configured_selector_target = bool(
            command.valve == 0
            and command.target_line is not None
            and self.selector_target
            and normalize_digital_target(command.target or "")
            == normalize_digital_target(self.selector_target)
        )
        selector_business_route = bool(
            configured_selector_target
            and command.action
            == (
                ActuationAction.OPEN
                if self.selector_odor_level
                else ActuationAction.CLOSE
            )
            and command.operation_id
            and command.generation is not None
            and command.step_id == "selector_odor"
            and command.action_kind == command.action
            and command.category
            in {
                ActuationCategory.MASTER,
                ActuationCategory.CLEANING,
                ActuationCategory.WARMUP,
                ActuationCategory.MANUAL,
                ActuationCategory.PRETEST,
            }
        )
        selector_manual_compensation = bool(
            configured_selector_target
            and command.category == ActuationCategory.MANUAL
            and command.action
            == (
                ActuationAction.CLOSE
                if self.selector_odor_level
                else ActuationAction.OPEN
            )
            and command.operation_id
            and command.generation is not None
            and command.step_id == "selector_compensation"
            and command.action_kind == command.action
        )
        if command.valve == 0 and not (
            selector_safety_route
            or selector_business_route
            or selector_manual_compensation
        ):
            return ActuationReceipt.from_write(
                command=command,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=command.wall_timestamp,
                result=ActuationResult.FAILED,
                message=(
                    "selector 仅允许匹配配置目标的专用安全路线命令，"
                    "或明确选择 odor 路线的业务命令。"
                ),
            )
        if (
            command.category == ActuationCategory.SAFETY
            and command.action == ActuationAction.OPEN
            and not selector_safety_route
        ):
            return ActuationReceipt.from_write(
                command=command,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=command.wall_timestamp,
                result=ActuationResult.FAILED,
                message="安全关闭命令不能用于打开，已拒绝且未写入硬件。",
            )
        try:
            if command.target_line is not None:
                device, line = command.target_device, command.target_line
            else:
                device, line = self.target_resolver(command.valve)
            logical_open = command.action == ActuationAction.OPEN
            physical_level = logical_open
            if command.valve != 0 and self.physical_level_resolver is not None:
                physical_level = bool(
                    self.physical_level_resolver(command.valve, logical_open)
                )
            ack = self.hal.write_digital_ack(
                device=device,
                line=line,
                state=physical_level,
                timeout_ms=self.write_timeout_ms,
            )
        except Exception as exc:
            return ActuationReceipt.from_write(
                command=command,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=command.wall_timestamp,
                result=ActuationResult.FAILED,
                message=f"数字输出准备失败：{exc}",
            )
        result = (
            ActuationResult.SUCCESS
            if ack.success
            else ActuationResult.UNCERTAIN
            if ack.uncertain
            else ActuationResult.FAILED
        )
        try:
            return ActuationReceipt.from_write(
                command=command,
                started_ns=ack.started_ns,
                actual_ns=ack.actual_ns,
                wall_timestamp=ack.wall_timestamp,
                result=result,
                message=ack.message,
            )
        except ValueError as exc:
            return ActuationReceipt.from_write(
                command=command,
                started_ns=ack.started_ns,
                actual_ns=ack.actual_ns,
                wall_timestamp=ack.wall_timestamp,
                result=ActuationResult.MEASUREMENT_FAULT,
                message=f"动作测量时序无效：{exc}",
            )
