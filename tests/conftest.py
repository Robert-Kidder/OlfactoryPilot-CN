from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMenu
from shiboken6 import delete as delete_qt_object
from shiboken6 import isValid

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MANAGED_BASETEMP_ATTRIBUTE = "_olfactorypilot_managed_basetemp"
_BASETEMP_REDIRECT_NOTICE = (
    "仓库内 --basetemp 已重定向到系统临时目录，以避免污染 Git/HIL evidence gate。"
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Redirect only repository-contained basetemp before pytest builds its factory."""
    requested = config.option.basetemp
    if requested is None:
        return

    requested_path = Path(requested)
    if not requested_path.is_absolute():
        requested_path = Path.cwd() / requested_path
    resolved_requested = requested_path.resolve()
    if not resolved_requested.is_relative_to(_PROJECT_ROOT):
        return

    system_temp_root = Path(tempfile.gettempdir()).resolve()
    if system_temp_root.is_relative_to(_PROJECT_ROOT):
        raise pytest.UsageError(
            "系统临时目录位于仓库内，无法安全重定向 --basetemp。"
        )
    managed_basetemp = Path(
        tempfile.mkdtemp(prefix="olfactorypilot-pytest-", dir=system_temp_root)
    ).resolve()
    if managed_basetemp.is_relative_to(_PROJECT_ROOT):
        shutil.rmtree(managed_basetemp, onerror=_retry_remove_readonly)
        raise pytest.UsageError(
            "pytest 临时目录仍位于仓库内，已拒绝使用以避免污染 Git/HIL evidence gate。"
        )
    config.option.basetemp = str(managed_basetemp)
    setattr(config, _MANAGED_BASETEMP_ATTRIBUTE, managed_basetemp)
    print(_BASETEMP_REDIRECT_NOTICE, file=sys.stderr)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Delete only the unique OS temp directory owned by this pytest session."""
    managed_basetemp = getattr(config, _MANAGED_BASETEMP_ATTRIBUTE, None)
    if managed_basetemp is not None:
        delattr(config, _MANAGED_BASETEMP_ATTRIBUTE)
        if not managed_basetemp.exists():
            return
        shutil.rmtree(managed_basetemp, onerror=_retry_remove_readonly)


def _retry_remove_readonly(function, path: str, error_info) -> None:
    """Retry Windows cleanup for read-only files created by nested Git repos."""
    error = error_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, os.stat(path, follow_symlinks=False).st_mode | stat.S_IWRITE)
    function(path)


def _cleanup_qt_root_widgets(app: QApplication) -> None:
    """Synchronously delete parentless test windows and their timer-owning children."""

    # QFluentWidgets.FluentWindow derives from QWidget rather than QMainWindow,
    # so an abstract Qt base-class filter misses the application's real shell.
    # Import lazily to keep QT_QPA_PLATFORM configured before application code.
    from app.views.main_window import MainWindow as ProductMainWindow

    roots = [
        widget
        for widget in list(app.topLevelWidgets())
        if widget.parent() is None
        and widget.windowType() in {Qt.WindowType.Window, Qt.WindowType.Dialog}
        and isinstance(widget, QMainWindow | QDialog | ProductMainWindow)
        and not isinstance(widget, QMenu)
    ]
    for widget in roots:
        if isValid(widget):
            widget.close()
    app.processEvents()
    for widget in roots:
        if isValid(widget):
            # pytest does not run QApplication.exec(); on Windows, deleteLater()
            # can therefore retain a FluentWindow and its 30/20 FPS timers for
            # the remainder of the session.  The owning controller has already
            # been joined before this helper runs, so synchronous destruction is
            # deterministic and remains strictly test-only.
            delete_qt_object(widget)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def temp_policy_plugin():
    """Expose this conftest plugin to its focused contract tests."""
    return sys.modules[__name__]


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def qtbot(qt_app):
    widgets = []

    class _Bot:
        registered_widgets = widgets

        def addWidget(self, widget):
            widgets.append(widget)

    yield _Bot()
    for widget in reversed(widgets):
        if isValid(widget):
            widget.close()
            delete_qt_object(widget)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


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
        if isValid(controller):
            delete_qt_object(controller)
    app = QApplication.instance()
    if app is not None:
        _cleanup_qt_root_widgets(app)
