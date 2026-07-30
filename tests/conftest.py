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


@pytest.fixture(autouse=True)
def deterministic_qt_thread_teardown(monkeypatch):
    """Track every Controller created by a test and join all owned threads."""
    from app.controllers.main_controller import MainController

    controllers = []
    original_init = MainController.__init__

    def tracked_init(controller, *args, **kwargs):
        original_init(controller, *args, **kwargs)
        controllers.append(controller)

    monkeypatch.setattr(MainController, "__init__", tracked_init)
    yield
    for controller in reversed(controllers):
        controller.teardown(timeout_ms=2000)
    app = QApplication.instance()
    if app is not None:
        for widget in list(app.topLevelWidgets()):
            widget.close()
            widget.deleteLater()
        app.processEvents()
