import pytest
from app.services.gating_service import GatingService, GatingState
from app.models import SafetyState

class MockLogger:
    def __init__(self):
        self.logs = []
    
    def info(self, msg, *args, **kwargs):
        self.logs.append((msg, args, kwargs))

def test_gating_initial_state():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    assert service.current_state == GatingState.NEUTRAL

def test_gating_inhale_transition():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    
    # Below threshold
    state, changed = service.update(0.1, safety_state="SAFE")
    assert state == GatingState.NEUTRAL
    assert not changed
    
    # Above threshold
    state, changed = service.update(0.6, safety_state="SAFE")
    assert state == GatingState.INHALE
    assert changed

def test_gating_exhale_transition():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    
    # Above exhale threshold (less negative)
    state, changed = service.update(-0.1, safety_state="SAFE")
    assert state == GatingState.NEUTRAL
    
    # Below exhale threshold (more negative)
    state, changed = service.update(-0.6, safety_state="SAFE")
    assert state == GatingState.EXHALE
    assert changed

def test_gating_safety_block():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    
    # High flow but unsafe
    state, changed = service.update(0.6, safety_state="LOW_FLOW")
    assert state == GatingState.BLOCKED
    assert changed
    
    # Back to safe
    state, changed = service.update(0.6, safety_state="SAFE")
    assert state == GatingState.INHALE
    assert changed

def test_threshold_updates():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    service.update(0.4, safety_state="SAFE")
    assert service.current_state == GatingState.NEUTRAL
    
    # Lower inhale threshold so 0.4 is now inhale
    service.set_thresholds(inhale=0.3, exhale=-0.5)
    state, changed = service.update(0.4, safety_state="SAFE")
    assert state == GatingState.INHALE
    assert changed

def test_batch_update():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    samples = [0.1, 0.2, 0.6, 0.7, 0.1] # Neutral -> Inhale -> Neutral
    
    transitions = service.process_batch(samples, safety_state="SAFE", timestamp_start=100.0, dt=0.01)
    
    # Should detect transition to INHALE at index 2 (100.02s)
    # And transition to NEUTRAL at index 4 (100.04s)
    assert len(transitions) == 2
    
    t1 = transitions[0]
    assert t1.state == GatingState.INHALE
    assert t1.sample_value == 0.6
    assert abs(t1.timestamp - 100.02) < 1e-6
    
    t2 = transitions[1]
    assert t2.state == GatingState.NEUTRAL
    assert t2.sample_value == 0.1
    assert abs(t2.timestamp - 100.04) < 1e-6

def test_batch_safety_blocked():
    service = GatingService(inhale_threshold=0.5, exhale_threshold=-0.5)
    samples = [0.6, 0.7]
    transitions = service.process_batch(samples, safety_state="LOW_FLOW", timestamp_start=100.0, dt=0.01)
    
    # Should immediate transition to BLOCKED if not already
    assert len(transitions) == 1
    assert transitions[0].state == GatingState.BLOCKED

