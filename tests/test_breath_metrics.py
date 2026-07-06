import pytest

from app.services.breath_metrics import BreathSampleBuffer, FrameRateTracker


class TestFrameRateTracker:
    def test_initial_state(self):
        tracker = FrameRateTracker()
        assert tracker.warning_active is False
        assert tracker.last_stats.fps_avg == 0.0

    def test_warning_trigger_timing(self):
        """Verify warning triggers only after sustained low FPS."""
        tracker = FrameRateTracker(
            window_s=10.0,
            warn_threshold=30.0,
            warn_duration=2.0,
            recover_duration=5.0
        )
        ts = 100.0

        # 1. Start with good FPS (50Hz = 0.02s interval)
        for _ in range(10):
            tracker.record_frame(timestamp=ts)
            ts += 0.02
        assert tracker.warning_active is False

        # 2. Drop to bad FPS (20Hz = 0.05s interval)
        # We need to simulate enough frames for 1.9s, then check
        start_bad_ts = ts
        while ts - start_bad_ts < 1.9:
            stats = tracker.record_frame(timestamp=ts)
            ts += 0.05

        # Should still be safe (duration < 2.0s)
        assert stats.warning_flag is False

        # 3. Push over the 2.0s threshold
        while ts - start_bad_ts < 2.1:
            stats = tracker.record_frame(timestamp=ts)
            ts += 0.05

        assert stats.warning_flag is True
        assert stats.reason == "fps_low"

    def test_recovery_timing(self):
        """Verify warning clears only after sustained good FPS."""
        tracker = FrameRateTracker(
            window_s=0.1,  # Instant metric reaction
            warn_threshold=30.0,
            warn_duration=1.0,
            recover_duration=2.0
        )
        ts = 100.0

        # 1. Trigger warning
        for _ in range(50): # 2.5s of bad frames (0.05s)
            stats = tracker.record_frame(timestamp=ts)
            ts += 0.05
        assert stats.warning_flag is True

        # 2. Start good frames (0.02s)
        start_good_ts = ts

        # Run for 1.9s (just under 2.0s recovery)
        while ts - start_good_ts < 1.9:
            stats = tracker.record_frame(timestamp=ts)
            ts += 0.02

        # Should still be warning
        assert stats.warning_flag is True

        # 3. Push over recovery threshold
        while ts - start_good_ts < 2.1:
            stats = tracker.record_frame(timestamp=ts)
            ts += 0.02

        assert stats.warning_flag is False
        assert stats.reason is None

    def test_empty_stats(self):
        tracker = FrameRateTracker()
        stats = tracker.record_frame(timestamp=0.0)
        assert stats.fps_avg == 0.0

class TestBreathSampleBuffer:
    def test_initial_state(self):
        buffer = BreathSampleBuffer()
        assert buffer.values() == []
        assert buffer.latest_value() is None
        assert buffer.is_stale(now=100.0) is True

    def test_append_samples_normal(self):
        buffer = BreathSampleBuffer(window_s=10.0, sample_hz=10.0)
        # Append 10 samples ending at t=10.0, interval=0.1s
        # t=9.1, 9.2, ..., 10.0
        samples = [float(i) for i in range(10)]
        buffer.append_samples(samples, timestamp=10.0, interval_s=0.1)

        assert len(buffer.values()) == 10
        assert buffer.latest_value() == 9.0
        # Check timestamps roughly
        assert buffer.samples[-1][0] == 10.0
        assert buffer.samples[0][0] == pytest.approx(9.1)

    def test_gap_filling(self):
        """Verify gaps are filled with the last known value."""
        buffer = BreathSampleBuffer(window_s=10.0, sample_hz=10.0)

        # 1. Initial data: t=0.0, val=1.0
        buffer.append_samples([1.0], timestamp=0.0, interval_s=0.1)

        # 2. Add data with gap: t=1.0 (gap size ~0.9s -> ~9 missing samples)
        # Expected behavior: fill from 0.1 to 0.9 with 1.0, then add 2.0 at 1.0
        buffer.append_samples([2.0], timestamp=1.0, interval_s=0.1)

        values = buffer.values()
        # Should have t=0.0, then gap fillers, then t=1.0
        # Gap is 1.0s, interval 0.1s => ~10 slots total.
        assert len(values) >= 10
        # Check filler values
        assert values[1] == 1.0
        assert values[-2] == 1.0
        assert values[-1] == 2.0

    def test_pruning(self):
        """Verify old samples are removed."""
        buffer = BreathSampleBuffer(window_s=1.0, sample_hz=10.0) # 1s window

        # Add 2s worth of data
        samples = [0.0] * 20
        buffer.append_samples(samples, timestamp=2.0, interval_s=0.1)

        # Should only keep last ~1s (10 samples)
        assert len(buffer.values()) <= 11
        assert buffer.samples[0][0] >= 1.0

    def test_staleness(self):
        buffer = BreathSampleBuffer()
        buffer.append_samples([1.0], timestamp=10.0)

        assert buffer.is_stale(now=10.5, stale_after_s=1.0) is False
        assert buffer.is_stale(now=11.1, stale_after_s=1.0) is True
