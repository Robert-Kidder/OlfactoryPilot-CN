from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Iterable


@dataclass
class FrameStats:
    fps_avg: float = 0.0
    fps_p95: float = 0.0
    window_s: float = 10.0
    frame_count: int = 0
    sample_count: int = 0
    warning_flag: bool = False
    reason: str | None = None


class FrameRateTracker:
    """Track render frame timings and raise warnings on sustained FPS drops."""

    def __init__(
        self,
        *,
        window_s: float = 10.0,
        warn_threshold: float = 30.0,
        warn_duration: float = 2.0,
        recover_duration: float = 5.0,
    ) -> None:
        self.window_s = window_s
        self.warn_threshold = warn_threshold
        self.warn_duration = warn_duration
        self.recover_duration = recover_duration
        self._timestamps: deque[float] = deque()
        self._low_start: float | None = None
        self._recover_start: float | None = None
        self.warning_active = False
        self.last_stats = FrameStats(window_s=self.window_s)

    def record_frame(self, *, timestamp: float | None = None, sample_count: int = 0) -> FrameStats:
        ts = time.time() if timestamp is None else timestamp
        self._timestamps.append(ts)
        self._prune(ts)
        stats = self._compute_stats(sample_count=sample_count)
        stats = self._apply_warning_state(stats, ts)
        self.last_stats = stats
        return stats

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _compute_stats(self, *, sample_count: int = 0) -> FrameStats:
        if len(self._timestamps) < 2:
            return replace(
                self.last_stats,
                fps_avg=0.0,
                fps_p95=0.0,
                frame_count=len(self._timestamps),
                sample_count=sample_count,
                warning_flag=False,
                reason=None,
            )

        intervals = [b - a for a, b in zip(self._timestamps, list(self._timestamps)[1:])]
        durations = sum(intervals)
        if durations <= 0:
            fps_avg = 0.0
            fps_p95 = 0.0
        else:
            fps_values = [1.0 / max(dt, 1e-6) for dt in intervals]
            fps_avg = len(fps_values) / durations
            fps_p95 = self._percentile(fps_values, 0.05)

        return FrameStats(
            fps_avg=fps_avg,
            fps_p95=fps_p95,
            window_s=self.window_s,
            frame_count=len(self._timestamps),
            sample_count=sample_count,
            warning_flag=False,
            reason=None,
        )

    def _apply_warning_state(self, stats: FrameStats, timestamp: float) -> FrameStats:
        warning = stats.warning_flag
        reason = stats.reason
        if stats.fps_p95 < self.warn_threshold:
            if self._low_start is None:
                self._low_start = timestamp
            self._recover_start = None
            if timestamp - self._low_start >= self.warn_duration:
                self.warning_active = True
                warning = True
                reason = "fps_low"
        else:
            self._low_start = None
            if self.warning_active:
                if self._recover_start is None:
                    self._recover_start = timestamp
                if timestamp - self._recover_start >= self.recover_duration:
                    self.warning_active = False
                    self._recover_start = None
            warning = self.warning_active
            reason = "fps_low" if self.warning_active else None

        return replace(stats, warning_flag=warning, reason=reason)

    @staticmethod
    def _percentile(values: Iterable[float], quantile: float) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        k = int(round((len(ordered) - 1) * quantile))
        return ordered[min(k, len(ordered) - 1)]


class BreathSampleBuffer:
    """Fixed window buffer for breath samples with simple gap filling."""

    def __init__(self, *, window_s: float = 10.0, sample_hz: float = 100.0) -> None:
        self.window_s = window_s
        self.expected_interval = 1.0 / sample_hz if sample_hz > 0 else 0.01
        self.samples: deque[tuple[float, float]] = deque()
        self._last_value: float = 0.0

    def append_samples(
        self,
        samples: Iterable[float],
        *,
        timestamp: float | None = None,
        interval_s: float | None = None,
    ) -> None:
        interval = interval_s or self.expected_interval
        base_ts = time.time() if timestamp is None else timestamp
        data = list(samples)
        if not data:
            return

        start_ts = base_ts - (len(data) - 1) * interval
        last_ts = self.samples[-1][0] if self.samples else None
        if last_ts is not None and start_ts - last_ts > interval * 1.5:
            self._fill_gap(last_ts, start_ts, interval)

        for idx, value in enumerate(data):
            sample_ts = start_ts + idx * interval
            self.samples.append((sample_ts, float(value)))
            self._last_value = float(value)
        self._prune(base_ts)

    def values(self) -> list[float]:
        return [val for _, val in self.samples]

    def latest_value(self) -> float | None:
        return self.samples[-1][1] if self.samples else None

    def is_stale(self, *, now: float | None = None, stale_after_s: float = 1.0) -> bool:
        if not self.samples:
            return True
        ts_now = time.time() if now is None else now
        age = ts_now - self.samples[-1][0]
        return age >= stale_after_s

    def seconds_since_update(self, *, now: float | None = None) -> float:
        if not self.samples:
            return float("inf")
        ts_now = time.time() if now is None else now
        return ts_now - self.samples[-1][0]

    def _fill_gap(self, last_ts: float, next_ts: float, interval: float) -> None:
        gap = next_ts - last_ts
        max_fill = min(int(gap / interval), int(self.window_s / interval))
        for i in range(1, max_fill):
            filler_ts = last_ts + i * interval
            if filler_ts >= next_ts:
                break
            self.samples.append((filler_ts, self._last_value))

    def _prune(self, reference_ts: float) -> None:
        cutoff = reference_ts - self.window_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
