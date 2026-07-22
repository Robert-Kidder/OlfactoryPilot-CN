from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.models.actuation import (
    ActuationAction,
    ActuationCategory,
    ActuationMetricsUpdate,
    ActuationQualitySnapshot,
    ActuationReceipt,
    ActuationResult,
    ActuationStreamSnapshot,
    ActuationWarningTransition,
)


@dataclass(frozen=True, slots=True)
class ActuationMetricsConfig:
    target_ms: float = 20.0
    single_limit_ms: float = 30.0
    window_size: int = 100
    min_samples: int = 20

    @classmethod
    def from_mapping(cls, config: dict[str, Any] | None) -> ActuationMetricsConfig:
        values = config or {}
        try:
            candidate = cls(
                target_ms=float(values.get("actuation_jitter_target_ms", cls.target_ms)),
                single_limit_ms=float(
                    values.get("actuation_jitter_single_limit_ms", cls.single_limit_ms)
                ),
                window_size=int(values.get("actuation_jitter_window_size", cls.window_size)),
                min_samples=int(values.get("actuation_jitter_min_samples", cls.min_samples)),
            )
        except (TypeError, ValueError, OverflowError):
            return cls()
        if (
            not math.isfinite(candidate.target_ms)
            or not math.isfinite(candidate.single_limit_ms)
            or candidate.target_ms <= 0
            or candidate.single_limit_ms <= candidate.target_ms
            or candidate.window_size <= 0
            or candidate.min_samples <= 0
            or candidate.min_samples > candidate.window_size
        ):
            return cls()
        return candidate


class ActuationMetrics:
    """Single-owner rolling quality calculator for normal protocol actions."""

    def __init__(self, config: ActuationMetricsConfig | dict[str, Any] | None = None) -> None:
        self.config = (
            config
            if isinstance(config, ActuationMetricsConfig)
            else ActuationMetricsConfig.from_mapping(config)
        )
        self._open: deque[float] = deque(maxlen=self.config.window_size)
        self._close: deque[float] = deque(maxlen=self.config.window_size)
        self._combined: deque[float] = deque(maxlen=self.config.window_size)
        self._warning = {"open": False, "close": False, "combined": False}
        self._last_jitter_ms: float | None = None
        self._severe_latched = False

    @property
    def severe_latched(self) -> bool:
        return self._severe_latched

    def reset(self) -> None:
        self._open.clear()
        self._close.clear()
        self._combined.clear()
        self._warning = {"open": False, "close": False, "combined": False}
        self._last_jitter_ms = None
        self._severe_latched = False

    def acknowledge_severe(self) -> None:
        """Clear the explicit-rearm latch without erasing the rolling windows."""
        self._severe_latched = False

    def record(self, receipt: ActuationReceipt) -> ActuationMetricsUpdate:
        included = (
            receipt.result == ActuationResult.SUCCESS
            and receipt.category == ActuationCategory.NORMAL
            and not receipt.stale
            and receipt.jitter_ms is not None
        )
        if not included:
            return ActuationMetricsUpdate(included=False, snapshot=self.snapshot())

        jitter = float(receipt.jitter_ms)
        stream_name = receipt.action.value
        stream = self._open if receipt.action == ActuationAction.OPEN else self._close
        stream.append(jitter)
        self._combined.append(jitter)
        self._last_jitter_ms = jitter

        transitions: list[ActuationWarningTransition] = []
        for name in (stream_name, "combined"):
            values = stream if name == stream_name else self._combined
            p95 = self._p95(values)
            active = p95 is not None and p95 > self.config.target_ms
            if active != self._warning[name]:
                self._warning[name] = active
                assert p95 is not None
                transitions.append(ActuationWarningTransition(name, active, p95))

        severe = jitter > self.config.single_limit_ms
        if severe:
            self._severe_latched = True
        return ActuationMetricsUpdate(
            included=True,
            snapshot=self.snapshot(),
            warning_transitions=tuple(transitions),
            severe=severe,
        )

    def snapshot(self) -> ActuationQualitySnapshot:
        return ActuationQualitySnapshot(
            open=self._stream_snapshot("open", self._open),
            close=self._stream_snapshot("close", self._close),
            combined=self._stream_snapshot("combined", self._combined),
            last_jitter_ms=self._last_jitter_ms,
            severe_latched=self._severe_latched,
        )

    def _stream_snapshot(self, name: str, values: deque[float]) -> ActuationStreamSnapshot:
        p95 = self._p95(values)
        return ActuationStreamSnapshot(
            sample_count=len(values),
            p95_ms=p95,
            warning=self._warning[name],
            target_met=None if p95 is None else p95 < self.config.target_ms,
        )

    def _p95(self, values: deque[float]) -> float | None:
        if len(values) < self.config.min_samples:
            return None
        ordered = sorted(values)
        rank = math.ceil(0.95 * len(ordered))
        return ordered[rank - 1]
