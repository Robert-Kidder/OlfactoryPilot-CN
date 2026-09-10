from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_CI = PROJECT_ROOT / "scripts" / "run-ci.ps1"
RUN_CLEAN_CLONE = PROJECT_ROOT / "scripts" / "run-clean-clone.ps1"
DEV_TEMP = PROJECT_ROOT / "scripts" / "dev_temp.py"
CANDIDATE_SHA = "4b277d162a6bffd470c8e201d9d40ff43d184739"


def _write_helper_python(fake_bin: Path) -> Path:
    helper_python = fake_bin / "helper-python.cmd"
    helper_python.write_text(
        "@echo off\r\n"
        "echo %*|findstr /C:\" cleanup \" >nul\r\n"
        "if not errorlevel 1 if not \"%FAKE_HELPER_CLEANUP_EXIT%\"==\"0\" exit /b %FAKE_HELPER_CLEANUP_EXIT%\r\n"
        "\"%REAL_PYTHON%\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )
    return helper_python


def _run_with_fake_python(
    tmp_path: Path,
    task: str,
    *,
    exit_code: int,
    exit_codes: tuple[int, ...] = (),
    create_dist: bool = False,
    missing_artifact: str = "",
    create_basetemp: bool = False,
    cleanup_exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    sandbox = tmp_path / "repository"
    scripts = sandbox / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUN_CI, scripts / RUN_CI.name)
    shutil.copy2(DEV_TEMP, scripts / DEV_TEMP.name)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    helper_python = _write_helper_python(fake_bin)
    log_path = tmp_path / "python-arguments.log"
    (fake_bin / "python.cmd").write_text(
        "@echo off\r\n"
        "setlocal EnableDelayedExpansion\r\n"
        "echo %*>>\"%FAKE_PYTHON_LOG%\"\r\n"
        "for /f %%C in ('find /c /v \"\" ^< \"%FAKE_PYTHON_LOG%\"') do set \"CALL_INDEX=%%C\"\r\n"
        "set \"LAST_ARGUMENT=\"\r\n"
        "for %%A in (%*) do set \"LAST_ARGUMENT=%%~A\"\r\n"
        "if \"%FAKE_PYTHON_CREATE_BASETEMP%\"==\"1\" (\r\n"
        "  if not exist \"!LAST_ARGUMENT!\" mkdir \"!LAST_ARGUMENT!\"\r\n"
        "  echo sentinel>\"!LAST_ARGUMENT!\\sentinel.txt\"\r\n"
        ")\r\n"
        "echo %*|findstr /C:\"PyInstaller\" >nul\r\n"
        "if not errorlevel 1 if \"%FAKE_PYTHON_CREATE_DIST%\"==\"1\" (\r\n"
        "  if not exist dist\\OlfactoryPilot\\_internal\\config mkdir dist\\OlfactoryPilot\\_internal\\config\r\n"
        "  if not exist dist\\OlfactoryPilot\\_internal\\docs mkdir dist\\OlfactoryPilot\\_internal\\docs\r\n"
        "  if /I not \"%FAKE_MISSING_ARTIFACT%\"==\"exe\" echo artifact>dist\\OlfactoryPilot\\OlfactoryPilot.exe\r\n"
        "  if /I not \"%FAKE_MISSING_ARTIFACT%\"==\"config\" echo {}>dist\\OlfactoryPilot\\_internal\\config\\default_config.json\r\n"
        "  if /I not \"%FAKE_MISSING_ARTIFACT%\"==\"manual\" echo manual>dist\\OlfactoryPilot\\_internal\\docs\\ManuelUtilisation_ProgOlfacto.pdf\r\n"
        ")\r\n"
        "set \"EXIT_CODE=%FAKE_PYTHON_EXIT%\"\r\n"
        "if \"!CALL_INDEX!\"==\"1\" if defined FAKE_PYTHON_EXIT_1 set \"EXIT_CODE=%FAKE_PYTHON_EXIT_1%\"\r\n"
        "if \"!CALL_INDEX!\"==\"2\" if defined FAKE_PYTHON_EXIT_2 set \"EXIT_CODE=%FAKE_PYTHON_EXIT_2%\"\r\n"
        "if \"!CALL_INDEX!\"==\"3\" if defined FAKE_PYTHON_EXIT_3 set \"EXIT_CODE=%FAKE_PYTHON_EXIT_3%\"\r\n"
        "exit /b !EXIT_CODE!\r\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_PYTHON_LOG"] = str(log_path)
    env["FAKE_PYTHON_EXIT"] = str(exit_code)
    for index, code in enumerate(exit_codes, start=1):
        env[f"FAKE_PYTHON_EXIT_{index}"] = str(code)
    env["FAKE_PYTHON_CREATE_DIST"] = "1" if create_dist else "0"
    env["FAKE_MISSING_ARTIFACT"] = missing_artifact
    env["FAKE_PYTHON_CREATE_BASETEMP"] = "1" if create_basetemp else "0"
    env["REAL_PYTHON"] = sys.executable
    env["FAKE_HELPER_CLEANUP_EXIT"] = str(cleanup_exit_code)
    env["OLFACTORYPILOT_DEV_PYTHON"] = str(helper_python)

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / RUN_CI.name),
            task,
        ],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    arguments = (
        log_path.read_text(encoding="utf-8").splitlines()
        if log_path.exists()
        else []
    )
    return result, arguments


