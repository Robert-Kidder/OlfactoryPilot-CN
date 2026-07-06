import pytest
from PySide6.QtWidgets import QGroupBox, QProgressBar, QSplitter

from app.views.calibration_view import CalibrationView


@pytest.fixture
def view(qtbot):
    widget = CalibrationView()
    qtbot.addWidget(widget)
    return widget

def test_new_layout_structure(view):
    """Verify the new layout structure exists (Splitter, Progress Bar)."""

    # Check for Splitter
    splitters = view.findChildren(QSplitter)
    assert len(splitters) >= 1, "QSplitter not found in layout"

    # Check for Progress Bar
    progress_bars = view.findChildren(QProgressBar)
    assert len(progress_bars) >= 1, "QProgressBar not found"

    # Check for Feedback GroupBox
    groups = view.findChildren(QGroupBox)
    feedback_group = next((g for g in groups if "状态" in g.title() or "Feedback" in g.title()), None)
    assert feedback_group is not None, "Feedback/Status GroupBox not found"

def test_progress_update(view):
    """Verify progress bar update method."""
    assert hasattr(view, 'set_calibration_progress')
    view.set_calibration_progress(50)
    # Ideally find the specific bar, but generic check:
    bar = view.findChild(QProgressBar)
    assert bar.value() == 50
