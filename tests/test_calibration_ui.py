import pytest
from PySide6.QtWidgets import QSpinBox, QPushButton, QLabel, QProgressBar
from app.views.calibration_view import CalibrationView

@pytest.fixture
def view(qtbot):
    widget = CalibrationView()
    qtbot.addWidget(widget)
    return widget

def test_calibration_ui_elements_exist(view):
    """Verify that all required UI elements for calibration session exist."""
    
    # Duration spinbox
    assert hasattr(view, '_duration_spin'), "Missing duration spinbox"
    assert isinstance(view._duration_spin, QSpinBox)
    assert view._duration_spin.value() == 10  # Default 10s
    
    # Start/Stop button
    assert hasattr(view, '_calibration_btn'), "Missing calibration button"
    assert isinstance(view._calibration_btn, QPushButton)
    assert "启动校准" in view._calibration_btn.text()
    
    # Status/Countdown label (or progress bar)
    assert hasattr(view, '_calibration_status'), "Missing calibration status label"
    assert isinstance(view._calibration_status, QLabel)
    
    # Stats labels
    assert hasattr(view, '_stats_max_label'), "Missing Max label"
    assert hasattr(view, '_stats_min_label'), "Missing Min label"
    assert hasattr(view, '_stats_offset_label'), "Missing Offset label"
    assert hasattr(view, '_stats_gain_label'), "Missing Gain label"
