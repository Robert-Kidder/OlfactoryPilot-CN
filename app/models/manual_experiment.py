from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .actuation import MAX_DURATION_NS
from .hardware_profile import ChannelRegistry, FlowSetpoints
from .safe_stop import SelectorConfig, normalize_digital_target


class ManualExperimentStatus(StrEnum):
    IDLE = "idle"
    FLOW_PENDING = "flow_pending"
    SELECTOR_PENDING = "selector_pending"
    OPENING = "opening"
    STIMULATING = "stimulating"
    CLOSING = "closing"
    ZEROING_A = "zeroing_a"
    SELECTOR_COMPENSATION = "selector_compensation"
    RESTORING_SUPPLY = "restoring_supply"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


class ManualExperimentOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class ManualExperimentIntent:
    external_ports: tuple[int, ...]
    total_sccm: float
    sample_a_sccm: float
    vacuum_c_sccm: float
    duration_ns: int

    def __post_init__(self) -> None:
        ports = tuple(self.external_ports)
        if not ports or len(set(ports)) != len(ports):
            raise ValueError("请选择至少一个且不重复的机外气口。")
        if any(type(port) is not int or port not in range(1, 21) for port in ports):
            raise ValueError("机外气口必须位于 1–20。")
        object.__setattr__(self, "external_ports", ports)
        FlowSetpoints(
            total_sccm=self.total_sccm,
            sample_a_sccm=self.sample_a_sccm,
            vacuum_c_sccm=self.vacuum_c_sccm,
        )
        if type(self.duration_ns) is not int or not 0 < self.duration_ns <= MAX_DURATION_NS:
            raise ValueError("刺激时长必须是有效正整数纳秒。")


@dataclass(frozen=True, slots=True)
class ManualSupplyIntent:
    enabled: bool
    total_sccm: float
    sample_a_sccm: float
    vacuum_c_sccm: float

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("供气 intent.enabled 必须是 boolean。")
        FlowSetpoints(
            total_sccm=self.total_sccm,
            sample_a_sccm=self.sample_a_sccm,
            vacuum_c_sccm=self.vacuum_c_sccm,
        )


@dataclass(frozen=True, slots=True)
class ManualExperimentIdentity:
    operation_id: str
    generation: int
    execution_epoch: int

    def __post_init__(self) -> None:
        if not str(self.operation_id).strip():
            raise ValueError("manual operation_id 不能为空。")
        for value, label in (
            (self.generation, "generation"),
            (self.execution_epoch, "execution_epoch"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"manual {label} 必须是非负整数。")


@dataclass(frozen=True, slots=True)
class ManualValveTarget:
    external_port: int
    internal_valve: int
    target: str
    active_high: bool

    def __post_init__(self) -> None:
        if type(self.external_port) is not int or not 1 <= self.external_port <= 20:
            raise ValueError("manual 机外气口必须位于 1–20。")
        if type(self.internal_valve) is not int or not 1 <= self.internal_valve <= 20:
            raise ValueError("manual 内部阀位必须位于 1–20。")
        normalize_digital_target(self.target)
        if type(self.active_high) is not bool:
            raise ValueError("manual active_high 必须是 boolean。")


@dataclass(frozen=True, slots=True)
class ManualExperimentPlan:
    identity: ManualExperimentIdentity
    flow_setpoints: FlowSetpoints
    selector: SelectorConfig
    targets: tuple[ManualValveTarget, ...]
    duration_ns: int
    supply_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ManualExperimentIdentity):
            raise ValueError("manual identity 类型无效。")
        if not isinstance(self.flow_setpoints, FlowSetpoints):
            raise ValueError("manual flow_setpoints 类型无效。")
        if not isinstance(self.selector, SelectorConfig):
            raise ValueError("manual selector 类型无效。")
        if type(self.supply_only) is not bool:
            raise ValueError("manual supply_only 必须是 boolean。")
        if not self.targets and not self.supply_only:
            raise ValueError("manual 刺激至少选择一个可用机外气口。")
        if self.targets and self.supply_only:
            raise ValueError("manual supply-only 计划不得包含气味阀目标。")
        if type(self.duration_ns) is not int or not 0 < self.duration_ns <= MAX_DURATION_NS:
            raise ValueError("manual duration_ns 必须是有效正整数。")
        ports = [target.external_port for target in self.targets]
        valves = [target.internal_valve for target in self.targets]
        physical = [normalize_digital_target(target.target) for target in self.targets]
        if len(set(ports)) != len(ports):
            raise ValueError("manual 机外气口不得重复。")
        if len(set(valves)) != len(valves):
            raise ValueError("manual 内部阀位不得重复。")
        if len(set(physical)) != len(physical):
            raise ValueError("manual NI target 不得重复。")
        selector_identity = normalize_digital_target(self.selector.target)
        if selector_identity in physical:
            raise ValueError("manual selector 不得作为普通气味阀。")

    @classmethod
    def from_registry(
        cls,
        *,
        identity: ManualExperimentIdentity,
        flow_setpoints: FlowSetpoints,
        selector: SelectorConfig,
        registry: ChannelRegistry,
        external_ports: tuple[int, ...] | list[int],
        duration_ns: int,
        allow_mock: bool = False,
    ) -> ManualExperimentPlan:
        ports = tuple(external_ports)
        if len(set(ports)) != len(ports):
            raise ValueError("manual 机外气口不得重复。")
        targets: list[ManualValveTarget] = []
        for port in ports:
            descriptor = registry.by_external_port(port)
            if not descriptor.enabled or not descriptor.verification_valid_for(
                allow_mock=allow_mock
            ):
                raise ValueError(f"机外气口 {port} 未启用或验证证据不适用于当前模式。")
            assert descriptor.internal_valve is not None
            targets.append(
                ManualValveTarget(
                    external_port=descriptor.external_port,
                    internal_valve=descriptor.internal_valve,
                    target=descriptor.target,
                    active_high=descriptor.active_high,
                )
            )
        return cls(
            identity=identity,
            flow_setpoints=flow_setpoints,
            selector=selector,
            targets=tuple(targets),
            duration_ns=duration_ns,
        )

    @classmethod
    def for_supply(
        cls,
        *,
        identity: ManualExperimentIdentity,
        flow_setpoints: FlowSetpoints,
        selector: SelectorConfig,
    ) -> ManualExperimentPlan:
        return cls(
            identity=identity,
            flow_setpoints=flow_setpoints,
            selector=selector,
            targets=(),
            duration_ns=1,
            supply_only=True,
        )


