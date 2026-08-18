from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .actuation import MAX_DURATION_NS, ActuationAction


class CleaningStatus(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"


class CleaningOutcome(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CleaningOperationIdentity:
    operation_id: str
    generation: int

    def __post_init__(self) -> None:
        if not str(self.operation_id).strip():
            raise ValueError("operation_id 不能为空。")
        if int(self.generation) < 0:
            raise ValueError("generation 必须为非负整数。")


def normalize_cleaning_target(target: str) -> str:
    raw = str(target).strip().replace("\\", "/")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if len(parts) < 2:
        raise ValueError(f"清洗目标格式无效：{target!r}")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class CleaningStep:
    operation_id: str
    generation: int
    step_id: str
    command_id: str
    target: str
    action_kind: ActuationAction
    channel: int
    external_label: str
    cycle_index: int
    duration_ns: int | None = None

    def __post_init__(self) -> None:
        CleaningOperationIdentity(self.operation_id, self.generation)
        if not str(self.step_id).strip():
            raise ValueError("step_id 不能为空。")
        if not str(self.command_id).strip():
            raise ValueError("command_id 不能为空。")
        if int(self.channel) <= 0:
            raise ValueError("清洗通道必须为正整数。")
        if int(self.cycle_index) <= 0:
            raise ValueError("cycle_index 必须为正整数。")
        object.__setattr__(self, "target", normalize_cleaning_target(self.target))
        object.__setattr__(self, "action_kind", ActuationAction(self.action_kind))
        if self.action_kind == ActuationAction.OPEN:
            if self.duration_ns is None or not (0 < self.duration_ns <= MAX_DURATION_NS):
                raise ValueError("清洗 open 步骤必须包含有效 duration_ns。")
        elif self.duration_ns is not None:
            raise ValueError("清洗 close 步骤不得携带 duration_ns。")


@dataclass(frozen=True, slots=True)
class CleaningPlan:
    identity: CleaningOperationIdentity
    gas_label: str
    flow_setpoints_sccm: tuple[tuple[str, float], ...]
    open_duration_ns: int
    cycles: int
    selected_channels: tuple[int, ...]
    external_labels: tuple[tuple[int, str], ...]
    steps: tuple[CleaningStep, ...]
    parallel_open_limit: int = 1

    def __post_init__(self) -> None:
        if self.parallel_open_limit != 1:
            raise ValueError("parallel_open_limit 必须等于 1。")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("清洗步骤 ID 必须唯一。")
        if len({step.command_id for step in self.steps}) != len(self.steps):
            raise ValueError("清洗 command_id 必须唯一。")


@dataclass(frozen=True, slots=True)
class CleaningConfigSnapshot:
    enabled: bool
    gas_label: str
    flow_channel: str
    flow_sccm: float
    max_approved_flow_sccm: float
    fixed_flow_setpoints_sccm: tuple[tuple[str, float], ...]
    open_duration_s: float
    max_open_duration_s: float
    cycles: int
    max_cycles: int
    parallel_open_limit: int
    selected_channels: tuple[int, ...]
    available_targets: tuple[tuple[int, str], ...]
    external_labels: tuple[tuple[int, str], ...]

    @classmethod
    def from_effective_config(
        cls,
        config: Mapping[str, Any],
        *,
        available_channels: Mapping[int, str],
    ) -> CleaningConfigSnapshot:
        raw = config.get("cleaning")
        if not isinstance(raw, Mapping):
            raise ValueError("缺少 cleaning 配置。")
        available = tuple(
            (int(channel), normalize_cleaning_target(target))
            for channel, target in sorted(available_channels.items())
        )
        available_by_channel = dict(available)
        selected_raw = raw.get("selected_channels", raw.get("default_channels", ()))
        try:
            selected = tuple(int(channel) for channel in selected_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("清洗通道必须是整数列表。") from exc
        if not selected:
            raise ValueError("清洗至少选择一路已配置通道。")
        if len(set(selected)) != len(selected):
            raise ValueError("清洗通道不得重复。")
        missing = [channel for channel in selected if channel not in available_by_channel]
        if missing:
            raise ValueError(f"清洗通道未配置映射：{missing}")
        selected_targets = [available_by_channel[channel] for channel in selected]
        if len(set(selected_targets)) != len(selected_targets):
            raise ValueError("所选清洗通道的硬件目标不得重复。")

        flow = _finite_number(raw.get("flow_sccm", raw.get("default_flow_sccm")), "清洗气流")
        max_flow = _finite_number(raw.get("max_approved_flow_sccm"), "批准流量上限")
        if flow <= 0 or max_flow <= 0 or flow > max_flow:
            raise ValueError("清洗气流必须位于批准范围内。")
        duration = _finite_number(
            raw.get("open_duration_s", raw.get("default_open_duration_s")),
            "每路时间",
        )
        max_duration = _finite_number(raw.get("max_open_duration_s", 120), "每路时间上限")
        if duration <= 0 or max_duration <= 0 or duration > max_duration:
            raise ValueError("清洗每路时间必须位于批准范围内。")
        cycles = _positive_int(raw.get("cycles", raw.get("default_cycles")), "循环轮数")
        max_cycles = _positive_int(raw.get("max_cycles", 20), "循环轮数上限")
        if cycles > max_cycles:
            raise ValueError("清洗循环轮数必须位于批准范围内。")
        parallel_open_limit = _positive_int(
            raw.get("parallel_open_limit", 1),
            "parallel_open_limit",
        )
        if parallel_open_limit != 1:
            raise ValueError("parallel_open_limit 当前必须等于 1。")

        flow_channel = str(raw.get("flow_channel", "A")).strip().upper()
        if flow_channel != "A":
            raise ValueError("当前清洗 flow_channel 只批准 A。")
        fixed_raw = raw.get("fixed_flow_setpoints_sccm", {"B": 0, "C": 0})
        if not isinstance(fixed_raw, Mapping):
            raise ValueError("fixed_flow_setpoints_sccm 必须是对象。")
        fixed = tuple(
            (channel, _finite_number(fixed_raw.get(channel, 0), f"{channel} 流量"))
            for channel in ("B", "C")
        )
        if any(value != 0 for _, value in fixed):
            raise ValueError("清洗 B/C 流量必须固定为 0。")

        labels_raw = raw.get("external_labels", {})
        labels_mapping = labels_raw if isinstance(labels_raw, Mapping) else {}
        labels = tuple(
            (channel, str(labels_mapping.get(str(channel), labels_mapping.get(channel, channel))))
            for channel, _target in available
        )
        return cls(
            enabled=bool(raw.get("enabled", True)),
            gas_label=str(raw.get("gas_label", "Air")).strip() or "Air",
            flow_channel=flow_channel,
            flow_sccm=flow,
            max_approved_flow_sccm=max_flow,
            fixed_flow_setpoints_sccm=fixed,
            open_duration_s=duration,
            max_open_duration_s=max_duration,
            cycles=cycles,
            max_cycles=max_cycles,
            parallel_open_limit=parallel_open_limit,
            selected_channels=selected,
            available_targets=available,
            external_labels=labels,
        )

    def external_label_for(self, channel: int) -> str:
        return dict(self.external_labels).get(int(channel), str(int(channel)))

    def build_plan(self, identity: CleaningOperationIdentity) -> CleaningPlan:
        targets = dict(self.available_targets)
        duration_ns = int(round(self.open_duration_s * 1_000_000_000))
        if not (0 < duration_ns <= MAX_DURATION_NS):
            raise ValueError("清洗每路时间转换后的纳秒值超出有效范围。")
        steps: list[CleaningStep] = []
        ordinal = 0
        for cycle in range(1, self.cycles + 1):
            for channel in self.selected_channels:
                for action in (ActuationAction.OPEN, ActuationAction.CLOSE):
                    ordinal += 1
                    suffix = action.value
                    step_id = f"cleaning-{ordinal:04d}-{suffix}"
                    steps.append(
                        CleaningStep(
                            operation_id=identity.operation_id,
                            generation=identity.generation,
                            step_id=step_id,
                            command_id=f"{identity.operation_id}:{identity.generation}:{ordinal:04d}",
                            target=targets[channel],
                            action_kind=action,
                            channel=channel,
                            external_label=self.external_label_for(channel),
                            cycle_index=cycle,
                            duration_ns=duration_ns if action == ActuationAction.OPEN else None,
                        )
                    )
        flow_values = ((self.flow_channel, self.flow_sccm),) + self.fixed_flow_setpoints_sccm
        return CleaningPlan(
            identity=identity,
            gas_label=self.gas_label,
            flow_setpoints_sccm=flow_values,
            open_duration_ns=duration_ns,
            cycles=self.cycles,
            selected_channels=self.selected_channels,
            external_labels=self.external_labels,
            steps=tuple(steps),
            parallel_open_limit=self.parallel_open_limit,
        )


@dataclass(frozen=True, slots=True)
class CleaningSnapshot:
    status: CleaningStatus = CleaningStatus.IDLE
    identity: CleaningOperationIdentity | None = None
    current_step_id: str | None = None
    current_channel: int | None = None
    remaining_ns: int = 0
    lease_held: bool = False
    recording_ready: bool = False
    close_confirmed: int = 0
    close_required: int = 0
    flow_zero_confirmed: bool = False
    selector_safe_confirmed: bool = False
    bundle_path: str | None = None
    possibly_open: tuple[str, ...] = ()
    recovery_reason: str = ""


@dataclass(frozen=True, slots=True)
class CleaningViewSnapshot:
    status: CleaningStatus = CleaningStatus.IDLE
    status_text: str = "清洗空闲"
    available_channels: tuple[int, ...] = ()
    selected_channels: tuple[int, ...] = ()
    external_labels: tuple[tuple[int, str], ...] = ()
    gas_label: str = "Air"
    flow_sccm: float = 1500.0
    max_flow_sccm: float = 1500.0
    open_duration_s: float = 10.0
    max_open_duration_s: float = 120.0
    cycles: int = 3
    max_cycles: int = 20
    estimated_duration_s: float = 0.0
    dirty: bool = False
    controls_enabled: bool = True
    can_save: bool = False
    can_start: bool = False
    can_stop: bool = False
    can_recover: bool = False
    current_step_id: str = ""
    current_channel: int | None = None
    remaining_s: float = 0.0
    lease_text: str = "idle"
    recording_ready: bool = False
    close_progress_text: str = "0/0"
    bundle_path: str = ""
    output_root: str = ""
    recovery_reason: str = ""


@dataclass(frozen=True, slots=True)
class CleaningResult:
    identity: CleaningOperationIdentity
    status: CleaningStatus
    outcome: CleaningOutcome
    reason: str = ""

    @property
    def safe_terminal(self) -> bool:
        return self.status == CleaningStatus.COMPLETED and self.outcome in {
            CleaningOutcome.COMPLETED,
            CleaningOutcome.ABORTED,
        }


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数值。") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值。")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正整数。")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是正整数。") from exc
    if number <= 0 or number != float(value):
        raise ValueError(f"{label}必须是正整数。")
    return number
