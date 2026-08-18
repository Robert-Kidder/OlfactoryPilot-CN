from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class ActuationAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class ActuationCategory(StrEnum):
    NORMAL = "normal"
    SAFETY = "safety"
    WARMUP = "warmup"
    MANUAL = "manual"
    PRETEST = "pretest"
    MASTER = "master"
    CLEANING = "cleaning"


class ActuationResult(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    MEASUREMENT_FAULT = "measurement_fault"
    UNCERTAIN = "uncertain"


MEASUREMENT_POINT_DAQMX_WRITE_ACK = "daqmx_write_ack"
MAX_DURATION_NS = (1 << 63) - 1


def duration_ms_to_ns(duration_ms: float) -> int:
    """Convert a finite positive protocol duration without losing sub-ms values."""
    try:
        value = float(duration_ms)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("duration_ms 必须是有限且大于 0 的数值。") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("duration_ms 必须是有限且大于 0 的数值。")
    try:
        duration_ns = int(round(value * 1_000_000))
    except (OverflowError, ValueError) as exc:
        raise ValueError("duration_ms 转换后的纳秒值超出有效范围。") from exc
    if duration_ns <= 0 or duration_ns > MAX_DURATION_NS:
        raise ValueError("duration_ms 转换后的纳秒值超出有效范围。")
    return duration_ns


@dataclass(frozen=True, slots=True)
class ActuationCommand:
    command_id: str
    execution_epoch: int
    arm_epoch: int
    sequence: int
    trial_id: str | None
    trial_index: int | None
    valve: int
    action: ActuationAction
    category: ActuationCategory
    expected_ns: int
    duration_ns: int | None
    wall_timestamp: float
    safety_generation: int
    target_device: str | None = None
    target_line: str | None = None
    operation_id: str | None = None
    generation: int | None = None
    step_id: str | None = None
    action_kind: ActuationAction | None = None

    def __post_init__(self) -> None:
        if not self.command_id:
            raise ValueError("command_id 不能为空。")
        if self.sequence < 0 or self.execution_epoch < 0 or self.arm_epoch < 0:
            raise ValueError("命令 sequence 与 epoch 必须为非负整数。")
        if self.valve < 0:
            raise ValueError("valve 必须为非负整数。")
        if self.expected_ns < 0:
            raise ValueError("expected_ns 必须为非负整数。")
        if self.duration_ns is not None and not (0 < self.duration_ns <= MAX_DURATION_NS):
            raise ValueError("duration_ns 必须位于有效正整数范围。")
        if not math.isfinite(float(self.wall_timestamp)):
            raise ValueError("wall_timestamp 必须为有限值。")
        if self.category == ActuationCategory.CLEANING and (
            not self.operation_id
            or self.generation is None
            or not self.step_id
            or self.action_kind is None
        ):
            raise ValueError("CLEANING 命令必须包含完整 maintenance identity。")
        if self.generation is not None and self.generation < 0:
            raise ValueError("generation 必须为非负整数。")
        if self.action_kind is not None and self.action_kind != self.action:
            raise ValueError("action_kind 必须与 action 一致。")

    @property
    def target(self) -> str | None:
        if self.target_device and self.target_line:
            return f"{self.target_device}/{self.target_line}"
        return self.target_line


@dataclass(frozen=True, slots=True)
class ActuationReceipt:
    command_id: str
    execution_epoch: int
    arm_epoch: int
    sequence: int
    trial_id: str | None
    trial_index: int | None
    valve: int
    action: ActuationAction
    category: ActuationCategory
    expected_ns: int
    started_ns: int | None
    actual_ns: int | None
    wall_timestamp: float
    offset_ms: float | None
    jitter_ms: float | None
    result: ActuationResult
    measurement_point: str
    message: str = ""
    stale: bool = False
    actual_duration_ms: float | None = None
    target_device: str | None = None
    target_line: str | None = None
    operation_id: str | None = None
    generation: int | None = None
    step_id: str | None = None
    action_kind: ActuationAction | None = None
    safety_generation: int = 0

    @classmethod
    def from_write(
        cls,
        *,
        command: ActuationCommand,
        started_ns: int | None,
        actual_ns: int | None,
        wall_timestamp: float,
        result: ActuationResult,
        category: ActuationCategory | None = None,
        message: str = "",
        stale: bool = False,
        actual_duration_ms: float | None = None,
    ) -> ActuationReceipt:
        if result == ActuationResult.SUCCESS:
            if started_ns is None or actual_ns is None:
                raise ValueError("成功回执必须包含 started_ns 与 actual_ns。")
            if not command.expected_ns <= started_ns <= actual_ns:
                raise ValueError("动作时间序列必须满足 expected_ns <= started_ns <= actual_ns。")
            offset_ms = (actual_ns - command.expected_ns) / 1_000_000
            jitter_ms = abs(offset_ms)
        else:
            offset_ms = None if actual_ns is None else (actual_ns - command.expected_ns) / 1_000_000
            jitter_ms = None if offset_ms is None else abs(offset_ms)
        if actual_duration_ms is not None and actual_duration_ms < 0:
            raise ValueError("actual_duration_ms 不得为负数。")
        return cls(
            command_id=command.command_id,
            execution_epoch=command.execution_epoch,
            arm_epoch=command.arm_epoch,
            sequence=command.sequence,
            trial_id=command.trial_id,
            trial_index=command.trial_index,
            valve=command.valve,
            action=command.action,
            category=category or command.category,
            expected_ns=command.expected_ns,
            started_ns=started_ns,
            actual_ns=actual_ns,
            wall_timestamp=float(wall_timestamp),
            offset_ms=offset_ms,
            jitter_ms=jitter_ms,
            result=result,
            measurement_point=MEASUREMENT_POINT_DAQMX_WRITE_ACK,
            message=message,
            stale=stale,
            actual_duration_ms=actual_duration_ms,
            target_device=command.target_device,
            target_line=command.target_line,
            operation_id=command.operation_id,
            generation=command.generation,
            step_id=command.step_id,
            action_kind=command.action_kind,
            safety_generation=command.safety_generation,
        )

    @property
    def target(self) -> str | None:
        if self.target_device and self.target_line:
            return f"{self.target_device}/{self.target_line}"
        return self.target_line


@dataclass(frozen=True, slots=True)
class ActuationStreamSnapshot:
    sample_count: int = 0
    p95_ms: float | None = None
    warning: bool = False
    target_met: bool | None = None


@dataclass(frozen=True, slots=True)
class ActuationQualitySnapshot:
    open: ActuationStreamSnapshot = ActuationStreamSnapshot()
    close: ActuationStreamSnapshot = ActuationStreamSnapshot()
    combined: ActuationStreamSnapshot = ActuationStreamSnapshot()
    last_jitter_ms: float | None = None
    severe_latched: bool = False


@dataclass(frozen=True, slots=True)
class ActuationWarningTransition:
    stream: str
    active: bool
    p95_ms: float


@dataclass(frozen=True, slots=True)
class ActuationMetricsUpdate:
    included: bool
    snapshot: ActuationQualitySnapshot
    warning_transitions: tuple[ActuationWarningTransition, ...] = ()
    severe: bool = False