def _assert_owned_pytest_arguments(argument: str, prefix: str) -> Path:
    command, separator, raw_basetemp = argument.partition(" --basetemp ")
    assert separator
    assert command == prefix
    basetemp = Path(raw_basetemp)
    assert basetemp.name == "basetemp"
    assert basetemp.parent.name.startswith("run-")
    assert basetemp.parent.parent.name == "pytest"
    assert basetemp.parent.parent.parent.name == ".devtmp"
    return basetemp


def _run_clean_clone_with_fake_tools(
    tmp_path: Path,
    *,
    fail_match: str = "",
    cleanup_exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    sandbox = tmp_path / "repository"
    scripts = sandbox / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUN_CLEAN_CLONE, scripts / RUN_CLEAN_CLONE.name)
    shutil.copy2(DEV_TEMP, scripts / DEV_TEMP.name)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    helper_python = _write_helper_python(fake_bin)
    log_path = tmp_path / "clean-clone-commands.log"
    (fake_bin / "git.cmd").write_text(
        "@echo off\r\n"
        "setlocal EnableDelayedExpansion\r\n"
        "echo git %*>>\"%FAKE_CLEAN_LOG%\"\r\n"
        "if /I \"%3\"==\"rev-parse\" (\r\n"
        "  echo %FAKE_CANDIDATE_SHA%\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "set \"LAST_ARGUMENT=\"\r\n"
        "for %%A in (%*) do set \"LAST_ARGUMENT=%%~A\"\r\n"
        "if /I \"%1\"==\"clone\" (\r\n"
        "  mkdir \"!LAST_ARGUMENT!\"\r\n"
        "  echo requirements>\"!LAST_ARGUMENT!\\requirements-dev.txt\"\r\n"
        "  echo spec>\"!LAST_ARGUMENT!\\pyinstaller.spec\"\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    fake_python = fake_bin / "python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        "setlocal EnableDelayedExpansion\r\n"
        "if /I \"%1\"==\"-c\" (\r\n"
        "  echo python -c>>\"%FAKE_CLEAN_LOG%\"\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "echo python %*>>\"%FAKE_CLEAN_LOG%\"\r\n"
        "if \"%FAKE_CLEAN_FAIL_MATCH%\"==\"\" goto no_failure\r\n"
        "echo %*|findstr /C:\"%FAKE_CLEAN_FAIL_MATCH%\" >nul\r\n"
        "if not errorlevel 1 exit /b 37\r\n"
        ":no_failure\r\n"
        "if /I \"%1\"==\"-m\" if /I \"%2\"==\"venv\" (\r\n"
        "  mkdir \"%~3\\Scripts\"\r\n"
        "  copy /Y \"%~f0\" \"%~3\\Scripts\\python.cmd\" >nul\r\n"
        ")\r\n"
        "echo %*|findstr /C:\"PyInstaller\" >nul\r\n"
        "if not errorlevel 1 (\r\n"
        "  mkdir dist\\OlfactoryPilot\\_internal\\config\r\n"
        "  mkdir dist\\OlfactoryPilot\\_internal\\docs\r\n"
        "  echo artifact>dist\\OlfactoryPilot\\OlfactoryPilot.exe\r\n"
        "  echo {}>dist\\OlfactoryPilot\\_internal\\config\\default_config.json\r\n"
        "  echo manual>dist\\OlfactoryPilot\\_internal\\docs\\ManuelUtilisation_ProgOlfacto.pdf\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_CLEAN_LOG"] = str(log_path)
    env["FAKE_CLEAN_FAIL_MATCH"] = fail_match
    env["FAKE_CANDIDATE_SHA"] = CANDIDATE_SHA
    env["REAL_PYTHON"] = sys.executable
    env["FAKE_HELPER_CLEANUP_EXIT"] = str(cleanup_exit_code)
    env["OLFACTORYPILOT_DEV_PYTHON"] = str(helper_python)
    env["OLFACTORYPILOT_CLEAN_CLONE_PYTHON"] = str(fake_python)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / RUN_CLEAN_CLONE.name),
        ],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    commands = (
        log_path.read_text(encoding="utf-8").splitlines()
        if log_path.exists()
        else []
    )
    return result, commands, sandbox


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize(
    ("task", "stage", "exit_code"),
    [
        ("lint", "ruff", 17),
        ("test", "pytest full", 18),
        ("build", "PyInstaller", 19),
    ],
)
def test_run_ci_propagates_native_python_exit_code(
    tmp_path: Path,
    task: str,
    stage: str,
    exit_code: int,
) -> None:
    result, arguments = _run_with_fake_python(
        tmp_path,
        task,
        exit_code=exit_code,
    )

    assert result.returncode == exit_code
    assert len(arguments) == 1
    output = result.stdout + result.stderr
    assert stage in output
    assert str(exit_code) in output


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
def test_run_ci_fast_excludes_only_registered_slow_layer(tmp_path: Path) -> None:
    result, arguments = _run_with_fake_python(tmp_path, "test-fast", exit_code=0)

    assert result.returncode == 0
    assert len(arguments) == 1
    _assert_owned_pytest_arguments(arguments[0], '-m pytest -m "not slow"')
    pytest_config = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "slow:" in pytest_config


