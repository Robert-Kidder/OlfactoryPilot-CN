from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .hardware_profile import VerificationStatus
from .safe_stop import SelectorConfig, normalize_digital_target


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    """现场单口验证的持久化参数与现场证据上限。"""

    flow_sccm: float = 1500.0
    duration_s: float = 20.0
    max_approved_flow_sccm: float = 1500.0

    def __post_init__(self) -> None:
        values = {
            "验证流量": self.flow_sccm,
            "最长验证时间": self.duration_s,
            "现场批准流量上限": self.max_approved_flow_sccm,
        }
        parsed: dict[str, float] = {}
        for label, raw in values.items():
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{label}必须是有限数值。") from exc
            if not math.isfinite(value):
                raise ValueError(f"{label}必须是有限数值。")
            parsed[label] = value
        if not 1.0 <= parsed["最长验证时间"] <= 60.0:
            raise ValueError("最长验证时间必须位于 1–60 秒。")
        if parsed["验证流量"] <= 0:
            raise ValueError("验证流量必须大于 0 ml/min。")
        if parsed["现场批准流量上限"] <= 0:
            raise ValueError("现场批准流量上限必须大于 0 ml/min。")
        if parsed["验证流量"] > parsed["现场批准流量上限"]:
            raise ValueError("验证流量超出当前现场证据支持范围。")
        object.__setattr__(self, "flow_sccm", parsed["验证流量"])
        object.__setattr__(self, "duration_s", parsed["最长验证时间"])
        object.__setattr__(
            self,
            "max_approved_flow_sccm",
            parsed["现场批准流量上限"],
        )

    def validate_for_max_sample(self, max_sample_a_sccm: float) -> None:
        maximum = float(max_sample_a_sccm)
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("当前 HardwareProfile 的 A 流量上限无效。")
        if self.flow_sccm > maximum:
            raise ValueError("验证流量超出当前 MFC A 量程，请修改后重试。")

    def to_dict(self) -> dict[str, float]:
        return {
            "flow_sccm": self.flow_sccm,
            "duration_s": self.duration_s,
            "max_approved_flow_sccm": self.max_approved_flow_sccm,
        }

    @classmethod
    def from_value(cls, raw) -> VerificationConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            from collections.abc import Mapping

            if not isinstance(raw, Mapping):
                raise ValueError("hardware_profile.verification_config 必须是对象。")
        allowed = {"flow_sccm", "duration_s", "max_approved_flow_sccm"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                "hardware_profile.verification_config 包含未知字段："
                + "、".join(sorted(str(item) for item in unknown))
            )
        return cls(
            flow_sccm=raw.get("flow_sccm", 1500.0),
            duration_s=raw.get("duration_s", 20.0),
            max_approved_flow_sccm=raw.get("max_approved_flow_sccm", 1500.0),
        )


