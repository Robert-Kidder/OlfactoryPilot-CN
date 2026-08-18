from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


def normalize_digital_target(target: str) -> str:
    """Return one canonical device/port/line identity for config comparisons."""

    raw = str(target).strip().replace("\\", "/")
    if "/" not in raw:
        raise ValueError("数字输出目标必须包含 device/line。")
    device, line = raw.split("/", 1)
    device = device.strip().casefold()
    line = line.strip().casefold()
    if line.startswith("p") and "." in line:
        port, bit = line[1:].split(".", 1)
        if port.isdigit() and bit.isdigit():
            line = f"port{int(port)}/line{int(bit)}"
    if not device or not line:
        raise ValueError("数字输出目标必须包含有效 device/line。")
    return f"{device}/{line}"


class SelectorRoute(StrEnum):
    ODOR = "odor"
    COMPENSATION = "compensation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    """A 路三通 selector；它不是普通气味阀，也没有全关位置。"""

    target: str
    safe_route: SelectorRoute = SelectorRoute.COMPENSATION
    safe_level: bool = False
    odor_level: bool = True

    def __post_init__(self) -> None:
        normalize_digital_target(self.target)
        if self.safe_route != SelectorRoute.COMPENSATION:
            raise ValueError("当前 selector 安全路线必须为补偿出口。")
        if self.safe_level == self.odor_level:
            raise ValueError("selector 两条路线必须使用不同电平。")

    def route_for_level(self, level: bool) -> SelectorRoute:
        if bool(level) == self.safe_level:
            return self.safe_route
        if bool(level) == self.odor_level:
            return SelectorRoute.ODOR
        return SelectorRoute.UNKNOWN


class SafeStopStatus(StrEnum):
    FENCED = "fenced"
    A_ZERO_PENDING = "a_zero_pending"
    A_ZERO_CONFIRMED = "a_zero_confirmed"
    SELECTOR_PENDING = "selector_pending"
    SELECTOR_CONFIRMED = "selector_confirmed"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class SafeStopIdentity:
    operation_id: str
    generation: int
    execution_epoch: int

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("safe stop operation_id 不能为空。")
        if self.generation < 0 or self.execution_epoch < 0:
            raise ValueError("safe stop generation/epoch 必须为非负整数。")


@dataclass(frozen=True, slots=True)
class AZeroReceipt:
    command_id: str
    identity: SafeStopIdentity
    success: bool
    confirmed_a: float
    stale: bool = False
    message: str = ""
    source: str = "safety:safe-stop"
    mode: str = "safe_stop_a_zero"
    lease_token: str | None = None


@dataclass(frozen=True, slots=True)
class SelectorReceipt:
    command_id: str
    identity: SafeStopIdentity
    target: str
    route: SelectorRoute
    success: bool
    stale: bool = False
    message: str = ""


class SafeStopPlan:
    """Pure evidence gate for A-zero-before-selector safe stopping."""

    def __init__(
        self,
        identity: SafeStopIdentity,
        selector: SelectorConfig | None,
    ) -> None:
        self.identity = identity
        self.selector = selector
        self.status = SafeStopStatus.FENCED
        self.recovery_reason = ""
        self._a_zero_command_id: str | None = None
        self._selector_command_id: str | None = None
        self._a_zero_receipt: AZeroReceipt | None = None
        self._selector_receipt: SelectorReceipt | None = None

    @property
    def selector_allowed(self) -> bool:
        return (
            self.selector is not None
            and self.status == SafeStopStatus.A_ZERO_CONFIRMED
        )

    @property
    def a_zero_confirmed(self) -> bool:
        receipt = self._a_zero_receipt
        return bool(
            receipt is not None
            and receipt.success
            and not receipt.stale
            and self._is_confirmed_zero(receipt.confirmed_a)
            and receipt.command_id == self._a_zero_command_id
            and receipt.identity == self.identity
        )

    @property
    def selector_confirmed(self) -> bool:
        receipt = self._selector_receipt
        return bool(
            self.selector is not None
            and receipt is not None
            and receipt.success
            and not receipt.stale
            and receipt.route == self.selector.safe_route
            and receipt.target == self.selector.target
            and receipt.command_id == self._selector_command_id
            and receipt.identity == self.identity
        )

    @property
    def safe_terminal(self) -> bool:
        return self.status == SafeStopStatus.COMPLETED

    def expect_a_zero(self, command_id: str) -> None:
        if self.status != SafeStopStatus.FENCED or not command_id:
            raise RuntimeError("safe stop 当前阶段不能请求 A 清零。")
        self._a_zero_command_id = command_id
        self.status = SafeStopStatus.A_ZERO_PENDING

    def accept_a_zero(self, receipt: AZeroReceipt) -> bool:
        if self._a_zero_receipt is not None:
            self.require_recovery(
                "A 清零 receipt 内容冲突。"
                if receipt != self._a_zero_receipt
                else "A 清零 receipt 重复或迟到。"
            )
            return False
        self._a_zero_receipt = receipt
        if (
            self.status != SafeStopStatus.A_ZERO_PENDING
            or receipt.command_id != self._a_zero_command_id
            or receipt.identity != self.identity
        ):
            self.require_recovery("A 清零 receipt 迟到或身份不匹配。")
            return False
        if receipt.stale or receipt.source != "safety:safe-stop" or receipt.mode != "safe_stop_a_zero":
            self.require_recovery("A 清零 receipt 已失效。")
            return False
        if not receipt.success or not self._is_confirmed_zero(receipt.confirmed_a):
            self.require_recovery(receipt.message or "A 清零未确认。")
            return False
        self.status = SafeStopStatus.A_ZERO_CONFIRMED
        return True

    def expect_selector(self, command_id: str) -> None:
        if not self.selector_allowed or not command_id:
            raise RuntimeError("未确认 A=0，禁止切换 selector。")
        self._selector_command_id = command_id
        self.status = SafeStopStatus.SELECTOR_PENDING

    def accept_selector(self, receipt: SelectorReceipt) -> bool:
        if self.selector is None:
            self.require_recovery("selector 配置不可用，禁止切换安全路线。")
            return False
        if self._selector_receipt is not None:
            self.require_recovery(
                "selector receipt 内容冲突。"
                if receipt != self._selector_receipt
                else "selector receipt 重复或迟到。"
            )
            return False
        self._selector_receipt = receipt
        if (
            self.status != SafeStopStatus.SELECTOR_PENDING
            or receipt.command_id != self._selector_command_id
            or receipt.identity != self.identity
            or receipt.target != self.selector.target
        ):
            self.require_recovery("selector receipt 迟到或身份不匹配。")
            return False
        if receipt.stale:
            self.require_recovery("selector receipt 已失效。")
            return False
        if (
            not receipt.success
            or receipt.route != self.selector.safe_route
            or receipt.route == SelectorRoute.UNKNOWN
        ):
            self.require_recovery(receipt.message or "selector 安全路线未确认。")
            return False
        self.status = SafeStopStatus.SELECTOR_CONFIRMED
        return True

    def complete(self, *, odors_closed: bool, owners_handed_off: bool) -> bool:
        if self.status != SafeStopStatus.SELECTOR_CONFIRMED:
            self.require_recovery("selector 安全路线证据不完整。")
            return False
        if not odors_closed or not owners_handed_off:
            self.require_recovery("气味阀关闭或 owner handoff 未完整确认。")
            return False
        self.status = SafeStopStatus.COMPLETED
        return True

    def timeout(self, stage: str) -> None:
        self.require_recovery(f"{stage} 超时，状态未确认。")

    def require_recovery(self, reason: str) -> None:
        self.status = SafeStopStatus.RECOVERY_REQUIRED
        if not self.recovery_reason:
            self.recovery_reason = str(reason)

    @staticmethod
    def _is_confirmed_zero(value: float) -> bool:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(parsed) and abs(parsed) <= 1e-9