def test_fast_marker_collection_matches_reviewed_modules(tmp_path: Path) -> None:
    slow_modules = [
        "tests/test_app.py",
        "tests/test_flow_controls.py",
        "tests/test_cleaning_view.py",
        "tests/test_session_view.py",
    ]
    retained_modules = [
        "tests/test_safe_stop.py",
        "tests/test_actuation_worker.py",
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "not slow",
            *slow_modules,
            *retained_modules,
            "--basetemp",
            str(tmp_path / "collection-root"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    collected = result.stdout
    assert "test_safe_stop.py::" in collected
    assert "test_actuation_worker.py::" in collected
    assert all(f"{Path(module).name}::" not in collected for module in slow_modules)
    assert "no tests collected" not in collected.lower()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
def test_run_ci_full_gate_keeps_lint_full_pytest_and_build(tmp_path: Path) -> None:
    result, arguments = _run_with_fake_python(
        tmp_path,
        "ci",
        exit_code=0,
        create_dist=True,
    )

    assert result.returncode == 0
    assert arguments[0] == "-m ruff check ."
    _assert_owned_pytest_arguments(arguments[1], "-m pytest")
    assert arguments[2] == "-m PyInstaller --noconfirm pyinstaller.spec"


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize(
    ("exit_codes", "expected_code", "expected_stages"),
    [
        ((31, 0, 0), 31, ["-m ruff check ."]),
        (
            (0, 32, 0),
            32,
            ["-m ruff check .", "-m pytest"],
        ),
    ],
)
def test_run_ci_stops_after_first_failed_ci_stage(
    tmp_path: Path,
    exit_codes: tuple[int, ...],
    expected_code: int,
    expected_stages: list[str],
) -> None:
    result, arguments = _run_with_fake_python(
        tmp_path,
        "ci",
        exit_code=0,
        exit_codes=exit_codes,
        create_dist=True,
    )

    assert result.returncode == expected_code
    assert len(arguments) == len(expected_stages)
    assert all(
        arguments[index].startswith(stage)
        for index, stage in enumerate(expected_stages)
    )
    assert all("PyInstaller" not in argument for argument in arguments)


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize("exit_code", [0, 23])
def test_run_ci_removes_managed_basetemp_after_success_and_failure(
    tmp_path: Path,
    exit_code: int,
) -> None:
    result, _ = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=exit_code,
        create_basetemp=True,
    )

    assert result.returncode == exit_code
    assert not (tmp_path / "repository" / ".devtmp").exists()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize(("stage_exit", "expected_exit"), [(0, 1), (23, 23)])
def test_run_ci_cleanup_failure_is_reported_preserved_and_keeps_stage_exit(
    tmp_path: Path,
    stage_exit: int,
    expected_exit: int,
) -> None:
    result, _ = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=stage_exit,
        cleanup_exit_code=41,
    )

    assert result.returncode == expected_exit
    assert "Failed to clean pytest owned session" in result.stdout + result.stderr
    sessions = list((tmp_path / "repository" / ".devtmp" / "pytest").glob("run-*"))
    assert len(sessions) == 1
    assert (sessions[0] / ".devtmp-owner.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
def test_run_ci_never_uses_external_basetemp_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OLFACTORYPILOT_PYTEST_BASETEMP",
        str(tmp_path / "caller-requested-external-root"),
    )
    result, arguments = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=0,
        create_basetemp=True,
    )

    assert result.returncode == 0
    basetemp = _assert_owned_pytest_arguments(arguments[0], "-m pytest")
    assert basetemp.is_relative_to(tmp_path / "repository" / ".devtmp")
    assert not (tmp_path / "caller-requested-external-root").exists()
    assert not (tmp_path / "repository" / ".devtmp").exists()


