from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import subprocess
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROHIBITED_MODULE_PREFIXES = (
    "nidaqmx",
    "serial",
    "app.services.real_hal",
    "app.services.hardware_check_service",
)
_MODULES_BEFORE_HARNESS_IMPORTS = set(sys.modules)

# Importing app.services normally executes app/services/__init__.py, whose public
# convenience imports include RealHAL and HardwareCheckService.  This harness is
# deliberately narrower: install only the package namespace, then load the one
# production service under test.  No hardware package is imported or probed.
_BOOTSTRAPPED_SERVICES_PACKAGE = "app.services" not in sys.modules
if _BOOTSTRAPPED_SERVICES_PACKAGE:
    services_package = types.ModuleType("app.services")
    services_package.__path__ = [str(PROJECT_ROOT / "app" / "services")]
    services_package.__package__ = "app.services"
    sys.modules["app.services"] = services_package

from app.models import (  # noqa: E402
    AppState,
    AZeroReceipt,
    SafeStopIdentity,
    SelectorConfig,
    SelectorReceipt,
    SelectorRoute,
    normalize_digital_target,
)

ShutdownService = importlib.import_module(
    "app.services.shutdown_service"
).ShutdownService
if _BOOTSTRAPPED_SERVICES_PACKAGE:
    # Keep the loaded production class, but do not poison a host process that
    # later needs the project's normal convenience package (for example pytest
    # global fixtures).  A standalone CLI never imports that package again.
    sys.modules.pop("app.services", None)
_HARDWARE_MODULES_IMPORTED_BY_HARNESS = sorted(
    name
    for name in set(sys.modules) - _MODULES_BEFORE_HARNESS_IMPORTS
    if any(
        name == prefix or name.startswith(f"{prefix}.")
        for prefix in PROHIBITED_MODULE_PREFIXES
    )
)


SCENARIOS = (
    "normal",
    "a_zero_failure",
    "a_zero_timeout",
    "stale_a_receipt",
    "late_a_receipt",
    "stale_selector_receipt",
    "late_selector_receipt",
    "selector_uncertain",
    "handoff_failure",
)
class EvidenceRecorder:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.timeline: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self._sequence = 0

    def record(self, event: str, **details: Any) -> dict[str, Any]:
        self._sequence += 1
        entry = {
            "sequence": self._sequence,
            "wall_time_utc": datetime.now(UTC).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "scenario": self.scenario,
            "event": event,
            **details,
        }
        self.timeline.append(entry)
        return entry

    def command(self, command: str, **details: Any) -> None:
        entry = self.record(
            "command",
            command=command,
            execution="mock_only",
            hardware_write=False,
            **details,
        )
        self.commands.append(entry)

    def receipt(self, receipt: str, **details: Any) -> None:
        entry = self.record(
            "receipt",
            receipt=receipt,
            evidence_kind="software_mock_receipt",
            mechanical_evidence=False,
            **details,
        )
        self.receipts.append(entry)


@dataclass
class RuntimeState:
    flows: dict[str, float]
    odors: dict[str, bool]
    selector_simulated_observation: str
    owner_handoff: dict[str, bool]

    @classmethod
    def fresh(cls, odor_targets: tuple[str, ...]) -> RuntimeState:
        return cls(
            flows={"A": 1500.0, "B": 0.0, "C": 0.0},
            odors={target: True for target in odor_targets},
            selector_simulated_observation=SelectorRoute.ODOR.value,
            owner_handoff={
                "maintenance": False,
                "do": False,
                "flow_lease": False,
                "ai": False,
                "serial": False,
            },
        )


class FakeHardwareOwner:
    def __init__(self, recorder: EvidenceRecorder, runtime: RuntimeState) -> None:
        self.recorder = recorder
        self.runtime = runtime

    def stop_heaters(self) -> bool:
        self.recorder.command("stop_heaters", owner="fake_hardware_owner")
        return True

    def flush_logs(self) -> None:
        self.recorder.record("logs_flushed", owner="fake_hardware_owner")

    def release_ai_resources(self) -> bool:
        self.runtime.owner_handoff["ai"] = True
        self.recorder.record("owner_handoff", owner="ai", success=True)
        return True


