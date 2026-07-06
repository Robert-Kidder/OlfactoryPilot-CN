import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def qtbot(qt_app):
    class _Bot:
        def addWidget(self, widget):
            # No-op placeholder; tests only verify widgets exist.
            widget.setParent(None)

    return _Bot()