@pytest.mark.skipif(os.name != "nt", reason="run-clean-clone.ps1 只支持 Windows")
@pytest.mark.parametrize(("fail_match", "expected_code"), [("", 0), ("-m pytest", 37)])
def test_clean_clone_keeps_all_work_in_owned_session_and_always_cleans(
    tmp_path_factory,
    fail_match: str,
    expected_code: int,
) -> None:
    tmp_path = tmp_path_factory.getbasetemp() / ("cc-fail" if fail_match else "cc-ok")
    tmp_path.mkdir(exist_ok=True)
    result, commands, sandbox = _run_clean_clone_with_fake_tools(
        tmp_path,
        fail_match=fail_match,
    )

    assert result.returncode == expected_code, result.stdout + result.stderr
    assert commands[0].endswith("rev-parse --verify HEAD{commit}")
    assert commands[1].startswith("git clone --no-local --no-hardlinks ")
    assert commands[2].endswith(f"checkout --detach {CANDIDATE_SHA}")
    assert f"Candidate revision: {CANDIDATE_SHA}" in result.stdout
    assert any("-m venv" in command for command in commands)
    assert any("-m ruff check ." in command for command in commands)
    assert any("-m pytest" in command for command in commands)
    if not fail_match:
        assert any("-m PyInstaller --noconfirm pyinstaller.spec" in command for command in commands)
        assert any("python -c" in command for command in commands)
    assert ".devtmp\\clean-clone\\run-" in commands[1]
    assert ".devtmp\\clean-clone\\run-" in commands[2]
    assert not (sandbox / ".devtmp").exists()