class FakeFlowOwner:
    def __init__(
        self,
        scenario: str,
        recorder: EvidenceRecorder,
        runtime: RuntimeState,
    ) -> None:
        self.scenario = scenario
        self.recorder = recorder
        self.runtime = runtime
        self._late_a_receipt: AZeroReceipt | None = None

    def zero_a_for_safe_stop(
        self, identity: SafeStopIdentity, timeout_ms: int
    ) -> AZeroReceipt | None:
        self.recorder.command(
            "zero_a_for_safe_stop",
            owner="fake_flow_owner",
            command_id="mock-a-zero-1",
            target="A",
            requested_value=0.0,
            timeout_ms=timeout_ms,
            identity=_identity_dict(identity),
        )
        if self.scenario in {"a_zero_timeout", "late_a_receipt"}:
            self.recorder.record(
                "receipt_timeout",
                receipt="a_zero",
                timeout_ms=timeout_ms,
            )
            if self.scenario == "late_a_receipt":
                self.runtime.flows["A"] = 0.0
                self._late_a_receipt = AZeroReceipt(
                    command_id="mock-a-zero-1",
                    identity=identity,
                    success=True,
                    confirmed_a=0.0,
                )
            return None

        success = self.scenario != "a_zero_failure"
        stale = self.scenario == "stale_a_receipt"
        if success:
            self.runtime.flows["A"] = 0.0
        receipt = AZeroReceipt(
            command_id="mock-a-zero-1",
            identity=identity,
            success=success,
            confirmed_a=0.0 if success else 1500.0,
            stale=stale,
            message="injected A-zero failure" if not success else "",
        )
        self.recorder.receipt(
            "a_zero",
            command_id=receipt.command_id,
            identity=_identity_dict(receipt.identity),
            success=receipt.success,
            stale=receipt.stale,
            confirmed_a=receipt.confirmed_a,
            source=receipt.source,
            mode=receipt.mode,
            injected_fault=self.scenario if self.scenario != "normal" else None,
        )
        return receipt

    def deliver_late_a_receipt(self, plan) -> bool:
        receipt = self._late_a_receipt
        if receipt is None:
            return False
        self.recorder.receipt(
            "a_zero",
            command_id=receipt.command_id,
            identity=_identity_dict(receipt.identity),
            success=receipt.success,
            stale=receipt.stale,
            confirmed_a=receipt.confirmed_a,
            source=receipt.source,
            mode=receipt.mode,
            delivered_after_timeout=True,
            injected_fault="late_a_receipt",
        )
        accepted = plan.accept_a_zero(receipt)
        self.recorder.record(
            "late_receipt_rejected",
            receipt="a_zero",
            accepted=accepted,
            safe_stop_status=plan.status.value,
        )
        return not accepted

    def zero_all_for_safe_stop(
        self, identity: SafeStopIdentity, timeout_ms: int
    ) -> bool:
        self.recorder.command(
            "zero_all_for_safe_stop",
            owner="fake_flow_owner",
            targets=["A", "B", "C"],
            requested_value=0.0,
            timeout_ms=timeout_ms,
            identity=_identity_dict(identity),
        )
        self.runtime.flows.update(A=0.0, B=0.0, C=0.0)
        self.recorder.receipt(
            "all_flows_zero",
            success=True,
            confirmed_flows=dict(self.runtime.flows),
        )
        return True

    def release_lease_for_safe_stop(self, identity, evidence=None) -> bool:
        success = bool(evidence is not None and evidence.complete)
        self.runtime.owner_handoff["flow_lease"] = success
        self.recorder.record(
            "owner_handoff",
            owner="flow_lease",
            success=success,
            identity=_identity_dict(identity),
        )
        return success

    def shutdown(self, timeout_ms: int) -> bool:
        self.runtime.owner_handoff["serial"] = True
        self.recorder.record(
            "owner_handoff",
            owner="serial",
            success=True,
            timeout_ms=timeout_ms,
        )
        return True