class HardwareVerificationPhase(StrEnum):
    """User-visible lifecycle of one isolated gas-port verification run."""

    IDLE = "idle"
    PREPARING = "preparing"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class HardwareVerificationSnapshot:
    phase: HardwareVerificationPhase = HardwareVerificationPhase.IDLE
    external_port: int | None = None
    started_ns: int | None = None
    deadline_ns: int | None = None
    duration_s: float = 0.0
    can_stop: bool = False
    awaiting_user_confirmation: bool = False
    result: VerificationStatus | None = None
    run_identity: str = ""
    revision: int = 0
    fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", HardwareVerificationPhase(self.phase))
        if self.result is not None:
            object.__setattr__(self, "result", VerificationStatus(self.result))
        duration = float(self.duration_s)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("验证时长必须是非负有限数值。")
        object.__setattr__(self, "duration_s", duration)
        active = self.phase in {
            HardwareVerificationPhase.PREPARING,
            HardwareVerificationPhase.RUNNING,
            HardwareVerificationPhase.AWAITING_CONFIRMATION,
        }
        if active:
            if self.external_port is None or not 1 <= int(self.external_port) <= 20:
                raise ValueError("活动验证必须指定 1–20 的气口编号。")
            if self.phase is HardwareVerificationPhase.PREPARING:
                if self.started_ns is not None or self.deadline_ns is not None:
                    raise ValueError("准备中的验证不能提前声明动作计时。")
            elif self.started_ns is None or self.deadline_ns is None:
                raise ValueError("运行或等待确认的验证必须包含开始与截止时间。")
            if not self.run_identity or not self.fingerprint:
                raise ValueError("活动验证必须包含运行身份与映射指纹。")
            if (
                self.started_ns is not None
                and self.deadline_ns is not None
                and int(self.deadline_ns) < int(self.started_ns)
            ):
                raise ValueError("活动验证截止时间不得早于开始时间。")
            expected_ns = int(duration * 1_000_000_000)
            if (
                self.started_ns is not None
                and self.deadline_ns is not None
                and int(self.deadline_ns) - int(self.started_ns) != expected_ns
            ):
                raise ValueError("活动验证时长必须与开始和截止时间一致。")
        if self.awaiting_user_confirmation != (
            self.phase is HardwareVerificationPhase.AWAITING_CONFIRMATION
        ):
            raise ValueError("等待确认标志必须与验证阶段一致。")
        can_stop_expected = self.phase in {
            HardwareVerificationPhase.PREPARING,
            HardwareVerificationPhase.RUNNING,
        }
        if self.can_stop != can_stop_expected:
            raise ValueError("仅运行中的验证可以立即停止。")
        finished = self.phase is HardwareVerificationPhase.FINISHED
        if finished != (self.result is not None):
            raise ValueError("完成阶段必须且只能携带验证结果。")
        if not active and not finished and self.result is not None:
            raise ValueError("未完成的验证不能携带结果。")

    @property
    def active(self) -> bool:
        return self.phase in {
            HardwareVerificationPhase.PREPARING,
            HardwareVerificationPhase.RUNNING,
            HardwareVerificationPhase.AWAITING_CONFIRMATION,
        }

    def remaining_seconds(self, now_ns: int) -> int:
        if not self.active or self.deadline_ns is None:
            return 0
        return max(0, math.ceil((self.deadline_ns - int(now_ns)) / 1_000_000_000))


@dataclass(frozen=True, slots=True)
class PhysicalVerificationContract:
    """Capability produced only by an authorized physical verification owner."""

    run_identity: str
    external_port: int
    revision: int
    fingerprint: str
    action_completed: bool
    safe_closed: bool
    authorized: bool
    ni_target: str = ""
    flow_setpoint_sccm: float = 0.0
    flow_readback_sccm: float = 0.0
    open_command_id: str = ""
    close_command_id: str = ""
    opened_at_ns: int | None = None
    closed_at_ns: int | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.action_completed, self.safe_closed, self.authorized)
        ):
            raise ValueError("现场验证完成契约的状态字段必须是布尔值。")
        if not self.run_identity.strip():
            raise ValueError("现场验证完成契约缺少运行身份。")
        if not 1 <= int(self.external_port) <= 20:
            raise ValueError("现场验证完成契约的气口必须位于 1–20。")
        if int(self.revision) < 0:
            raise ValueError("现场验证完成契约的 revision 无效。")
        fingerprint = str(self.fingerprint).strip().lower()
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("现场验证完成契约的映射指纹无效。")
        object.__setattr__(self, "fingerprint", fingerprint)
        if self.ni_target:
            normalize_digital_target(self.ni_target)
        for label, raw in (
            ("验证流量", self.flow_setpoint_sccm),
            ("验证流量回读", self.flow_readback_sccm),
        ):
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label}必须是非负有限数值。")
            object.__setattr__(
                self,
                (
                    "flow_setpoint_sccm"
                    if label == "验证流量"
                    else "flow_readback_sccm"
                ),
                value,
            )
        if (
            self.open_command_id
            and self.close_command_id
            and self.open_command_id == self.close_command_id
        ):
            raise ValueError("现场验证 open/close command ID 必须不同。")
        if (
            self.opened_at_ns is not None
            and self.closed_at_ns is not None
            and int(self.closed_at_ns) < int(self.opened_at_ns)
        ):
            raise ValueError("现场验证 close receipt 不得早于 open receipt。")

    def permits(
        self,
        *,
        external_port: int,
        revision: int,
        fingerprint: str,
        run_identity: str,
        ni_target: str,
    ) -> bool:
        return bool(
            self.authorized
            and self.action_completed
            and self.safe_closed
            and self.external_port == int(external_port)
            and self.revision == int(revision)
            and self.fingerprint == str(fingerprint).strip().lower()
            and self.run_identity == str(run_identity)
            and bool(self.ni_target)
            and normalize_digital_target(self.ni_target)
            == normalize_digital_target(ni_target)
            and self.flow_setpoint_sccm > 0
            and self.flow_readback_sccm > 0
            and bool(self.open_command_id)
            and bool(self.close_command_id)
            and self.open_command_id != self.close_command_id
            and self.opened_at_ns is not None
            and self.closed_at_ns is not None
        )