@pytest.mark.skipif(os.name != "nt", reason="run-clean-clone.ps1 只支持 Windows")
@pytest.mark.parametrize(("stage_failure", "expected_exit"), [("", 1), ("-m pytest", 37)])
def test_clean_clone_cleanup_failure_is_reported_preserved_and_keeps_stage_exit(
    tmp_path_factory,
    stage_failure: str,
    expected_exit: int,
) -> None:
    suffix = "cleanup-stage-fail" if stage_failure else "cleanup-only"
    tmp_path = tmp_path_factory.getbasetemp() / suffix
    tmp_path.mkdir(exist_ok=True)
    result, _, sandbox = _run_clean_clone_with_fake_tools(
        tmp_path,
        fail_match=stage_failure,
        cleanup_exit_code=41,
    )

    assert result.returncode == expected_exit
    assert "clean-clone owned session 清理失败" in result.stdout + result.stderr
    sessions = list((sandbox / ".devtmp" / "clean-clone").glob("run-*"))
    assert len(sessions) == 1
    assert (sessions[0] / ".devtmp-owner.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize(
    ("missing_artifact", "expected_name"),
    [
        ("exe", "OlfactoryPilot.exe"),
        ("config", "default_config.json"),
        ("manual", "ManuelUtilisation_ProgOlfacto.pdf"),
    ],
)
def test_run_ci_build_rejects_missing_required_artifact(
    tmp_path: Path,
    missing_artifact: str,
    expected_name: str,
) -> None:
    result, arguments = _run_with_fake_python(
        tmp_path,
        "build",
        exit_code=0,
        create_dist=True,
        missing_artifact=missing_artifact,
    )

    assert result.returncode != 0
    assert arguments == ["-m PyInstaller --noconfirm pyinstaller.spec"]
    assert expected_name in result.stdout + result.stderr


def test_distribution_inputs_are_minimal() -> None:
    spec = (PROJECT_ROOT / "pyinstaller.spec").read_text(encoding="utf-8")
    assert 'project_root / "config" / "default_config.json"' in spec
    assert 'project_root / "docs" / "ManuelUtilisation_ProgOlfacto.pdf"' in spec
    assert "docs_dir" not in spec


def test_local_config_template_keeps_connection_authorities_aligned() -> None:
    from app.main import load_effective_config
    from app.models import HardwareProfile
    from app.services import RealHAL

    example = json.loads(
        (PROJECT_ROOT / "config" / "local_config.example.json").read_text(
            encoding="utf-8"
        )
    )
    nested = example["hardware_profile"]["connections"]
    assert example["hal_mode"] == "mock"
    assert example["serial_port"] == nested["serial_port"]
    assert example["ni_devices"] == nested["ni_devices"]
    assert example["alicat_unit_ids"] == nested["alicat_unit_ids"]
    assert isinstance(example["signal_offset"], int | float)
    assert isinstance(example["signal_gain"], int | float)
    assert all(
        isinstance(example["cleaning"][key], int | float)
        for key in ("flow_sccm", "open_duration_s", "cycles")
    )
    assert all(
        isinstance(example["hardware_profile"]["verification_config"][key], int | float)
        for key in ("flow_sccm", "duration_s", "max_approved_flow_sccm")
    )

    merged = load_effective_config(
        PROJECT_ROOT / "config" / "default_config.json",
        PROJECT_ROOT / "config" / "local_config.example.json",
    )
    profile = HardwareProfile.from_config(merged)
    real_hal = RealHAL.from_config(merged)

    assert real_hal.serial_port == profile.connections.serial_port
    assert tuple(merged["ni_devices"]) == profile.connections.ni_device_ids
    assert real_hal._unit_ids == profile.connections.alicat_unit_ids


