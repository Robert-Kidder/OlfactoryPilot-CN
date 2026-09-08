from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .hardware_profile import VerificationStatus


class HardwareVerificationPhase(StrEnum):
    """User-visible lifecycle of one isolated gas-port verification run."""

    IDLE = "idle"
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
            HardwareVerificationPhase.RUNNING,
            HardwareVerificationPhase.AWAITING_CONFIRMATION,
        }
        if active:
            if self.external_port is None or not 1 <= int(self.external_port) <= 20:
                raise ValueError("活动验证必须指定 1–20 的气口编号。")
            if self.started_ns is None or self.deadline_ns is None:
                raise ValueError("活动验证必须包含开始与截止时间。")
            if not self.run_identity or not self.fingerprint:
                raise ValueError("活动验证必须包含运行身份与映射指纹。")
            if int(self.deadline_ns) < int(self.started_ns):
                raise ValueError("活动验证截止时间不得早于开始时间。")
            expected_ns = int(duration * 1_000_000_000)
            if int(self.deadline_ns) - int(self.started_ns) != expected_ns:
                raise ValueError("活动验证时长必须与开始和截止时间一致。")
        if self.awaiting_user_confirmation != (
            self.phase is HardwareVerificationPhase.AWAITING_CONFIRMATION
        ):
            raise ValueError("等待确认标志必须与验证阶段一致。")
        can_stop_expected = self.phase is HardwareVerificationPhase.RUNNING
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

    def permits(
        self,
        *,
        external_port: int,
        revision: int,
        fingerprint: str,
        run_identity: str,
    ) -> bool:
        return bool(
            self.authorized
            and self.action_completed
            and self.safe_closed
            and self.external_port == int(external_port)
            and self.revision == int(revision)
            and self.fingerprint == str(fingerprint).strip().lower()
            and self.run_identity == str(run_identity)
        )
