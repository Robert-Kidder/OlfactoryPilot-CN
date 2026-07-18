from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)


class TtlInputError(RuntimeError):
    """TTL 采样链路返回了不能安全解释的输入。"""


@dataclass(frozen=True)
class TtlPulse:
    timestamp: float
    arm_epoch: int
    sequence: int


@dataclass(frozen=True)
class TtlTriggerConfig:
    high_threshold_v: float = 2.0
    low_threshold_v: float = 0.8
    debounce_ms: float = 2.0
    poll_hz: int = 1000

    @classmethod
    def from_mapping(cls, config: dict[str, Any] | None) -> TtlTriggerConfig:
        values = config or {}
        try:
            high = float(values.get("ttl_high_threshold_v", cls.high_threshold_v))
            low = float(values.get("ttl_low_threshold_v", cls.low_threshold_v))
            debounce = float(values.get("ttl_debounce_ms", cls.debounce_ms))
            poll = int(float(values.get("ttl_poll_hz", cls.poll_hz)))
        except (TypeError, ValueError, OverflowError):
            LOG.warning("TTL 配置无效，已使用安全默认值。")
            return cls()
        if (
            not math.isfinite(high)
            or not math.isfinite(low)
            or not math.isfinite(debounce)
            or high <= low
            or debounce < 0
            or poll <= 0
        ):
            LOG.warning("TTL 配置无效，已使用安全默认值。")
            return cls()
        return cls(
            high_threshold_v=high,
            low_threshold_v=low,
            debounce_ms=debounce,
            poll_hz=poll,
        )


class TtlTriggerService:
    """与 UI 无关的 TTL 迟滞、去抖和上升沿检测器。"""

    def __init__(self, config: TtlTriggerConfig | dict[str, Any] | None = None) -> None:
        self.config = config if isinstance(config, TtlTriggerConfig) else TtlTriggerConfig.from_mapping(config)
        self._arm_epoch: int | None = None
        self._sequence = 0
        self._stable_high = False
        self._seen_low = False
        self._candidate_high: bool | None = None
        self._candidate_since: float | None = None

    @property
    def is_armed(self) -> bool:
        return self._arm_epoch is not None

    @property
    def arm_epoch(self) -> int | None:
        return self._arm_epoch

    def arm(self, *, arm_epoch: int) -> None:
        self._arm_epoch = int(arm_epoch)

    def disarm(self) -> None:
        self._arm_epoch = None

    def process_sample(self, value: float, *, timestamp: float) -> TtlPulse | None:
        try:
            sample = float(value)
            captured_at = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise TtlInputError("TTL 输入样本或时间戳无效，请检查 AI6 采集链路。") from exc
        if not math.isfinite(sample) or not math.isfinite(captured_at):
            raise TtlInputError("TTL 输入样本或时间戳包含非有限值，请检查 AI6 采集链路。")

        desired: bool | None = None
        if not self._stable_high and sample >= self.config.high_threshold_v:
            if self._seen_low:
                desired = True
        elif self._stable_high and sample <= self.config.low_threshold_v:
            desired = False
        elif not self._stable_high and sample <= self.config.low_threshold_v:
            self._seen_low = True

        if desired is None:
            if (
                (not self._stable_high and sample <= self.config.low_threshold_v)
                or (self._stable_high and sample >= self.config.high_threshold_v)
            ):
                self._clear_candidate()
            return None

        if self._candidate_high != desired:
            self._candidate_high = desired
            self._candidate_since = captured_at
            if self.config.debounce_ms > 0:
                return None

        assert self._candidate_since is not None
        elapsed_ms = (captured_at - self._candidate_since) * 1000
        if elapsed_ms + 1e-9 < self.config.debounce_ms:
            return None

        self._stable_high = desired
        self._clear_candidate()
        if not desired:
            self._seen_low = True
            return None
        if self._arm_epoch is None:
            return None
        self._sequence += 1
        return TtlPulse(
            timestamp=captured_at,
            arm_epoch=self._arm_epoch,
            sequence=self._sequence,
        )

    def _clear_candidate(self) -> None:
        self._candidate_high = None
        self._candidate_since = None