def test_representative_local_and_generated_paths_are_git_ignored() -> None:
    candidates = [
        "node_modules/example/package.json",
        "npm-debug.log.1",
        "Thumbs.db",
        "Desktop.ini",
        "run.session.part/state.json",
        "recovery/session.json",
        "config/local_config.json",
        ".devtmp/pytest/run-0123456789abcdef0123456789abcdef/marker.json",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=PROJECT_ROOT,
        input=("\n".join(candidates) + "\n").encode(),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().splitlines() == candidates


def test_hil_git_status_excludes_only_managed_devtmp(tmp_path: Path) -> None:
    sandbox = tmp_path / "repository"
    sandbox.mkdir()
    shutil.copy2(PROJECT_ROOT / ".gitignore", sandbox / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=sandbox, check=True)
    managed = sandbox / ".devtmp" / "pytest" / "run-owned" / "data.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("ignored\n", encoding="utf-8")
    ordinary = sandbox / "random-untracked-file.tmp"
    ordinary.write_text("must remain visible\n", encoding="utf-8")

    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.splitlines() == ["?? .gitignore", "?? random-untracked-file.tmp"]


def test_ui_capture_uses_owned_session_and_evidence_directory() -> None:
    source = (PROJECT_ROOT / "scripts" / "capture_story_4_6_ui.py").read_text(
        encoding="utf-8"
    )

    assert "create_session(\"ui-capture\", REPO_ROOT)" in source
    assert "cleanup_session(session.path, session.token)" in source
    assert '"sprint-artifacts" / "evidence" / "screenshots"' in source
    assert "tempfile" not in source


def test_ui_capture_failure_removes_its_owned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import capture_story_4_6_ui as capture

    purpose_root = PROJECT_ROOT / ".devtmp" / "ui-capture"
    before = set(purpose_root.glob("run-*")) if purpose_root.exists() else set()

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(capture, "build_application", fail_build)
    with pytest.raises(RuntimeError, match="injected capture failure"):
        capture.main()

    after = set(purpose_root.glob("run-*")) if purpose_root.exists() else set()
    assert after == before


def test_simulation_screenshot_manifest_matches_accepted_bytes() -> None:
    screenshot_root = PROJECT_ROOT / "docs" / "sprint-artifacts" / "evidence" / "screenshots"
    manifest = json.loads((screenshot_root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["classification"] == "simulation-ui-reference-fixture"
    assert manifest["physical_or_hil_evidence"] is False
    assert set(manifest["files"]) == {
        path.name for path in screenshot_root.glob("*.png")
    }
    for name, expected_hash in manifest["files"].items():
        assert hashlib.sha256((screenshot_root / name).read_bytes()).hexdigest() == expected_hash


def test_readme_keeps_tooling_guidance_stable() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/run-clean-clone.ps1" in readme
    assert ".devtmp/<purpose>/run-<uuid>/" in readme
    assert "提供可复用的实验协议编排与信号、事件记录基础" in readme
    assert "执行实验协议" not in readme
    assert "BMAD Method 6.12" not in readme
    assert "npx bmad-method@" not in readme
    assert "npx bmad-method install" in readme


def test_only_unfinished_execution_specs_remain_active() -> None:
    active_dir = PROJECT_ROOT / "docs" / "sprint-artifacts"
    terminal_statuses = {"done", "completed"}
    for path in active_dir.glob("spec-*.md"):
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        status_line = next(
            line for line in frontmatter.splitlines() if line.startswith("status:")
        )
        status = status_line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
        assert status not in terminal_statuses, path.name


def test_default_clone_configuration_is_simulation_safe() -> None:
    default = json.loads(
        (PROJECT_ROOT / "config" / "default_config.json").read_text(encoding="utf-8")
    )

    assert default["hal_mode"] == "mock"
    assert not (PROJECT_ROOT / "config" / "local_config.example.json").samefile(
        PROJECT_ROOT / "config" / "default_config.json"
    )
