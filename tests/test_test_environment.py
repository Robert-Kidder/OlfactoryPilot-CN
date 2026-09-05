from __future__ import annotations

import os
import stat
import uuid
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
    "仓库内 --basetemp 已重定向到系统临时目录，以避免污染 Git/HIL evidence gate。"
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
    actual = tmp_path_factory.getbasetemp().resolve()

    assert not actual.is_relative_to(PROJECT_ROOT)
    expected = os.environ.get(EXPECTED_BASETEMP_ENV)
    if expected is not None:
        assert actual == Path(expected).resolve()


def test_default_basetemp_keeps_pytest_semantics_without_redirect_notice(
    capsys,
    temp_policy_plugin,
) -> None:
    config = _config(None)

    temp_policy_plugin.pytest_configure(config)
    temp_policy_plugin.pytest_unconfigure(config)

    assert config.option.basetemp is None
    assert capsys.readouterr().err == ""


def test_repository_basetemp_is_redirected_once_and_cleaned(
    capsys,
    temp_policy_plugin,
) -> None:
    directory_name = f".pytest-contract-{uuid.uuid4().hex}"
    requested = Path("tests") / ".." / directory_name
    repository_path = PROJECT_ROOT / directory_name
    config = _config(str(requested))
    hook_options = temp_policy_plugin.pytest_configure.pytest_impl

    assert hook_options["tryfirst"] is True
    temp_policy_plugin.pytest_configure(config)
    managed_basetemp = Path(config.option.basetemp).resolve()
    readonly = managed_basetemp / "readonly.txt"
    readonly.write_text("owned by pytest session\n", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)

    assert capsys.readouterr().err.count(REDIRECT_NOTICE) == 1
    assert not managed_basetemp.is_relative_to(PROJECT_ROOT)
    assert not repository_path.exists()

    temp_policy_plugin.pytest_unconfigure(config)

    assert not managed_basetemp.exists()
    assert not repository_path.exists()

    temp_policy_plugin.pytest_unconfigure(config)


def test_repository_basetemp_rejects_system_temp_inside_repository(
    monkeypatch,
    capsys,
    temp_policy_plugin,
) -> None:
    config = _config(str(PROJECT_ROOT / ".pytest-contract-unsafe-temp"))
    monkeypatch.setattr(temp_policy_plugin.tempfile, "gettempdir", lambda: PROJECT_ROOT)

    with pytest.raises(pytest.UsageError, match="系统临时目录位于仓库内"):
        temp_policy_plugin.pytest_configure(config)

    assert config.option.basetemp.endswith(".pytest-contract-unsafe-temp")
    assert capsys.readouterr().err == ""


def test_external_basetemp_is_preserved_and_not_deleted(
    tmp_path: Path,
    capsys,
    temp_policy_plugin,
) -> None:
    external_basetemp = tmp_path / "explicit-basetemp"
    external_basetemp.mkdir()
    marker = external_basetemp / "user-owned.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    config = _config(str(external_basetemp))

    temp_policy_plugin.pytest_configure(config)
    temp_policy_plugin.pytest_unconfigure(config)

    assert config.option.basetemp == str(external_basetemp)
    assert capsys.readouterr().err == ""
    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_concurrent_sessions_get_distinct_managed_basetemps(
    capsys,
    temp_policy_plugin,
) -> None:
    directory_name = f".pytest-contract-{uuid.uuid4().hex}"
    requested = str(Path("tests") / ".." / directory_name)
    first = _config(requested)
    second = _config(requested)

    temp_policy_plugin.pytest_configure(first)
    temp_policy_plugin.pytest_configure(second)
    first_path = Path(first.option.basetemp).resolve()
    second_path = Path(second.option.basetemp).resolve()

    assert capsys.readouterr().err.count(REDIRECT_NOTICE) == 2
    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()

    temp_policy_plugin.pytest_unconfigure(first)
    assert not first_path.exists()
    assert second_path.exists()

    temp_policy_plugin.pytest_unconfigure(second)
    assert not second_path.exists()
