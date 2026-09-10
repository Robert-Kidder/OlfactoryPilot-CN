from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QMainWindow, QMenu, QWidget
from shiboken6 import isValid

from app.controllers import MainController
from app.main import DEFAULT_CONFIG, load_config
from app.models import AppState
from app.services import MockHAL
from app.views import MainWindow
from app.workers import HardwareWorker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REDIRECT_NOTICE = (
    "pytest 临时目录已重定向到仓库内 owned session，以免污染仓库父目录或系统 TEMP。"
)
EXPECTED_BASETEMP_ENV = "OLFACTORYPILOT_EXPECTED_BASETEMP"


def _config(basetemp: str | None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


def test_qt_cleanup_deletes_only_parentless_root_windows(
    qt_app, temp_policy_plugin
) -> None:
    root = QMainWindow()
    child = QWidget(root)
    excluded_menu = QMenu()
    root.show()
    excluded_menu.show()

    temp_policy_plugin._cleanup_qt_root_widgets(qt_app)

    assert not isValid(root)
    assert not isValid(child)
    assert isValid(excluded_menu)
    excluded_menu.close()
    excluded_menu.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


def test_qt_cleanup_deletes_fluent_product_window(
    qt_app, temp_policy_plugin
) -> None:
    state = AppState.from_config(load_config(DEFAULT_CONFIG))
    controller = MainController(
        state,
        HardwareWorker(telemetry_hz=5, hal=MockHAL(), simulation=True),
    )
    window = MainWindow(controller, state)

    temp_policy_plugin._cleanup_qt_root_widgets(qt_app)

    assert not isValid(window)


def test_qtbot_tracks_widgets_without_reparenting(qtbot) -> None:
    root = QWidget()
    child = QWidget(root)

    qtbot.addWidget(root)
    qtbot.addWidget(child)

    assert child.parentWidget() is root
    assert qtbot.registered_widgets == [root, child]


def test_actual_pytest_basetemp_contract(tmp_path_factory) -> None:
    from scripts.dev_temp import path_is_in_current_session

    actual = tmp_path_factory.getbasetemp().resolve()

    assert path_is_in_current_session(actual, PROJECT_ROOT)
    assert actual.name == "basetemp"
    assert actual.parent.name.startswith("run-")
    expected = os.environ.get(EXPECTED_BASETEMP_ENV)
    if expected is not None:
        assert actual == Path(expected).resolve()


def test_default_basetemp_uses_owned_session_and_cleans_it(
    capsys,
    monkeypatch,
    temp_policy_plugin,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV

    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config = _config(None)

    temp_policy_plugin.pytest_configure(config)
    managed_basetemp = Path(config.option.basetemp).resolve()
    temp_policy_plugin.pytest_unconfigure(config)

    assert managed_basetemp.is_relative_to(PROJECT_ROOT / ".devtmp" / "pytest")
    assert not managed_basetemp.parent.exists()
    assert capsys.readouterr().err.count(REDIRECT_NOTICE) == 1


@pytest.mark.parametrize("requested", ["relative-root", None, "C:/external-root"])
def test_any_requested_basetemp_is_redirected_once_and_cleaned(
    capsys,
    monkeypatch,
    temp_policy_plugin,
    requested: str | None,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV

    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config = _config(requested)
    hook_options = temp_policy_plugin.pytest_configure.pytest_impl

    assert hook_options["tryfirst"] is True
    temp_policy_plugin.pytest_configure(config)
    managed_basetemp = Path(config.option.basetemp).resolve()
    managed_basetemp.mkdir()
    (managed_basetemp / "owned.txt").write_text("owned\n", encoding="utf-8")

    assert capsys.readouterr().err.count(REDIRECT_NOTICE) == 1
    assert managed_basetemp.is_relative_to(PROJECT_ROOT / ".devtmp" / "pytest")

    temp_policy_plugin.pytest_unconfigure(config)

    assert not managed_basetemp.parent.exists()

    temp_policy_plugin.pytest_unconfigure(config)


def test_wrapper_session_is_claimed_by_pytest_and_cleaned(
    monkeypatch,
    temp_policy_plugin,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV, create_session

    wrapper = create_session("pytest", PROJECT_ROOT)
    monkeypatch.setenv(SESSION_ENV, str(wrapper.path))
    monkeypatch.setenv(TOKEN_ENV, wrapper.token)
    config = _config(str(wrapper.basetemp))

    temp_policy_plugin.pytest_configure(config)
    assert Path(config.option.basetemp).parent == wrapper.path
    temp_policy_plugin.pytest_unconfigure(config)

    assert not wrapper.path.exists()


def test_concurrent_sessions_get_distinct_managed_basetemps(
    capsys,
    monkeypatch,
    temp_policy_plugin,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV

    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    first = _config(None)
    second = _config(None)

    temp_policy_plugin.pytest_configure(first)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    temp_policy_plugin.pytest_configure(second)
    first_path = Path(first.option.basetemp).resolve()
    second_path = Path(second.option.basetemp).resolve()

    assert capsys.readouterr().err.count(REDIRECT_NOTICE) == 2
    assert first_path != second_path
    assert first_path.parent.exists()
    assert second_path.parent.exists()

    temp_policy_plugin.pytest_unconfigure(second)
    assert not second_path.parent.exists()
    assert first_path.parent.exists()

    temp_policy_plugin.pytest_unconfigure(first)
    assert not first_path.parent.exists()


def test_bare_pytest_reports_cleanup_failure_and_preserves_session(
    monkeypatch,
    temp_policy_plugin,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV, cleanup_session

    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config = _config(None)
    temp_policy_plugin.pytest_configure(config)
    session = getattr(config, temp_policy_plugin._MANAGED_SESSION_ATTRIBUTE)
    original_cleanup = temp_policy_plugin.cleanup_session

    def fail_cleanup(_path, _token):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(temp_policy_plugin, "cleanup_session", fail_cleanup)
    try:
        with pytest.raises(pytest.UsageError, match="未能正常清理"):
            temp_policy_plugin.pytest_unconfigure(config)
        assert session.path.exists()
    finally:
        monkeypatch.setattr(temp_policy_plugin, "cleanup_session", original_cleanup)
        cleanup_session(session.path, session.token)