class FakeActuationOwner:
    def __init__(
        self,
        scenario: str,
        recorder: EvidenceRecorder,
        runtime: RuntimeState,
        selector: SelectorConfig,
    ) -> None:
        self.scenario = scenario
        self.recorder = recorder
        self.runtime = runtime
        self.selector = selector
        self.fallback_called = False

    def fence_for_safe_stop(self, **values: Any) -> SafeStopIdentity:
        identity = SafeStopIdentity(
            values["operation_id"],
            values["generation"],
            execution_epoch=1,
        )
        self.recorder.record(
            "fence",
            owner="fake_actuation_owner",
            identity=_identity_dict(identity),
            timeout_ms=values["timeout_ms"],
        )
        return identity

    def route_selector_safe(
        self, plan, timeout_ms: int
    ) -> SelectorReceipt | None:
        command_id = "mock-selector-safe-1"
        plan.expect_selector(command_id)
        self.recorder.command(
            "route_selector_safe",
            owner="fake_actuation_owner",
            command_id=command_id,
            target=self.selector.target,
            requested_route=self.selector.safe_route.value,
            timeout_ms=timeout_ms,
            identity=_identity_dict(plan.identity),
        )

        success = self.scenario != "selector_uncertain"
        stale = self.scenario == "stale_selector_receipt"
        route = (
            SelectorRoute.UNKNOWN
            if self.scenario == "selector_uncertain"
            else SelectorRoute.COMPENSATION
        )
        if self.scenario == "late_selector_receipt":
            plan.timeout("selector 安全路线 receipt")
            self.recorder.record(
                "receipt_timeout",
                receipt="selector",
                timeout_ms=timeout_ms,
            )
        if self.scenario in {
            "normal",
            "stale_selector_receipt",
            "late_selector_receipt",
            "handoff_failure",
        }:
            self.runtime.selector_simulated_observation = (
                SelectorRoute.COMPENSATION.value
            )
        else:
            self.runtime.selector_simulated_observation = SelectorRoute.UNKNOWN.value
        receipt = SelectorReceipt(
            command_id=command_id,
            identity=plan.identity,
            target=self.selector.target,
            route=route,
            success=success,
            stale=stale,
            message="injected selector uncertainty" if not success else "",
        )
        self.recorder.receipt(
            "selector",
            command_id=receipt.command_id,
            identity=_identity_dict(receipt.identity),
            target=receipt.target,
            route=receipt.route.value,
            success=receipt.success,
            stale=receipt.stale,
            delivered_after_timeout=self.scenario == "late_selector_receipt",
            injected_fault=self.scenario if self.scenario != "normal" else None,
        )
        if self.scenario == "late_selector_receipt":
            accepted = plan.accept_selector(receipt)
            self.recorder.record(
                "late_receipt_rejected",
                receipt="selector",
                accepted=accepted,
                safe_stop_status=plan.status.value,
            )
            return None
        return receipt

    def close_odors_for_safe_stop(
        self, identity: SafeStopIdentity, timeout_ms: int
    ) -> bool:
        self.recorder.command(
            "close_odors_for_safe_stop",
            owner="fake_actuation_owner",
            targets=list(self.runtime.odors),
            requested_level=False,
            timeout_ms=timeout_ms,
            identity=_identity_dict(identity),
        )
        for target in self.runtime.odors:
            self.runtime.odors[target] = False
        self.recorder.receipt(
            "odors_closed",
            success=True,
            closed_count=len(self.runtime.odors),
        )
        return True

    def handoff_maintenance_for_safe_stop(self) -> bool:
        self.runtime.owner_handoff["maintenance"] = True
        self.recorder.record(
            "owner_handoff",
            owner="maintenance",
            success=True,
        )
        return True

    def shutdown(self, timeout_ms: int) -> bool:
        success = self.scenario != "handoff_failure"
        self.runtime.owner_handoff["do"] = success
        self.recorder.record(
            "owner_handoff",
            owner="do",
            success=success,
            timeout_ms=timeout_ms,
            injected_fault="handoff_failure" if not success else None,
        )
        return success

    def fallback_close_all_after_handoff(self) -> bool:
        self.fallback_called = True
        self.recorder.command(
            "fallback_close_all_after_handoff",
            owner="fake_actuation_owner",
        )
        return True


def _identity_dict(identity: SafeStopIdentity) -> dict[str, Any]:
    return {
        "operation_id": identity.operation_id,
        "generation": identity.generation,
        "execution_epoch": identity.execution_epoch,
    }


def _state() -> AppState:
    state = AppState(low_flow_threshold=0.2)
    state.hardware_ready = True
    state.telemetry.connected = True
    return state


def _git_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "UNAVAILABLE"
    return completed.stdout.rstrip()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_snapshot() -> dict[str, Any]:
    status = _git_output("status", "--porcelain=v1", "--untracked-files=all")
    commit = _git_output("rev-parse", "HEAD")
    tree = _git_output("rev-parse", "HEAD^{tree}")
    relevant_paths = (
        Path("app/models/safe_stop.py"),
        Path("app/services/shutdown_service.py"),
        Path("config/default_config.json"),
        Path("scripts/hil_story45_safe_stop.py"),
    )
    source_hashes = {
        path.as_posix(): _sha256_file(PROJECT_ROOT / path)
        for path in relevant_paths
    }
    bundle = "".join(
        f"{path}\0{digest}\n" for path, digest in sorted(source_hashes.items())
    ).encode()
    dirty_diff = _git_output("diff", "--binary", "HEAD", "--")
    return {
        "captured_before_evidence_write": True,
        "commit": commit,
        "tree": tree,
        "branch": _git_output("branch", "--show-current"),
        "working_tree_clean": status == "",
        "working_tree_status_available": status != "UNAVAILABLE",
        "working_tree_status": status.splitlines(),
        "tracked_dirty_diff_sha256": hashlib.sha256(dirty_diff.encode()).hexdigest(),
        "executed_source_sha256": source_hashes,
        "executed_source_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "python": sys.version,
    }


