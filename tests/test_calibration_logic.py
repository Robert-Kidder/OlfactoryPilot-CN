from app.services.calibration_service import CalibrationResult, CalibrationSession


def test_calibration_session_workflow():
    session = CalibrationSession(duration_sec=0.1)

    # Initial state
    assert not session.is_active
    assert session.result is None

    # Start
    session.start()
    assert session.is_active

    # Ingest data
    session.update(0.5)
    session.update(1.5)

    # Check stats
    assert session.current_max == 1.5
    assert session.current_min == 0.5

    # Stop (or finish)
    result = session.stop()
    assert not session.is_active
    assert isinstance(result, CalibrationResult)

    # Check calculation
    # Offset = -(Max + Min) / 2 = -(1.5 + 0.5) / 2 = -1.0
    # Gain = TargetRange / (Max - Min) = 10.0 / (1.5 - 0.5) = 10.0 (Assuming default target range 10)
    assert result.offset == -1.0
    assert result.gain == 10.0

def test_calibration_reset():
    session = CalibrationSession(duration_sec=10)
    session.start()
    session.update(1.0)
    session.stop()

    session.start()
    # Stats should reset
    assert session.current_max == float('-inf')
    assert session.current_min == float('inf')
