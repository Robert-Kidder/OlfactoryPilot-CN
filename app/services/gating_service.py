from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class GatingState(str, Enum):
    NEUTRAL = "NEUTRAL"
    INHALE = "INHALE_ABOVE"
    EXHALE = "EXHALE_ABOVE"
    BLOCKED = "BLOCKED"


@dataclass
class GatingTransition:
    state: GatingState
    timestamp: float
    sample_value: float
    safety_state: str


class GatingService:
    def __init__(
        self,
        inhale_threshold: float = 0.2,
        exhale_threshold: float = -0.2,
    ) -> None:
        self.inhale_threshold = inhale_threshold
        self.exhale_threshold = exhale_threshold
        self.current_state = GatingState.NEUTRAL

    def set_thresholds(self, inhale: float, exhale: float) -> None:
        self.inhale_threshold = inhale
        self.exhale_threshold = exhale

    def update(self, value: float, safety_state: str) -> tuple[GatingState, bool]:
        """
        Update state based on a single sample.
        Returns (new_state, changed).
        """
        new_state = self._determine_state(value, safety_state)
        changed = new_state != self.current_state
        if changed:
            self.current_state = new_state
        return new_state, changed

    def process_batch(
        self,
        samples: list[float],
        safety_state: str,
        timestamp_start: float,
        dt: float = 0.01,
    ) -> list[GatingTransition]:
        """
        Process a batch of samples and return all state transitions.
        This is useful for accurate event logging from high-frequency data.
        """
        transitions = []
        for i, value in enumerate(samples):
            # Calculate timestamp for this sample
            # dt is 1/sampling_rate (e.g. 0.01s for 100Hz)
            ts = timestamp_start + i * dt
            
            new_state = self._determine_state(value, safety_state)
            
            if new_state != self.current_state:
                self.current_state = new_state
                transitions.append(
                    GatingTransition(
                        state=new_state,
                        timestamp=ts,
                        sample_value=value,
                        safety_state=safety_state,
                    )
                )
        return transitions

    def _determine_state(self, value: float, safety_state: str) -> GatingState:
        if safety_state not in ("SAFE", "WARNING"): # Assuming SAFE is the only truly safe state, but maybe allow WARNING? 
            # AC4: "Given SafetyState=LOW_FLOW or DATA_STALE ... gating state is 'BLOCKED'"
            # Checking main_controller, valid states are SAFE, LOW_FLOW, DATA_STALE, or hardware strings.
            # Usually only SAFE allows operation.
            if safety_state != "SAFE":
                return GatingState.BLOCKED

        if value >= self.inhale_threshold:
            return GatingState.INHALE
        if value <= self.exhale_threshold:
            return GatingState.EXHALE
        return GatingState.NEUTRAL