def _validate_candidate_snapshot(candidate: dict[str, Any]) -> None:
    unavailable = []
    if candidate.get("commit") == "UNAVAILABLE":
        unavailable.append("commit")
    if candidate.get("tree") == "UNAVAILABLE":
        unavailable.append("tree")
    if candidate.get("working_tree_status_available") is not True:
        unavailable.append("working_tree_status")
    if unavailable:
        raise RuntimeError(
            "Git provenance 不可用，拒绝执行：" + ", ".join(unavailable)
        )


def _candidate_configuration(
    scenario: str,
) -> tuple[dict[str, Any], SelectorConfig, tuple[str, ...]]:
    config_path = PROJECT_ROOT / "config" / "default_config.json"
    production = json.loads(config_path.read_text(encoding="utf-8"))
    valve_mapping = production["valve_mapping"]
    production_odors = valve_mapping["variants"]["20-channel"]
    production_selector = valve_mapping["selector"]
    expected_keys = {str(index) for index in range(1, 21)}
    if set(production_odors) != expected_keys:
        raise ValueError("default_config 20-channel 必须精确包含气味阀 1..20。")
    if type(production_selector.get("safe_level")) is not bool or type(
        production_selector.get("odor_level")
    ) is not bool:
        raise ValueError("default_config selector 电平必须为 JSON boolean。")
    selector = SelectorConfig(
        target=str(production_selector["target"]),
        safe_route=SelectorRoute(str(production_selector["safe_route"])),
        safe_level=production_selector["safe_level"],
        odor_level=production_selector["odor_level"],
    )
    odor_targets = tuple(
        str(production_odors[str(index)]) for index in range(1, 21)
    )
    normalized_odors = tuple(
        normalize_digital_target(target) for target in odor_targets
    )
    if len(set(normalized_odors)) != 20:
        raise ValueError("default_config 20-channel 气味阀目标必须唯一。")
    if normalize_digital_target(selector.target) in normalized_odors:
        raise ValueError("selector 不得出现在气味阀 1..20 映射中。")

    evidence = {
        "mode": "mock_only",
        "configuration_role": (
            "default_config + documented local candidate; "
            "NOT a safely merged effective production configuration"
        ),
        "scenario": scenario,
        "fault_injection": None if scenario == "normal" else scenario,
        "initial_simulated_state": {
            "flows_sccm": {"A": 1500.0, "B": 0.0, "C": 0.0},
            "all_odor_valves_open": True,
            "selector_route": "odor",
        },
        "shutdown": {
            "retry_limit": 0,
            "a_zero_and_owner_timeout_ms": 2000,
            "selector_and_odor_timeout_ms": 500,
        },
        "selector": production_selector,
        "odor_valves": production_odors,
        "mapping_validation_passed": True,
        "documented_local_candidate_not_used": {
            "port": "COM6",
            "baud_rate": production["baud_rate"],
            "addresses": production["alicat_unit_ids"],
            "flow_unit": "sccm",
            "configured_unit_code": production["alicat_flow_unit"],
            "flow_field": production["alicat_flow_field"],
            "timeout_s": production["alicat_timeout_s"],
            "setpoint_tolerance": production["alicat_setpoint_tolerance"],
            "verify_delay_s": production["alicat_setpoint_verify_delay_s"],
            "verify_retries": production["alicat_setpoint_verify_retries"],
            "setpoint_scale": production["alicat_setpoint_scale"],
            "readback_scale": production["alicat_readback_scale"],
        },
        "source": {
            "path": "config/default_config.json",
            "sha256": _sha256_file(config_path),
            "notice": "仅记录候选参数；本离线演练不访问这些硬件。",
        },
    }
    return evidence, selector, odor_targets


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        for value in values
    )
    path.write_text(text, encoding="utf-8")


def _write_hashes(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name == "hashes.sha256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    (directory / "hashes.sha256").write_text("".join(lines), encoding="ascii")


def _prohibited_modules_loaded() -> set[str]:
    return {
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in PROHIBITED_MODULE_PREFIXES
        )
    }


def _module_audit(*, before: set[str] | None = None) -> dict[str, Any]:
    loaded = _prohibited_modules_loaded()
    newly_loaded = sorted(loaded - (before or set()))
    return {
        "mode": "mock_only",
        "hardware_access_authorized": False,
        "hardware_modules_imported_by_harness": _HARDWARE_MODULES_IMPORTED_BY_HARNESS,
        "host_preloaded_hardware_modules": sorted(loaded),
        "newly_loaded_prohibited_modules": newly_loaded,
        "import_guard_passed": (
            not _HARDWARE_MODULES_IMPORTED_BY_HARNESS and not newly_loaded
        ),
    }


