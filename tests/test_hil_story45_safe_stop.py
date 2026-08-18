from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "hil_story45_safe_stop.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("hil_story45_safe_stop", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture(scope="module")
def harness():
    return _load_harness()


@pytest.mark.parametrize(
    "scenario",
    [
        "a_zero_failure",
        "a_zero_timeout",
        "stale_a_receipt",
        "late_a_receipt",
    ],
)
def test_invalid_a_receipt_never_routes_selector_and_still_closes(
    harness, tmp_path, scenario
) -> None:
    summary = harness.run_scenario(scenario, tmp_path)
    commands = _read_jsonl(tmp_path / scenario / "commands.jsonl")

    assert summary["safe_stop_status"] == "recovery_required"
    assert summary["safety_assertions"]["selector_command_count"] == 0
    assert not any(
        item.get("command") == "route_selector_safe" for item in commands
    )
    assert summary["final_state"]["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert summary["final_state"]["odor_valves"]["all_closed"] is True
    assert all(summary["final_state"]["owner_handoff"].values())
    assert summary["recovery_reasons"]
    assert summary["verification"] == {"passed": True, "violations": []}


def test_normal_proves_a_receipt_precedes_selector_and_finishes_safe(
    harness, tmp_path
) -> None:
    summary = harness.run_scenario("normal", tmp_path)
    timeline = _read_jsonl(tmp_path / "normal" / "timeline.jsonl")
    a_receipt = next(
        item for item in timeline if item.get("receipt") == "a_zero"
    )
    selector_command = next(
        item
        for item in timeline
        if item.get("command") == "route_selector_safe"
    )

    assert a_receipt["sequence"] < selector_command["sequence"]
    assert a_receipt["monotonic_ns"] <= selector_command["monotonic_ns"]
    assert summary["safe_stop_status"] == "completed"
    assert summary["result"] == "success"
    assert summary["final_state"]["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert summary["final_state"]["odor_valves"]["count"] == 20
    assert summary["final_state"]["odor_valves"]["all_closed"] is True
    assert summary["final_state"]["selector"]["software_evidence"] == "compensation"
    assert all(summary["final_state"]["owner_handoff"].values())
    assert summary["verification"] == {"passed": True, "violations": []}

    ordered_events = [
        next(item for item in timeline if item["event"] == "fence"),
        next(item for item in timeline if item.get("command") == "zero_a_for_safe_stop"),
        a_receipt,
        selector_command,
        next(item for item in timeline if item.get("receipt") == "selector"),
        next(item for item in timeline if item.get("command") == "close_odors_for_safe_stop"),
        next(item for item in timeline if item.get("receipt") == "odors_closed"),
        next(item for item in timeline if item.get("command") == "zero_all_for_safe_stop"),
        next(item for item in timeline if item.get("receipt") == "all_flows_zero"),
        next(item for item in timeline if item.get("owner") == "maintenance"),
        next(item for item in timeline if item.get("owner") == "do"),
        next(item for item in timeline if item.get("owner") == "flow_lease"),
        next(item for item in timeline if item.get("command") == "stop_heaters"),
        next(item for item in timeline if item.get("owner") == "ai"),
        next(item for item in timeline if item.get("owner") == "serial"),
    ]
    assert [item["sequence"] for item in ordered_events] == sorted(
        item["sequence"] for item in ordered_events
    )
    assert a_receipt["identity"] == selector_command["identity"]
    assert a_receipt["command_id"] == "mock-a-zero-1"
    selector_receipt = ordered_events[4]
    assert selector_receipt["command_id"] == selector_command["command_id"]
    assert selector_receipt["identity"] == selector_command["identity"]


@pytest.mark.parametrize(
    "scenario", ["stale_selector_receipt", "late_selector_receipt"]
)
def test_invalid_selector_receipt_is_unknown_without_retry(
    harness, tmp_path, scenario
) -> None:
    summary = harness.run_scenario(scenario, tmp_path)

    assert summary["safe_stop_status"] == "recovery_required"
    assert summary["safety_assertions"]["selector_command_count"] == 1
    assert summary["final_state"]["selector"]["software_evidence"] == "unknown"
    assert summary["final_state"]["selector"]["mechanical_evidence"] is False
    assert summary["final_state"]["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert summary["final_state"]["odor_valves"]["all_closed"] is True
    assert all(summary["final_state"]["owner_handoff"].values())
    assert summary["recovery_reasons"]
    assert summary["verification"] == {"passed": True, "violations": []}


def test_selector_uncertainty_preserves_unknown_and_continues_cleanup(
    harness, tmp_path
) -> None:
    summary = harness.run_scenario("selector_uncertain", tmp_path)

    assert summary["safe_stop_status"] == "recovery_required"
    assert summary["safety_assertions"]["selector_command_count"] == 1
    assert summary["final_state"]["selector"] == {
        "software_evidence": "unknown",
        "simulated_observation": "unknown",
        "mechanical_evidence": False,
    }
    assert summary["final_state"]["odor_valves"]["all_closed"] is True
    assert summary["final_state"]["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert all(summary["final_state"]["owner_handoff"].values())
    assert summary["recovery_reasons"]
    assert summary["verification"] == {"passed": True, "violations": []}


def test_do_handoff_failure_never_crosses_owner_for_fallback(
    harness, tmp_path
) -> None:
    summary = harness.run_scenario("handoff_failure", tmp_path)
    commands = _read_jsonl(tmp_path / "handoff_failure" / "commands.jsonl")

    assert summary["safe_stop_status"] == "recovery_required"
    assert summary["final_state"]["owner_handoff"]["do"] is False
    assert summary["safety_assertions"]["fallback_called"] is False
    assert not any(
        item.get("command") == "fallback_close_all_after_handoff"
        for item in commands
    )
    assert summary["final_state"]["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert summary["final_state"]["odor_valves"]["all_closed"] is True
    assert summary["recovery_reasons"]
    assert summary["verification"] == {"passed": True, "violations": []}


def test_every_scenario_has_fresh_directory_and_complete_evidence(
    harness, tmp_path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPT),
            "--scenario",
            "all",
            "--output-root",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verification_passed"] is True
    expected_files = {
        "run-manifest.json",
        "effective-config.json",
        "timeline.jsonl",
        "commands.jsonl",
        "receipts.jsonl",
        "shutdown-event.json",
        "owner-handoff.json",
        "summary.json",
        "hashes.sha256",
    }
    runtime_ids = set()
    for scenario in harness.SCENARIOS:
        directory = tmp_path / scenario
        assert {path.name for path in directory.iterdir()} == expected_files
        timeline = _read_jsonl(directory / "timeline.jsonl")
        runtime_ids.add(timeline[0]["runtime_id"])
        assert timeline[0]["fresh_runtime"] is True
        manifest = _read_json(directory / "run-manifest.json")
        summary = _read_json(directory / "summary.json")
        shutdown = _read_json(directory / "shutdown-event.json")
        owners = _read_json(directory / "owner-handoff.json")
        assert summary["mode"] == "mock_only"
        assert summary["verification"]["passed"] is True
        assert manifest["module_audit"]["before"]["import_guard_passed"] is True
        assert manifest["module_audit"]["after"]["import_guard_passed"] is True
        assert manifest["module_audit"]["after"]["newly_loaded_prohibited_modules"] == []
        assert manifest["candidate"]["captured_before_evidence_write"] is True
        assert shutdown["safe_stop_status"] == summary["safe_stop_status"]
        assert shutdown["result"] == summary["result"]
        assert shutdown["recovery_required"] == (
            summary["safe_stop_status"] == "recovery_required"
        )
        assert shutdown["selector_safe_confirmed"] == (
            summary["final_state"]["selector"]["software_evidence"]
            == "compensation"
        )
        assert shutdown["valves_closed"] == (
            summary["safe_stop_status"] == "completed"
        )
        assert shutdown["mechanical_evidence"] is False
        assert owners["owners"] == summary["final_state"]["owner_handoff"]
        assert owners["mechanical_evidence"] is False
        hash_entries = {
            line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in (directory / "hashes.sha256").read_text(encoding="ascii").splitlines()
        }
        assert set(hash_entries) == expected_files - {"hashes.sha256"}
        for name, expected_digest in hash_entries.items():
            actual_digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            assert actual_digest == expected_digest
    assert len(runtime_ids) == len(harness.SCENARIOS)


@pytest.mark.parametrize(
    ("scenario", "receipt_name"),
    [("late_a_receipt", "a_zero"), ("late_selector_receipt", "selector")],
)
def test_late_receipt_uses_matching_identity_after_timeout(
    harness, tmp_path, scenario, receipt_name
) -> None:
    summary = harness.run_scenario(scenario, tmp_path)
    timeline = _read_jsonl(tmp_path / scenario / "timeline.jsonl")
    timeout = next(
        item
        for item in timeline
        if item["event"] == "receipt_timeout" and item["receipt"] == receipt_name
    )
    late_receipt = next(
        item
        for item in timeline
        if item.get("receipt") == receipt_name
        and item.get("delivered_after_timeout") is True
    )
    fence = next(item for item in timeline if item["event"] == "fence")

    assert timeout["sequence"] < late_receipt["sequence"]
    assert late_receipt["identity"] == fence["identity"]
    rejection = next(
        item
        for item in timeline
        if item.get("event") == "late_receipt_rejected"
        and item.get("receipt") == receipt_name
    )
    assert rejection["accepted"] is False
    assert summary["safe_stop_status"] == "recovery_required"
    assert summary["verification"]["passed"] is True


def test_cli_has_no_live_option_and_rejects_it(tmp_path) -> None:
    help_result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    rejected = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPT),
            "--live",
            "--output-root",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )

    assert help_result.returncode == 0
    assert "--live" not in help_result.stdout
    assert rejected.returncode == 2
    assert "unrecognized arguments: --live" in rejected.stderr


def test_cli_oracle_returns_nonzero_on_unexpected_result(
    harness, monkeypatch, tmp_path
) -> None:
    clean_audit = {
        "mode": "mock_only",
        "hardware_access_authorized": False,
        "hardware_modules_imported_by_harness": [],
        "host_preloaded_hardware_modules": [],
        "newly_loaded_prohibited_modules": [],
        "import_guard_passed": True,
    }
    monkeypatch.setattr(harness, "_module_audit", lambda: clean_audit)
    monkeypatch.setattr(
        harness,
        "_candidate_snapshot",
        lambda: {
            "commit": "test-commit",
            "tree": "test-tree",
            "working_tree_status_available": True,
        },
    )
    monkeypatch.setattr(
        harness,
        "run_scenario",
        lambda *_args, **_kwargs: {
            "scenario": "normal",
            "safe_stop_status": "recovery_required",
            "verification": {"passed": False, "violations": ["synthetic"]},
        },
    )

    assert (
        harness.main(
            ["--scenario", "normal", "--output-root", str(tmp_path)]
        )
        == 1
    )


def test_unhandled_harness_exception_always_fails_verification(
    harness, monkeypatch, tmp_path
) -> None:
    def fail_shutdown(*_args, **_kwargs):
        raise RuntimeError("synthetic harness boundary failure")

    monkeypatch.setattr(harness.ShutdownService, "shutdown", fail_shutdown)
    summary = harness.run_scenario("normal", tmp_path)

    assert summary["verification"]["passed"] is False
    assert any(
        "未处理 harness 异常" in violation
        for violation in summary["verification"]["violations"]
    )


def test_direct_run_detects_prohibited_module_loaded_during_scenario(
    harness, monkeypatch, tmp_path
) -> None:
    original_shutdown = harness.ShutdownService.shutdown
    injected_name = "serial.story45_synthetic"

    def load_prohibited_module(service, *args, **kwargs):
        sys.modules[injected_name] = types.ModuleType(injected_name)
        return original_shutdown(service, *args, **kwargs)

    monkeypatch.setattr(
        harness.ShutdownService,
        "shutdown",
        load_prohibited_module,
    )
    try:
        summary = harness.run_scenario("normal", tmp_path)
    finally:
        sys.modules.pop(injected_name, None)

    audit = summary["safety_assertions"]["hardware_import_guard_after"]
    assert injected_name in audit["newly_loaded_prohibited_modules"]
    assert summary["verification"]["passed"] is False


@pytest.mark.parametrize(
    "candidate",
    [
        {
            "commit": "UNAVAILABLE",
            "tree": "tree",
            "working_tree_status_available": True,
        },
        {
            "commit": "commit",
            "tree": "UNAVAILABLE",
            "working_tree_status_available": True,
        },
        {
            "commit": "commit",
            "tree": "tree",
            "working_tree_status_available": False,
        },
    ],
)
def test_git_provenance_unavailable_fails_before_scenario_directory(
    harness, tmp_path, candidate
) -> None:
    with pytest.raises(RuntimeError, match="Git provenance 不可用"):
        harness.run_scenario("normal", tmp_path, candidate=candidate)

    assert not (tmp_path / "normal").exists()


def test_candidate_configuration_strictly_parses_selector_and_odor_map(
    harness,
) -> None:
    evidence, selector, odor_targets = harness._candidate_configuration("normal")

    assert "NOT a safely merged effective production configuration" in evidence[
        "configuration_role"
    ]
    assert selector.target == "Dev2/P1.0"
    assert selector.safe_route.value == "compensation"
    assert selector.safe_level is False
    assert selector.odor_level is True
    assert set(evidence["odor_valves"]) == {str(index) for index in range(1, 21)}
    assert len(odor_targets) == len(set(odor_targets)) == 20
    assert selector.target not in odor_targets


def test_unrelated_recovery_reason_cannot_satisfy_scenario_oracle(
    harness, tmp_path
) -> None:
    summary = harness.run_scenario("a_zero_failure", tmp_path)
    timeline = _read_jsonl(tmp_path / "a_zero_failure" / "timeline.jsonl")
    event = _read_json(tmp_path / "a_zero_failure" / "shutdown-event.json")
    summary["recovery_reasons"] = ["unrelated selector failure"]

    verification = harness._verify_summary(
        "a_zero_failure",
        summary,
        timeline,
        event=event,
        config_matches=True,
    )

    assert verification["passed"] is False
    assert any(
        "专属 recovery reason" in violation
        for violation in verification["violations"]
    )


def test_clean_subprocess_blocks_every_hardware_import(tmp_path) -> None:
    import_guard = """
import importlib.abc
import runpy
import sys

blocked = (
    "nidaqmx",
    "serial",
    "app.services.real_hal",
    "app.services.hardware_check_service",
)

class HardwareImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("hardware import attempted: " + fullname)
        return None

script, output_root = sys.argv[1:]
sys.meta_path.insert(0, HardwareImportBlocker())
sys.argv = [script, "--scenario", "all", "--output-root", output_root]
runpy.run_path(script, run_name="__main__")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            import_guard,
            str(SCRIPT),
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    for scenario in _load_harness().SCENARIOS:
        manifest = _read_json(tmp_path / scenario / "run-manifest.json")
        assert manifest["module_audit"]["before"]["import_guard_passed"] is True
        assert manifest["module_audit"]["after"]["import_guard_passed"] is True
        assert manifest["module_audit"]["after"]["hardware_modules_imported_by_harness"] == []
        commands = _read_jsonl(tmp_path / scenario / "commands.jsonl")
        assert commands
        assert all(item["execution"] == "mock_only" for item in commands)
        assert all(item["hardware_write"] is False for item in commands)
