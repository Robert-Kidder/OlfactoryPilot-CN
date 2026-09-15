from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.hal import FlowReadbackSnapshot


def _finite_non_negative(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是有限非负数。")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} 必须是有限非负数。")
    return parsed


def _positive(value: Any, label: str) -> float:
    parsed = _finite_non_negative(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return parsed


def _capacity(value: Any, label: str) -> float | None:
    if value is None:
        return None
    return _positive(value, label)


@dataclass(frozen=True, slots=True)
class FlowDeviceCapacities:
    """真实 MFC 容量；未知容量必须显式保留为 ``None``。"""

    a_sccm: float | None = 5000.0
    b_sccm: float | None = 5000.0
    c_sccm: float | None = 5000.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "a_sccm", _capacity(self.a_sccm, "A 设备容量"))
        object.__setattr__(self, "b_sccm", _capacity(self.b_sccm, "B 设备容量"))
        object.__setattr__(self, "c_sccm", _capacity(self.c_sccm, "C 设备容量"))


@dataclass(frozen=True, slots=True)
class CommissioningApprovedMaxima:
    """项目批准上限；它只约束 intent，绝不生成自动动作。"""

    a_sccm: float = 500.0
    b_sccm: float = 0.0
    c_sccm: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "a_sccm", _finite_non_negative(self.a_sccm, "A commissioning 批准上限"))
        object.__setattr__(self, "b_sccm", _finite_non_negative(self.b_sccm, "B commissioning 批准上限"))
        object.__setattr__(self, "c_sccm", _finite_non_negative(self.c_sccm, "C commissioning 批准上限"))