def run_scenario(
    scenario: str,
    output_root: Path,
    *,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"未知场景：{scenario}")
    candidate = candidate or _candidate_snapshot()
    _validate_candidate_snapshot(candidate)
    module_audit_baseline = _prohibited_modules_loaded()
    module_audit_before = _module_audit(before=module_audit_baseline)
    candidate_config, selector, odor_targets = _candidate_configuration(scenario)
    scenario_dir = output_root / scenario
    scenario_dir.mkdir(parents=True, exist_ok=False)

    recorder = EvidenceRecorder(scenario)
    runtime = RuntimeState.fresh(odor_targets)
    hardware = FakeHardwareOwner(recorder, runtime)
    actuation = FakeActuationOwner(scenario, recorder, runtime, selector)
    flow = FakeFlowOwner(scenario, recorder, runtime)
    state = _state()
    shutdown_path = scenario_dir / "shutdown-event.json"
    service = ShutdownService(
        state=state,
        worker=hardware,
        actuation_worker=actuation,
        flow_worker=flow,
        selector=selector,
        retry_limit=0,
        record_path=shutdown_path,
        actuation_timeout_ms=2000,
        emergency_close_timeout_ms=500,
    )

    recorder.record(
        "runtime_started",
        runtime_id=f"{scenario}-{time.monotonic_ns()}",
        fresh_runtime=True,
        mode="mock_only",
    )
    try:
        event = service.shutdown(
            source="story-4.5-offline-harness", reason=scenario
        )
    except Exception as exc:  # pragma: no cover - defensive evidence boundary
        recorder.record(
            "unhandled_harness_exception",
            exception_type=type(exc).__name__,
            message=str(exc),
        )
        event = {
            "source": "story-4.5-offline-harness",
            "reason": scenario,
            "result": "recovery_required",
            "safe_stop_status": "recovery_required",
            "a_zero_confirmed": False,
            "selector_safe_confirmed": False,
            "valves_closed": False,
            "heaters_off": False,
            "recovery_required": True,
            "error": f"离线 harness 异常：{type(exc).__name__}: {exc}",
        }

    plan = service._last_safe_stop_plan
    if scenario == "late_a_receipt" and plan is not None:
        flow.deliver_late_a_receipt(plan)
    recorder.record(
        "runtime_finished",
        result=event["result"],
        safe_stop_status=event["safe_stop_status"],
    )

    selector_evidence = (
        SelectorRoute.COMPENSATION.value
        if plan is not None
        and plan.selector_confirmed
        and scenario != "late_selector_receipt"
        else SelectorRoute.UNKNOWN.value
    )
    recovery_reasons = []
    if event.get("error"):
        recovery_reasons.append(event["error"])
    raw_selector_safe_confirmed = bool(event.get("selector_safe_confirmed"))
    event["raw_service_selector_safe_confirmed"] = raw_selector_safe_confirmed
    event["selector_safe_confirmed"] = (
        selector_evidence == SelectorRoute.COMPENSATION.value
    )
    module_audit_after = _module_audit(before=module_audit_baseline)
    summary = {
        "schema_version": 1,
        "scenario": scenario,
        "mode": "mock_only",
        "result": event["result"],
        "safe_stop_status": event["safe_stop_status"],
        "recovery_reasons": recovery_reasons,
        "final_state": {
            "flows": dict(runtime.flows),
            "odor_valves": {
                "count": len(runtime.odors),
                "all_closed": all(not value for value in runtime.odors.values()),
                "states": dict(runtime.odors),
            },
            "selector": {
                "software_evidence": selector_evidence,
                "simulated_observation": runtime.selector_simulated_observation,
                "mechanical_evidence": False,
            },
            "owner_handoff": dict(runtime.owner_handoff),
        },
        "safety_assertions": {
            "a_zero_receipt_before_selector": _a_receipt_before_selector(
                recorder.timeline,
                plan_a_zero_confirmed=(
                    plan is not None and plan.a_zero_confirmed
                ),
            ),
            "selector_command_count": sum(
                item.get("command") == "route_selector_safe"
                for item in recorder.commands
            ),
            "fallback_called": actuation.fallback_called,
            "hardware_import_guard_before": module_audit_before,
            "hardware_import_guard_after": module_audit_after,
        },
        "evidence_notice": (
            "所有观察均为软件模拟；mock receipt 不构成 DAQmx、机械阀或气路证据。"
        ),
    }
    manifest = {
        "schema_version": 1,
        "scenario": scenario,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "candidate": candidate,
        "parameters": candidate_config,
        "module_audit": {
            "before": module_audit_before,
            "after": module_audit_after,
        },
    }

    summary["verification"] = _verify_summary(
        scenario,
        summary,
        recorder.timeline,
        event=event,
        config_matches=candidate_config["mapping_validation_passed"],
    )

    _write_json(scenario_dir / "run-manifest.json", manifest)
    _write_json(scenario_dir / "effective-config.json", candidate_config)
    _write_jsonl(scenario_dir / "timeline.jsonl", recorder.timeline)
    _write_jsonl(scenario_dir / "commands.jsonl", recorder.commands)
    _write_jsonl(scenario_dir / "receipts.jsonl", recorder.receipts)
    shutdown_evidence = {
        **event,
        "evidence_kind": "software_mock_shutdown_event",
        "mechanical_evidence": False,
    }
    _write_json(shutdown_path, shutdown_evidence)
    _write_json(
        scenario_dir / "owner-handoff.json",
        {
            "mode": "mock_only",
            "evidence_kind": "software_mock_owner_state",
            "mechanical_evidence": False,
            "owners": runtime.owner_handoff,
        },
    )
    _write_json(scenario_dir / "summary.json", summary)
    _write_hashes(scenario_dir)
    return summary


