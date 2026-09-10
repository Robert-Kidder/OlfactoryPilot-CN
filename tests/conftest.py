from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMenu
from shiboken6 import delete as delete_qt_object
from shiboken6 import isValid

from scripts.dev_temp import (
    CURRENT_SESSION_ENV,
    CURRENT_TOKEN_ENV,
    SESSION_ENV,
    TOKEN_ENV,
    DevTempError,
    Session,
    claim_session_from_environment,
    cleanup_session,
    create_session,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MANAGED_SESSION_ATTRIBUTE = "_olfactorypilot_managed_devtemp_session"
_PREVIOUS_SESSION_ENV_ATTRIBUTE = "_olfactorypilot_previous_devtemp_environment"
_BASETEMP_REDIRECT_NOTICE = (
    "pytest 临时目录已重定向到仓库内 owned session，以免污染仓库父目录或系统 TEMP。"
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """在 TempPathFactory 构造前建立或认领唯一 repo-local session。"""
    try:
        session = claim_session_from_environment(_PROJECT_ROOT)
        if session is None:
            session = create_session("pytest", _PROJECT_ROOT)
    except (DevTempError, OSError) as error:
        raise pytest.UsageError(f"无法建立 pytest owned session：{error}") from error
    config.option.basetemp = str(session.basetemp)
    setattr(config, _MANAGED_SESSION_ATTRIBUTE, session)
    setattr(
        config,
        _PREVIOUS_SESSION_ENV_ATTRIBUTE,
        (
            os.environ.get(CURRENT_SESSION_ENV),
            os.environ.get(CURRENT_TOKEN_ENV),
        ),
    )
    os.environ.pop(SESSION_ENV, None)
    os.environ.pop(TOKEN_ENV, None)
    os.environ[CURRENT_SESSION_ENV] = str(session.path)
    os.environ[CURRENT_TOKEN_ENV] = session.token
    print(_BASETEMP_REDIRECT_NOTICE, file=sys.stderr)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """只清理由本 pytest 进程持有的完整 session。"""
    session: Session | None = getattr(config, _MANAGED_SESSION_ATTRIBUTE, None)
    if session is None:
        return
    delattr(config, _MANAGED_SESSION_ATTRIBUTE)
    cleanup_error: DevTempError | OSError | None = None
    try:
        cleanup_session(session.path, session.token)
    except (DevTempError, OSError) as error:
        cleanup_error = error
        print(f"pytest owned session 清理失败并已保留：{error}", file=sys.stderr)
    finally:
        previous_session, previous_token = getattr(
            config,
            _PREVIOUS_SESSION_ENV_ATTRIBUTE,
            (None, None),
        )
        if hasattr(config, _PREVIOUS_SESSION_ENV_ATTRIBUTE):
            delattr(config, _PREVIOUS_SESSION_ENV_ATTRIBUTE)
        for name, previous in (
            (CURRENT_SESSION_ENV, previous_session),
            (CURRENT_TOKEN_ENV, previous_token),
        ):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
    if cleanup_error is not None:
        raise pytest.UsageError(
            f"pytest owned session 未能正常清理：{cleanup_error}"
        )


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