@dataclass(frozen=True, slots=True)
class RealSupplyPolicy:
    """启动时冻结的真实供气 commissioning 门禁。"""

    enabled: bool = False
    capacities: FlowDeviceCapacities = FlowDeviceCapacities()
    approved_maxima: CommissioningApprovedMaxima = CommissioningApprovedMaxima()
    expected_gas: str = "Air"
    requested_setpoint_tolerance_sccm: float = 1e-9
    accepted_readback_tolerance_sccm: float = 1.0
    active_mass_flow_tolerance_sccm: float = 25.0
    zero_mass_flow_tolerance_sccm: float = 5.0
    required_consecutive_samples: int = 6
    observation_window_s: float = 1.0
    maximum_settling_deadline_s: float = 5.0
    maximum_nonzero_hold_duration_s: float = 15.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("real_supply_policy.enabled 必须是 boolean。")
        if not isinstance(self.capacities, FlowDeviceCapacities):
            raise ValueError("real_supply_policy.device_capacities_sccm 无效。")
        if not isinstance(self.approved_maxima, CommissioningApprovedMaxima):
            raise ValueError("real_supply_policy.commissioning_approved_maxima_sccm 无效。")
        if self.expected_gas != "Air":
            raise ValueError("real_supply_policy.expected_gas 必须固定为 Air。")
        for channel, approved, capacity in (
            ("A", self.approved_maxima.a_sccm, self.capacities.a_sccm),
            ("B", self.approved_maxima.b_sccm, self.capacities.b_sccm),
            ("C", self.approved_maxima.c_sccm, self.capacities.c_sccm),
        ):
            if approved > 0 and capacity is None:
                raise ValueError(f"{channel} 设备容量未知时 commissioning 批准上限必须为 0。")
            if capacity is not None and approved > capacity:
                raise ValueError(f"{channel} commissioning 批准上限不得超过设备容量。")
        for name, label in (
            ("requested_setpoint_tolerance_sccm", "requested setpoint tolerance"),
            ("accepted_readback_tolerance_sccm", "accepted/readback tolerance"),
            ("active_mass_flow_tolerance_sccm", "active mass-flow tolerance"),
            ("zero_mass_flow_tolerance_sccm", "zero-channel tolerance"),
        ):
            object.__setattr__(self, name, _finite_non_negative(getattr(self, name), label))
        if type(self.required_consecutive_samples) is not int or self.required_consecutive_samples < 2:
            raise ValueError("连续有效样本数必须是至少 2 的整数。")
        for name, label in (
            ("observation_window_s", "settling observation window"),
            ("maximum_settling_deadline_s", "maximum settling deadline"),
            ("maximum_nonzero_hold_duration_s", "maximum non-zero hold duration"),
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), label))
        if self.observation_window_s >= self.maximum_settling_deadline_s:
            raise ValueError("settling observation window 必须小于 maximum settling deadline。")
        if self.maximum_settling_deadline_s >= self.maximum_nonzero_hold_duration_s:
            raise ValueError("maximum settling deadline 必须小于 maximum non-zero hold duration。")

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> RealSupplyPolicy:
        raw_value = config.get("real_supply_policy")
        if raw_value is None:
            raw: Mapping[str, Any] = {}
        elif not isinstance(raw_value, Mapping):
            raise ValueError("real_supply_policy 必须是对象。")
        else:
            raw = raw_value
        capacities_value = raw.get("device_capacities_sccm")
        if capacities_value is None:
            capacities_raw: Mapping[str, Any] = {}
        elif not isinstance(capacities_value, Mapping):
            raise ValueError("real_supply_policy.device_capacities_sccm 必须是对象。")
        else:
            capacities_raw = capacities_value
        approved_value = raw.get("commissioning_approved_maxima_sccm")
        if approved_value is None:
            approved_raw: Mapping[str, Any] = {
                "A": 500.0,
                "B": 0.0,
                "C": 0.0,
            }
        elif not isinstance(approved_value, Mapping):
            raise ValueError("real_supply_policy.commissioning_approved_maxima_sccm 必须是对象。")
        else:
            approved_raw = approved_value
        return cls(
            enabled=raw.get("enabled", False),
            capacities=FlowDeviceCapacities(
                a_sccm=capacities_raw.get("A", 5000.0),
                b_sccm=capacities_raw.get("B", 5000.0),
                c_sccm=capacities_raw.get("C", 5000.0),
            ),
            approved_maxima=CommissioningApprovedMaxima(
                a_sccm=approved_raw.get("A", 500.0),
                b_sccm=approved_raw.get("B", 0.0),
                c_sccm=approved_raw.get("C", 0.0),
            ),
            expected_gas=raw.get("expected_gas", "Air"),
            requested_setpoint_tolerance_sccm=raw.get(
                "requested_setpoint_tolerance_sccm", 1e-9
            ),
            accepted_readback_tolerance_sccm=raw.get(
                "accepted_readback_tolerance_sccm", 1.0
            ),
            active_mass_flow_tolerance_sccm=raw.get(
                "active_mass_flow_tolerance_sccm", 25.0
            ),
            zero_mass_flow_tolerance_sccm=raw.get(
                "zero_mass_flow_tolerance_sccm", 5.0
            ),
            required_consecutive_samples=raw.get("required_consecutive_samples", 6),
            observation_window_s=raw.get("observation_window_s", 1.0),
            maximum_settling_deadline_s=raw.get(
                "maximum_settling_deadline_s", 5.0
            ),
            maximum_nonzero_hold_duration_s=raw.get(
                "maximum_nonzero_hold_duration_s", 15.0
            ),
        )

    def controller_targets(
        self,
        *,
        sample_a_sccm: float,
        main_b_sccm: float,
        vacuum_c_sccm: float,
    ) -> tuple[float, float, float]:
        requested = {
            "A": _finite_non_negative(sample_a_sccm, "A requested setpoint"),
            "B": _finite_non_negative(main_b_sccm, "B requested setpoint"),
            "C": _finite_non_negative(vacuum_c_sccm, "C requested setpoint"),
        }
        targets = {
            "A": requested["A"] + requested["C"],
            "B": requested["B"],
            "C": requested["C"],
        }
        self.validate_controller_targets(
            (targets["A"], targets["B"], targets["C"])
        )
        return targets["A"], targets["B"], targets["C"]

    def validate_controller_targets(
        self,
        targets_sccm: tuple[float, float, float],
    ) -> None:
        targets = {
            channel: _finite_non_negative(value, f"{channel} controller target")
            for channel, value in zip(("A", "B", "C"), targets_sccm, strict=True)
        }
        if all(target == 0.0 for target in targets.values()):
            raise ValueError("开始供气至少需要一个已批准的非零 controller target。")
        capacities = {
            "A": self.capacities.a_sccm,
            "B": self.capacities.b_sccm,
            "C": self.capacities.c_sccm,
        }
        approved_maxima = {
            "A": self.approved_maxima.a_sccm,
            "B": self.approved_maxima.b_sccm,
            "C": self.approved_maxima.c_sccm,
        }
        for channel in ("B", "C", "A"):
            target = targets[channel]
            capacity = capacities[channel]
            if target != 0.0 and capacity is None:
                raise ValueError(f"{channel} 设备容量未知，只允许 0 sccm。")
            if capacity is not None and target > capacity:
                raise ValueError(
                    f"{channel} controller target {target:g} sccm 超出设备容量 {capacity:g} sccm。"
                )
            if target > approved_maxima[channel]:
                raise ValueError(
                    f"{channel} controller target {target:g} sccm 超出 commissioning 批准上限 "
                    f"{approved_maxima[channel]:g} sccm。"
                )

    def targets_match(
        self,
        expected: tuple[float, float, float],
        actual: tuple[float, float, float],
    ) -> bool:
        tolerance = self.requested_setpoint_tolerance_sccm
        return all(
            math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
            for left, right in zip(expected, actual, strict=True)
        )

    def accepted_readbacks_match(
        self,
        expected: tuple[float, float, float],
        actual: tuple[float, float, float],
    ) -> bool:
        tolerance = self.accepted_readback_tolerance_sccm
        return all(
            math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
            for left, right in zip(expected, actual, strict=True)
        )