def _a_receipt_before_selector(
    timeline: list[dict[str, Any]], *, plan_a_zero_confirmed: bool
) -> bool | None:
    selector = next(
        (
            item
            for item in timeline
            if item.get("command") == "route_selector_safe"
        ),
        None,
    )
    if selector is None:
        return None
    a_receipt = next(
        (
            item
            for item in timeline
            if item.get("receipt") == "a_zero"
            and item.get("success") is True
            and item.get("stale") is False
            and item.get("confirmed_a") == 0.0
            and item.get("source") == "safety:safe-stop"
            and item.get("mode") == "safe_stop_a_zero"
        ),
        None,
    )
    return bool(
        plan_a_zero_confirmed
        and a_receipt is not None
        and a_receipt.get("command_id") == "mock-a-zero-1"
        and a_receipt.get("identity") == selector.get("identity")
        and a_receipt["sequence"] < selector["sequence"]
        and a_receipt["monotonic_ns"] <= selector["monotonic_ns"]
    )


def _ordered_timeline(
    timeline: list[dict[str, Any]],
    predicates: tuple[Callable[[dict[str, Any]], bool], ...],
) -> bool:
    sequences: list[int] = []
    for predicate in predicates:
        match = next((item for item in timeline if predicate(item)), None)
        if match is None:
            return False
        sequences.append(int(match["sequence"]))
    return sequences == sorted(sequences) and len(sequences) == len(set(sequences))


