from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_CI = PROJECT_ROOT / "scripts" / "run-ci.ps1"
_DEFAULT_BASETEMP = object()


def _run_with_fake_python(
    tmp_path: Path,
    task: str,
    *,
    exit_code: int,
    exit_codes: tuple[int, ...] = (),
    create_dist: bool = False,
    missing_artifact: str = "",
    create_basetemp: bool = False,
    basetemp: Path | str | None | object = _DEFAULT_BASETEMP,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    sandbox = tmp_path / "repository"
    scripts = sandbox / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUN_CI, scripts / RUN_CI.name)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
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
    if basetemp is _DEFAULT_BASETEMP:
        env["OLFACTORYPILOT_PYTEST_BASETEMP"] = str(tmp_path / "pytest-root")
    elif basetemp is None:
        env.pop("OLFACTORYPILOT_PYTEST_BASETEMP", None)
    else:
        env["OLFACTORYPILOT_PYTEST_BASETEMP"] = str(basetemp)

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
        check=False,
    )
    arguments = (
        log_path.read_text(encoding="utf-8").splitlines()
        if log_path.exists()
        else []
    )
    return result, arguments


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
    assert arguments == [
        f'-m pytest -m "not slow" --basetemp {tmp_path / "pytest-root"}'
    ]
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
    assert arguments == [
        "-m ruff check .",
        f"-m pytest --basetemp {tmp_path / 'pytest-root'}",
        "-m PyInstaller --noconfirm pyinstaller.spec",
    ]


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
        basetemp=None,
    )

    assert result.returncode == exit_code
    assert not list(tmp_path.glob("op-pytest-*"))


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
def test_run_ci_preserves_explicit_basetemp(tmp_path: Path) -> None:
    explicit = tmp_path / "caller-owned-pytest-root"
    result, _ = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=0,
        create_basetemp=True,
        basetemp=explicit,
    )

    assert result.returncode == 0
    assert (explicit / "sentinel.txt").is_file()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
def test_run_ci_normalizes_relative_external_basetemp(tmp_path: Path) -> None:
    expected = tmp_path / "relative-external-pytest-root"
    result, arguments = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=0,
        create_basetemp=True,
        basetemp=r"..\relative-external-pytest-root",
    )

    assert result.returncode == 0
    assert arguments == [f"-m pytest --basetemp {expected}"]
    assert (expected / "sentinel.txt").is_file()


@pytest.mark.skipif(os.name != "nt", reason="run-ci.ps1 只支持 Windows")
@pytest.mark.parametrize("kind", ["relative", "inside", "existing"])
def test_run_ci_rejects_unsafe_explicit_basetemp(tmp_path: Path, kind: str) -> None:
    if kind == "relative":
        base_temp: Path | str = "relative-pytest-root"
    elif kind == "inside":
        base_temp = tmp_path / "repository" / "inside-pytest-root"
    else:
        base_temp = tmp_path / "pre-existing-pytest-root"
        base_temp.mkdir()

    result, arguments = _run_with_fake_python(
        tmp_path,
        "test",
        exit_code=0,
        basetemp=base_temp,
    )

    assert result.returncode != 0
    assert arguments == []
    output = result.stdout + result.stderr
    assert "OLFACTORYPILOT_PYTEST_BASETEMP" in output


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