class CommissioningMonitorStatus(StrEnum):
    STABLE = "stable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommissioningMonitorResult:
    operation_id: str
    status: CommissioningMonitorStatus
    reason: str
    first_nonzero_tx_monotonic_ns: int
    evaluated_at_monotonic_ns: int
    consecutive_samples: int

    @property
    def stable(self) -> bool:
        return self.status is CommissioningMonitorStatus.STABLE


class FlowSettlingMonitor:
    """FlowWorker-owned, monotonic and retry-free three-channel evaluator."""

    def __init__(
        self,
        *,
        policy: RealSupplyPolicy,
        operation_id: str,
        targets_sccm: tuple[float, float, float],
        first_nonzero_tx_monotonic_ns: int,
    ) -> None:
        if not policy.enabled:
            raise ValueError("Real supply policy 未启用。")
        if not operation_id:
            raise ValueError("commissioning operation_id 不能为空。")
        if type(first_nonzero_tx_monotonic_ns) is not int or first_nonzero_tx_monotonic_ns <= 0:
            raise ValueError("首个非零 TX 单调时戳无效。")
        self.policy = policy
        self.operation_id = operation_id
        self.targets_sccm = tuple(float(value) for value in targets_sccm)
        self.first_nonzero_tx_monotonic_ns = first_nonzero_tx_monotonic_ns
        self._valid_timestamps: list[int] = []
        self._last_snapshot_ns = 0
        self._stable_emitted = False
        self._failed = False

    def evaluate(
        self,
        snapshot: FlowReadbackSnapshot,
        *,
        now_ns: int | None = None,
    ) -> CommissioningMonitorResult | None:
        if self._failed:
            return None
        evaluated_ns = int(snapshot.monotonic_ns if now_ns is None else now_ns)
        elapsed_ns = evaluated_ns - self.first_nonzero_tx_monotonic_ns
        if elapsed_ns >= round(self.policy.maximum_nonzero_hold_duration_s * 1e9):
            self._failed = True
            return self._result(
                CommissioningMonitorStatus.FAILED,
                "maximum non-zero hold duration 已到，必须执行 SafeStop。",
                evaluated_ns,
            )
        if (
            not self._stable_emitted
            and elapsed_ns >= round(self.policy.maximum_settling_deadline_s * 1e9)
        ):
            self._failed = True
            return self._result(
                CommissioningMonitorStatus.FAILED,
                "maximum settling deadline 内未取得连续三路稳定证据。",
                evaluated_ns,
            )
        if (
            not snapshot.fresh
            or snapshot.monotonic_ns <= self._last_snapshot_ns
            or snapshot.monotonic_ns < self.first_nonzero_tx_monotonic_ns
        ):
            self._valid_timestamps.clear()
            return None
        self._last_snapshot_ns = snapshot.monotonic_ns
        readings = snapshot.by_channel
        if set(readings) != {"A", "B", "C"}:
            self._valid_timestamps.clear()
            return None
        accepted_tolerance = self.policy.accepted_readback_tolerance_sccm
        active_tolerance = self.policy.active_mass_flow_tolerance_sccm
        zero_tolerance = self.policy.zero_mass_flow_tolerance_sccm
        zero_threshold = self.policy.requested_setpoint_tolerance_sccm
        valid = True
        for channel, target in zip(("A", "B", "C"), self.targets_sccm, strict=True):
            reading = readings[channel]
            if (
                not reading.fresh
                or reading.monotonic_ns < self.first_nonzero_tx_monotonic_ns
                or reading.gas.strip().casefold()
                != self.policy.expected_gas.casefold()
                or not math.isclose(
                reading.setpoint_sccm,
                target,
                rel_tol=0.0,
                abs_tol=accepted_tolerance,
                )
            ):
                valid = False
                break
            if target > zero_threshold:
                valid = math.isclose(
                    reading.mass_flow_sccm,
                    target,
                    rel_tol=0.0,
                    abs_tol=active_tolerance,
                )
            else:
                valid = abs(reading.mass_flow_sccm) <= zero_tolerance
            if not valid:
                break
        if not valid:
            self._valid_timestamps.clear()
            if self._stable_emitted:
                self._failed = True
                return self._result(
                    CommissioningMonitorStatus.FAILED,
                    "已稳定的三路流量证据越界，必须执行 SafeStop。",
                    evaluated_ns,
                )
            return None
        self._valid_timestamps.append(snapshot.monotonic_ns)
        required = self.policy.required_consecutive_samples
        if len(self._valid_timestamps) > required:
            self._valid_timestamps = self._valid_timestamps[-required:]
        if (
            not self._stable_emitted
            and len(self._valid_timestamps) == required
            and self._valid_timestamps[-1] - self._valid_timestamps[0]
            >= round(self.policy.observation_window_s * 1e9)
        ):
            self._stable_emitted = True
            return self._result(
                CommissioningMonitorStatus.STABLE,
                "连续新鲜 A/B/C setpoint 与实际流量证据满足 commissioning 门槛。",
                evaluated_ns,
            )
        return None

    def fail(self, reason: str, *, now_ns: int) -> CommissioningMonitorResult | None:
        if self._failed:
            return None
        self._failed = True
        return self._result(CommissioningMonitorStatus.FAILED, reason, int(now_ns))

    def _result(
        self,
        status: CommissioningMonitorStatus,
        reason: str,
        evaluated_ns: int,
    ) -> CommissioningMonitorResult:
        return CommissioningMonitorResult(
            operation_id=self.operation_id,
            status=status,
            reason=reason,
            first_nonzero_tx_monotonic_ns=self.first_nonzero_tx_monotonic_ns,
            evaluated_at_monotonic_ns=evaluated_ns,
            consecutive_samples=len(self._valid_timestamps),
        )