@dataclass(frozen=True, slots=True)
class PhysicalVerificationPlan:
    run_identity: str
    external_port: int
    internal_valve: int
    target: str
    active_high: bool
    revision: int
    fingerprint: str
    duration_s: float
    flow_sccm: float
    max_sample_a_sccm: float
    selector: SelectorConfig
    close_targets: tuple[tuple[int, str, bool], ...]

    def __post_init__(self) -> None:
        if not self.run_identity.strip():
            raise ValueError("现场验证 plan 缺少运行身份。")
        if not 1 <= int(self.external_port) <= 20:
            raise ValueError("现场验证气口必须位于 1–20。")
        if not 1 <= int(self.internal_valve) <= 20:
            raise ValueError("现场验证控制通道必须位于 1–20。")
        target = normalize_digital_target(self.target)
        if int(self.revision) < 0:
            raise ValueError("现场验证 revision 无效。")
        fingerprint = str(self.fingerprint).strip().lower()
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("现场验证 mapping fingerprint 无效。")
        config = VerificationConfig(
            flow_sccm=self.flow_sccm,
            duration_s=self.duration_s,
            max_approved_flow_sccm=self.max_sample_a_sccm,
        )
        config.validate_for_max_sample(self.max_sample_a_sccm)
        identities: set[str] = set()
        valves: set[int] = set()
        target_found = False
        for valve, close_target, physical_level in self.close_targets:
            if not 1 <= int(valve) <= 20 or type(physical_level) is not bool:
                raise ValueError("现场验证全关目标无效。")
            valve = int(valve)
            if valve in valves:
                raise ValueError("现场验证全关阀身份不得重复。")
            valves.add(valve)
            normalized = normalize_digital_target(close_target)
            if normalized in identities:
                raise ValueError("现场验证全关目标不得重复。")
            identities.add(normalized)
            target_found = target_found or (
                valve == int(self.internal_valve)
                and normalized == target
                and physical_level is (not self.active_high)
            )
        if len(self.close_targets) != 20:
            raise ValueError("现场验证启动必须包含气味阀 1–20 的完整关闭目标。")
        if valves != set(range(1, 21)):
            raise ValueError("现场验证全关阀身份必须恰好覆盖 1–20。")
        if not target_found:
            raise ValueError("现场验证目标的阀身份、线路或关闭电平不匹配。")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "duration_s", config.duration_s)
        object.__setattr__(self, "flow_sccm", config.flow_sccm)


class PhysicalVerificationOutcome(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    TIMED_OUT = "timed_out"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class PhysicalVerificationWorkerSnapshot:
    plan: PhysicalVerificationPlan
    phase: HardwareVerificationPhase
    started_ns: int | None = None
    deadline_ns: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class PhysicalVerificationResult:
    plan: PhysicalVerificationPlan
    outcome: PhysicalVerificationOutcome
    contract: PhysicalVerificationContract | None = None
    reason: str = ""
    lease_releasable: bool = False