@dataclass(frozen=True, slots=True)
class ManualExperimentSnapshot:
    status: ManualExperimentStatus = ManualExperimentStatus.IDLE
    identity: ManualExperimentIdentity | None = None
    selected_external_ports: tuple[int, ...] = ()
    flow_confirmed: bool = False
    selector_odor_confirmed: bool = False
    open_confirmed: tuple[int, ...] = ()
    close_confirmed: tuple[int, ...] = ()
    flow_zero_confirmed: bool = False
    selector_compensation_confirmed: bool = False
    supply_restored: bool = False
    supply_restored_at: float | None = None
    # Receipt-owned supply evidence: None means that the current physical
    # state cannot be proven.  A command being queued never changes this
    # value; only its correlated successful receipt may do so.
    supply_enabled: bool | None = None
    supply_transitioning: bool = False
    ready_ns: int | None = None
    deadline_ns: int | None = None
    remaining_ns: int = 0
    possibly_open: tuple[int, ...] = ()
    recovery_reason: str = ""


@dataclass(frozen=True, slots=True)
class ManualPresentationSnapshot:
    """GUI 一帧消费的手动实验页面一致性快照。"""

    generation: int
    connected: bool
    hardware_ready: bool
    safety_state: str
    safety_reason: str
    airflow: float
    telemetry_timestamp: float
    experiment: ManualExperimentSnapshot
    controls_enabled: bool
    can_apply_flow: bool
    can_release: bool
    can_stop: bool
    detail_text: str = ""
    expected_flow_transition: bool = False

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("presentation generation 必须是非负整数。")
        if not isinstance(self.experiment, ManualExperimentSnapshot):
            raise ValueError("presentation 必须包含有效 manual snapshot。")
        if isinstance(self.airflow, bool) or not math.isfinite(float(self.airflow)):
            raise ValueError("presentation airflow 必须是有限数值。")
        if isinstance(self.telemetry_timestamp, bool) or not math.isfinite(
            float(self.telemetry_timestamp)
        ):
            raise ValueError("presentation telemetry timestamp 必须是有限数值。")


@dataclass(frozen=True, slots=True)
class ManualExperimentResult:
    identity: ManualExperimentIdentity
    status: ManualExperimentStatus
    outcome: ManualExperimentOutcome
    reason: str = ""

    @property
    def completed(self) -> bool:
        return (
            self.status is ManualExperimentStatus.COMPLETED
            and self.outcome is ManualExperimentOutcome.COMPLETED
        )