def _full_shutdown_order_verified(
    scenario: str, timeline: list[dict[str, Any]]
) -> bool:
    def is_event(name: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: item.get("event") == name

    def is_command(name: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: item.get("command") == name

    def is_receipt(name: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: item.get("receipt") == name

    def is_owner(name: str) -> Callable[[dict[str, Any]], bool]:
        return lambda item: item.get("owner") == name

    common_tail = (
        is_command("close_odors_for_safe_stop"),
        is_command("zero_all_for_safe_stop"),
        is_owner("maintenance"),
        is_owner("do"),
        is_owner("flow_lease"),
        is_command("stop_heaters"),
        is_event("logs_flushed"),
        is_owner("ai"),
        is_owner("serial"),
    )
    a_faults = {
        "a_zero_failure",
        "a_zero_timeout",
        "stale_a_receipt",
        "late_a_receipt",
    }
    if scenario in a_faults:
        a_terminal = (
            (lambda item: item.get("receipt") == "a_zero")
            if scenario in {"a_zero_failure", "stale_a_receipt"}
            else (
                lambda item: item.get("event") == "receipt_timeout"
                and item.get("receipt") == "a_zero"
            )
        )
        return _ordered_timeline(
            timeline,
            (
                is_event("fence"),
                is_command("zero_a_for_safe_stop"),
                a_terminal,
                *common_tail,
            ),
        )
    return _ordered_timeline(
        timeline,
        (
            is_event("fence"),
            is_command("zero_a_for_safe_stop"),
            is_receipt("a_zero"),
            is_command("route_selector_safe"),
            is_receipt("selector"),
            *common_tail,
        ),
    )


def _scenario_injection_verified(
    scenario: str, timeline: list[dict[str, Any]]
) -> bool:
    def matching(**values: Any) -> list[dict[str, Any]]:
        return [
            item
            for item in timeline
            if all(item.get(key) == value for key, value in values.items())
        ]

    if scenario == "normal":
        return not any(item.get("injected_fault") for item in timeline)
    if scenario == "a_zero_failure":
        return bool(
            matching(
                receipt="a_zero",
                injected_fault=scenario,
                success=False,
                confirmed_a=1500.0,
            )
        )
    if scenario == "a_zero_timeout":
        return bool(matching(event="receipt_timeout", receipt="a_zero")) and not matching(
            event="receipt", receipt="a_zero"
        )
    if scenario == "stale_a_receipt":
        return bool(
            matching(receipt="a_zero", injected_fault=scenario, stale=True)
        )
    if scenario == "late_a_receipt":
        return bool(
            matching(event="receipt_timeout", receipt="a_zero")
            and matching(
                receipt="a_zero",
                injected_fault=scenario,
                delivered_after_timeout=True,
            )
            and matching(
                event="late_receipt_rejected",
                receipt="a_zero",
                accepted=False,
            )
        )
    if scenario == "stale_selector_receipt":
        return bool(
            matching(receipt="selector", injected_fault=scenario, stale=True)
        )
    if scenario == "late_selector_receipt":
        return bool(
            matching(event="receipt_timeout", receipt="selector")
            and matching(
                receipt="selector",
                injected_fault=scenario,
                delivered_after_timeout=True,
            )
            and matching(
                event="late_receipt_rejected",
                receipt="selector",
                accepted=False,
            )
        )
    if scenario == "selector_uncertain":
        return bool(
            matching(
                receipt="selector",
                injected_fault=scenario,
                success=False,
                route="unknown",
            )
        )
    if scenario == "handoff_failure":
        return bool(
            matching(
                event="owner_handoff",
                owner="do",
                injected_fault=scenario,
                success=False,
            )
        )
    return False


def _verify_summary(
    scenario: str,
    summary: dict[str, Any],
    timeline: list[dict[str, Any]],
    *,
    event: dict[str, Any],
    config_matches: bool,
) -> dict[str, Any]:
    violations: list[str] = []
    final_state = summary["final_state"]
    assertions = summary["safety_assertions"]
    expected_status = "completed" if scenario == "normal" else "recovery_required"
    if summary["safe_stop_status"] != expected_status:
        violations.append(
            f"safe_stop_status 应为 {expected_status}，实际为 {summary['safe_stop_status']}"
        )
    if final_state["flows"] != {"A": 0.0, "B": 0.0, "C": 0.0}:
        violations.append("最终 A/B/C 未全部确认为模拟零值。")
    if not final_state["odor_valves"]["all_closed"]:
        violations.append("气味阀 1-20 未全部模拟关闭。")
    if not config_matches:
        violations.append("default candidate selector/20 路映射校验失败。")
    if any(item.get("event") == "unhandled_harness_exception" for item in timeline):
        violations.append("发生未处理 harness 异常。")
    if not assertions["hardware_import_guard_before"]["import_guard_passed"]:
        violations.append("场景执行前硬件模块 guard 未通过。")
    if not assertions["hardware_import_guard_after"]["import_guard_passed"]:
        violations.append("场景执行期间加载了 prohibited hardware module。")
    if not _scenario_injection_verified(scenario, timeline):
        violations.append(f"{scenario} 的专属 fault injection 证据不匹配。")
    if not _full_shutdown_order_verified(scenario, timeline):
        violations.append("完整 shutdown 动作偏序不满足。")

    a_faults = {
        "a_zero_failure",
        "a_zero_timeout",
        "stale_a_receipt",
        "late_a_receipt",
    }
    selector_faults = {
        "stale_selector_receipt",
        "late_selector_receipt",
        "selector_uncertain",
    }
    selector_count = assertions["selector_command_count"]
    if scenario in a_faults and selector_count != 0:
        violations.append("A receipt 无效后仍发出了 selector 命令。")
    if scenario not in a_faults and selector_count != 1:
        violations.append("selector 命令次数不等于 1。")
    if scenario not in a_faults and assertions["a_zero_receipt_before_selector"] is not True:
        violations.append("selector/handoff 场景缺少有效 A receipt 先行证据。")
    if scenario == "normal":
        if assertions["a_zero_receipt_before_selector"] is not True:
            violations.append("未证明有效 A-zero receipt 严格早于 selector 命令。")
        if final_state["selector"]["software_evidence"] != "compensation":
            violations.append("正常场景 selector 补偿路线证据不完整。")
        if not all(final_state["owner_handoff"].values()):
            violations.append("正常场景 owner handoff 不完整。")
    else:
        if not summary["recovery_reasons"]:
            violations.append("故障场景缺少 RECOVERY_REQUIRED 原因。")
        if scenario != "handoff_failure" and not all(
            final_state["owner_handoff"].values()
        ):
            violations.append("故障收尾未完成全部 owner handoff。")
    if scenario in selector_faults and final_state["selector"]["software_evidence"] != "unknown":
        violations.append("无效 selector receipt 未保持 UNKNOWN 证据状态。")
    if scenario == "handoff_failure":
        expected_owners = {
            "maintenance": True,
            "do": False,
            "flow_lease": False,
            "ai": True,
            "serial": True,
        }
        if final_state["owner_handoff"] != expected_owners:
            violations.append("handoff failure 的 owner 终态不符合预期。")
        if assertions["fallback_called"]:
            violations.append("DO owner 卡住时错误调用了跨线程 fallback。")
    if scenario == "late_a_receipt" and not any(
        item.get("event") == "late_receipt_rejected"
        and item.get("receipt") == "a_zero"
        and item.get("accepted") is False
        for item in timeline
    ):
        violations.append("未证明 deadline 后 A receipt 被拒绝。")
    if scenario == "late_selector_receipt" and not any(
        item.get("event") == "late_receipt_rejected"
        and item.get("receipt") == "selector"
        and item.get("accepted") is False
        for item in timeline
    ):
        violations.append("未证明 deadline 后 selector receipt 被拒绝。")
    expected_reason = {
        "a_zero_failure": "injected A-zero failure",
        "a_zero_timeout": "A 清零 receipt 超时",
        "stale_a_receipt": "A 清零 receipt 已失效",
        "late_a_receipt": "A 清零 receipt 超时",
        "stale_selector_receipt": "selector receipt 已失效",
        "late_selector_receipt": "selector 安全路线 receipt 超时",
        "selector_uncertain": "injected selector uncertainty",
        "handoff_failure": "DO ownership",
    }.get(scenario)
    if expected_reason and expected_reason not in "; ".join(summary["recovery_reasons"]):
        violations.append(f"{scenario} 缺少专属 recovery reason。")

    expected_recovery = summary["safe_stop_status"] == "recovery_required"
    expected_selector_confirmed = (
        final_state["selector"]["software_evidence"] == "compensation"
    )
    expected_valves_closed = (
        summary["safe_stop_status"] == "completed"
        and final_state["odor_valves"]["all_closed"]
        and final_state["flows"] == {"A": 0.0, "B": 0.0, "C": 0.0}
    )
    event_expectations = {
        "result": summary["result"],
        "safe_stop_status": summary["safe_stop_status"],
        "recovery_required": expected_recovery,
        "selector_safe_confirmed": expected_selector_confirmed,
        "valves_closed": expected_valves_closed,
    }
    for field, expected in event_expectations.items():
        if event.get(field) != expected:
            violations.append(
                f"shutdown event {field} 与 summary 不一致：{event.get(field)!r} != {expected!r}"
            )
    return {"passed": not violations, "violations": violations}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Story 4.5 mock-only HIL 离线演练；不会导入、枚举、打开或写入硬件。"
        )
    )
    parser.add_argument(
        "--scenario",
        choices=("all", *SCENARIOS),
        default="all",
        help="运行一个故障场景或全部场景（默认：all）。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="证据输出根目录；每个场景使用独立子目录。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    module_audit = _module_audit()
    if (
        module_audit["host_preloaded_hardware_modules"]
        or not module_audit["import_guard_passed"]
    ):
        parser.error("宿主进程已加载硬件模块；拒绝运行 mock-only CLI。")
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    resolved_output_root = args.output_root.resolve()
    if resolved_output_root.is_relative_to(PROJECT_ROOT):
        parser.error("证据目录必须位于 Git 仓库外，避免污染候选版本记录。")
    existing = [
        str(args.output_root / scenario)
        for scenario in scenarios
        if (args.output_root / scenario).exists()
    ]
    if existing:
        parser.error(f"证据场景目录已存在，拒绝覆盖：{existing}")
    candidate = _candidate_snapshot()
    try:
        _validate_candidate_snapshot(candidate)
    except RuntimeError as exc:
        parser.error(str(exc))
    args.output_root.mkdir(parents=True, exist_ok=True)
    summaries = [
        run_scenario(
            scenario,
            args.output_root,
            candidate=candidate,
        )
        for scenario in scenarios
    ]
    verification_passed = all(
        item["verification"]["passed"] for item in summaries
    )
    result = {
        "mode": "mock_only",
        "scenario_count": len(summaries),
        "output_root": str(resolved_output_root),
        "verification_passed": verification_passed,
        "results": {
            item["scenario"]: item["safe_stop_status"] for item in summaries
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if verification_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
