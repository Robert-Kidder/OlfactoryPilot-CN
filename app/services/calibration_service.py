from __future__ import annotations

import time
from dataclasses import dataclass

@dataclass
class CalibrationResult:
    offset: float
    gain: float
    min_val: float
    max_val: float
    timestamp: float

class CalibrationSession:
    def __init__(self, duration_sec: float = 10.0, target_range: float = 10.0):
        self.duration_sec = duration_sec
        self.target_range = target_range
        self.is_active = False
        self.start_time: float | None = None
        self.current_max = float('-inf')
        self.current_min = float('inf')
        self.result: CalibrationResult | None = None

    def start(self) -> None:
        self.is_active = True
        self.start_time = time.time()
        self.current_max = float('-inf')
        self.current_min = float('inf')
        self.result = None

    def update(self, sample: float) -> None:
        if not self.is_active:
            return
        
        if sample > self.current_max:
            self.current_max = sample
        if sample < self.current_min:
            self.current_min = sample

    def stop(self) -> CalibrationResult | None:
        if not self.is_active:
            return None
        
        self.is_active = False
        
        # Calculate parameters (AC3)
        # Avoid division by zero
        span = self.current_max - self.current_min
        if span <= 1e-6:
            # Fallback for flatline
            offset = 0.0
            gain = 1.0
        else:
            offset = -(self.current_max + self.current_min) / 2.0
            gain = self.target_range / span

        self.result = CalibrationResult(
            offset=offset,
            gain=gain,
            min_val=self.current_min,
            max_val=self.current_max,
            timestamp=time.time()
        )
        return self.result

    def get_progress(self) -> float:
        if not self.is_active or not self.start_time:
            return 0.0
        elapsed = time.time() - self.start_time
        return min(elapsed / self.duration_sec, 1.0)
    
    def is_finished(self) -> bool:
        if not self.is_active or not self.start_time:
            return False
        return (time.time() - self.start_time) >= self.duration_sec
