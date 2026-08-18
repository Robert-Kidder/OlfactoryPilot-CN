from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.main import build_application, load_effective_config
from app.models import CleaningStatus
from app.services.session_file_service import SessionFileService

LIVE_CONFIRMATION = "I_AUTHORIZE_LIVE_CLEANING_HIL"
TERMINAL_STATUSES = {
    CleaningStatus.COMPLETED,
    CleaningStatus.FAILED,
    CleaningStatus.RECOVERY_REQUIRED,
}


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def source_state() -> dict[str, Any]:
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--binary", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    return {
        "head": _git("rev-parse", "HEAD"),
        "worktree_clean": not bool(status),
        "worktree_status": status.splitlines(),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
    }


def validate_source_candidate(
    parser: argparse.ArgumentParser,
    *,
    live: bool,
    exploratory: bool,
    candidate_commit: str,
    state: dict[str, Any],
) -> None:
    if not live or exploratory:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{40}", candidate_commit):
        parser.error("正式 live Gate 必须提供 40 位 --candidate-commit")
    if candidate_commit.lower() != str(state["head"]).lower():
        parser.error("--candidate-commit 必须精确等于当前 HEAD")
    if not state["worktree_clean"]:
        parser.error("正式 live Gate 要求 index/worktree clean；探索性运行请显式加 --exploratory")


def validate_cleaning_config(config: dict[str, Any]) -> dict[str, Any]:
    cleaning = config.get("cleaning")
    if not isinstance(cleaning, dict):
        raise ValueError("缺少 cleaning 配置")
    selected = cleaning.get("selected_channels", cleaning.get("default_channels", []))
    flow = float(cleaning.get("flow_sccm", cleaning.get("default_flow_sccm", 0)))
    approved = float(cleaning.get("max_approved_flow_sccm", 0))
    duration = float(
        cleaning.get("open_duration_s", cleaning.get("default_open_duration_s", 0))
    )
    cycles = int(cleaning.get("cycles", cleaning.get("default_cycles", 0)))
    if not selected:
        raise ValueError("清洗通道集合为空")
    if flow <= 0 or approved <= 0 or flow > approved:
        raise ValueError(f"清洗流量 {flow:g} 超出批准范围 0 < flow <= {approved:g}")
    if duration <= 0 or cycles <= 0:
        raise ValueError("清洗持续时间和轮数必须为正值")
    return {
        "selected_channels": [int(channel) for channel in selected],
        "flow_sccm": flow,
        "max_approved_flow_sccm": approved,
        "open_duration_s": duration,
        "cycles": cycles,
        "gas_label": str(cleaning.get("gas_label", "Air")),
        "fixed_flow_setpoints_sccm": dict(
            cleaning.get("fixed_flow_setpoints_sccm", {"B": 0, "C": 0})
        ),
    }


def hardware_inventory() -> dict[str, Any]:
    from nidaqmx.system import System
    from serial.tools import list_ports

    return {
        "ni_devices": [
            {
                "name": device.name,
                "product_type": device.product_type,
                "serial_num": int(device.serial_num),
            }
            for device in System.local().devices
        ],
        "serial_ports": [
            {"device": port.device, "description": port.description}
            for port in list_ports.comports()
        ],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pump_until(
    app,
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    tick: Callable[[], None] | None = None,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        app.processEvents()
        if tick is not None:
            tick()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    if tick is not None:
        tick()
    return bool(predicate())


def _snapshot_payload(controller) -> dict[str, Any]:
    snapshot = controller._cleaning_runtime
    return {
        "at_monotonic_s": time.monotonic(),
        **asdict(snapshot),
        "display_message": controller._cleaning_display_message,
        "hardware_ready": bool(controller.state.hardware_ready),
        "connected": bool(controller.state.telemetry.connected),
        "safety_state": controller.state.telemetry.safety_state,
        "flow_setpoints_ready": bool(controller.state.flow_setpoints_ready),
        "startup_zero_confirmed": bool(controller._startup_zero_confirmed),
        "device_lease": controller.device_lease.snapshot.kind.value,
    }


def _bundle_evidence(controller, final_result) -> dict[str, Any]:
    if final_result is None or final_result.final_dir is None:
        descriptor = controller._cleaning_descriptor
        staging_dir = (
            None if descriptor is None else Path(descriptor.paths.staging_dir)
        )
        manifest_path = (
            None if staging_dir is None else staging_dir / "manifest.json"
        )
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path is not None and manifest_path.is_file()
            else {}
        )
        log_path = None if descriptor is None else Path(descriptor.paths.log_path)
        records = (
            [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if log_path is not None and log_path.is_file()
            else []
        )
        receipts = [
            record
            for record in records
            if record.get("record_type") == "receipt"
            and record.get("event") == "actuation_receipt"
        ]
        return {
            "complete": False,
            "message": (
                "maintenance finalization 未发布 complete；"
                "失败场景必须保留 staging/recovery 证据"
            ),
            "staging_dir": None if staging_dir is None else str(staging_dir),
            "staging_exists": bool(
                staging_dir is not None and staging_dir.is_dir()
            ),
            "manifest": manifest,
            "record_count": len(records),
            "receipt_record_count": len(receipts),
            "claim_boundary": (
                "failure/recovery-required runs must not publish a complete "
                "maintenance bundle"
            ),
        }
    final_dir = Path(final_result.final_dir)
    service = SessionFileService(master_valve_line=controller.state.master_valve_line)
    validation = service.validate_maintenance_bundle(final_dir)
    manifest_path = final_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {}
    )
    log_name = manifest.get("log_file")
    records: list[dict[str, Any]] = []
    if isinstance(log_name, str) and (final_dir / log_name).is_file():
        records = [
            json.loads(line)
            for line in (final_dir / log_name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    receipts = [
        record
        for record in records
        if record.get("record_type") == "receipt"
        and record.get("event") == "actuation_receipt"
    ]
    open_intervals_ms: list[dict[str, Any]] = []
    for opened in receipts:
        if (
            opened.get("category") != "cleaning"
            or opened.get("action_kind") != "open"
            or opened.get("result") != "success"
            or not isinstance(opened.get("actual_ns"), int)
        ):
            continue
        closed = next(
            (
                record
                for record in receipts
                if record.get("target") == opened.get("target")
                and record.get("action_kind") == "close"
                and record.get("result") == "success"
                and isinstance(record.get("actual_ns"), int)
                and int(record.get("operation_sequence", 0))
                > int(opened.get("operation_sequence", 0))
            ),
            None,
        )
        if closed is not None:
            open_intervals_ms.append(
                {
                    "target": opened.get("target"),
                    "open_sequence": opened.get("operation_sequence"),
                    "close_sequence": closed.get("operation_sequence"),
                    "duration_ms": (
                        int(closed["actual_ns"]) - int(opened["actual_ns"])
                    )
                    / 1_000_000,
                }
            )
    return {
        "complete": bool(final_result.complete),
        "final_dir": str(final_dir),
        "finalization_message": final_result.message,
        "validation_complete": bool(validation.complete),
        "validation_reason": validation.reason,
        "manifest": manifest,
        "record_count": len(records),
        "receipt_record_count": len(receipts),
        "receipt_results": sorted(
            {str(record.get("result", "")) for record in receipts}
        ),
        "open_intervals_ms": open_intervals_ms,
        "claim_boundary": (
            "DAQmx write acknowledgements and Alicat readback validate software/"
            "electronic control only; they do not prove mechanical valve motion."
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Story 4.1 production cleaning owner live HIL gate"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config" / "default_config.json",
    )
    parser.add_argument(
        "--local-config",
        type=Path,
        default=REPO_ROOT / "config" / "local_config.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "logs" / "benchmarks",
    )
    parser.add_argument(
        "--scenario",
        choices=("baseline", "stop", "low-flow", "disconnect"),
        default="baseline",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--candidate-commit", default="")
    parser.add_argument("--exploratory", action="store_true")
    parser.add_argument("--trigger-after-s", type=float, default=1.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    parser.add_argument("--timeout-s", type=float, default=360.0)
    args = parser.parse_args(argv)
    if args.live and args.confirm != LIVE_CONFIRMATION:
        parser.error(f"live hardware requires --confirm {LIVE_CONFIRMATION}")
    if args.trigger_after_s < 0 or args.startup_timeout_s <= 0 or args.timeout_s <= 0:
        parser.error("timeouts must be positive and trigger-after-s must be non-negative")
    state = source_state()
    validate_source_candidate(
        parser,
        live=args.live,
        exploratory=args.exploratory,
        candidate_commit=args.candidate_commit,
        state=state,
    )
    args.source_state = state
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_effective_config(args.config, args.local_config)
    recipe = validate_cleaning_config(config)
    if args.live:
        if str(config.get("hal_mode", "")).lower() != "real":
            raise RuntimeError("live HIL 要求 effective config 的 hal_mode=real")
        if recipe["flow_sccm"] > 1500:
            raise RuntimeError("Story 4.1 当前禁止超过 1500 ml/min 批准上限")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    mode = "live" if args.live else "simulation"
    run_dir = args.output_root / f"story-4-1-{stamp}-{args.scenario}-{mode}"
    run_dir.mkdir(parents=True, exist_ok=False)
    maintenance_root = run_dir / "maintenance-output"
    maintenance_root.mkdir()
    metadata = {
        "story": "4.1",
        "scenario": args.scenario,
        "started_at": time.time(),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "live": bool(args.live),
        "authorization": (
            "explicit user authorization received" if args.live else "simulation"
        ),
        "formal_gate": bool(args.live and not args.exploratory),
        "source": args.source_state,
        "recipe": recipe,
        "mapping_observation": {
            "method": "hand outside outlet, without touching or blocking",
            "at_1500_sccm": "detectable but may be too weak for reliable mapping",
            "result": "not claimed by this automated gate",
        },
        "claim_boundary": (
            "software-injected LOW_FLOW/disconnect are declared injections; "
            "DAQmx write acknowledgement is not mechanical completion evidence"
        ),
    }
    if args.live:
        metadata["hardware_inventory"] = hardware_inventory()
    _write_json(run_dir / "metadata.json", metadata)

    if not args.live:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    controller = None
    app = None
    snapshots: list[dict[str, Any]] = []
    last_key: tuple[Any, ...] | None = None
    injected = False
    running_started_at: float | None = None
    summary: dict[str, Any] = {"metadata": metadata}

    def capture() -> None:
        nonlocal last_key
        if controller is None:
            return
        payload = _snapshot_payload(controller)
        key = (
            payload["status"],
            payload["current_step_id"],
            payload["current_channel"],
            payload["close_confirmed"],
            payload["close_required"],
            tuple(payload["possibly_open"]),
            payload["safety_state"],
            payload["connected"],
            payload["flow_setpoints_ready"],
            payload["device_lease"],
        )
        if key != last_key:
            snapshots.append(payload)
            last_key = key
            _write_json(run_dir / "snapshots.json", {"snapshots": snapshots})

    try:
        app, window = build_application(
            args.config,
            start_worker=True,
            simulation=not args.live,
            local_config_path=args.local_config,
        )
        controller = window.controller
        controller._cleaning_output_root = maintenance_root
        ready = pump_until(
            app,
            lambda: bool(
                controller.state.hardware_ready
                and controller.state.telemetry.connected
                and (
                    controller.state.flow_setpoints_ready
                    or controller._startup_zero_confirmed
                )
                and controller.state.telemetry.safety_state in {"SAFE", "LOW_FLOW"}
            ),
            timeout_s=args.startup_timeout_s,
            tick=capture,
        )
        if not ready:
            raise RuntimeError(
                "startup readiness timeout: "
                f"hardware_ready={controller.state.hardware_ready}, "
                f"connected={controller.state.telemetry.connected}, "
                f"flow_setpoints_ready={controller.state.flow_setpoints_ready}, "
                f"safety={controller.state.telemetry.safety_state}"
            )
        if not controller.handle_cleaning_start_requested():
            raise RuntimeError(controller._cleaning_display_message)

        def terminal() -> bool:
            nonlocal injected, running_started_at
            capture()
            runtime = controller._cleaning_runtime
            if (
                runtime.status == CleaningStatus.RUNNING
                and running_started_at is None
            ):
                running_started_at = time.monotonic()
            if (
                args.scenario != "baseline"
                and not injected
                and runtime.status == CleaningStatus.RUNNING
                and running_started_at is not None
                and time.monotonic() - running_started_at >= args.trigger_after_s
            ):
                if args.scenario == "stop":
                    injected = bool(controller.handle_cleaning_stop_requested())
                elif args.scenario == "low-flow":
                    controller.state.telemetry.safety_state = "LOW_FLOW"
                    controller.actuation_interlock.update(safety_state="LOW_FLOW")
                    controller.actuation_worker.post_interlock_changed(
                        timestamp=time.time()
                    )
                    injected = True
                elif args.scenario == "disconnect":
                    controller.state.telemetry.connected = False
                    controller.state.hardware_ready = False
                    controller.actuation_interlock.update(
                        connected=False,
                        hardware_ready=False,
                    )
                    controller.actuation_worker.post_interlock_changed(
                        timestamp=time.time()
                    )
                    injected = True
            return (
                runtime.status in TERMINAL_STATUSES
                and controller._cleaning_finalize_event.is_set()
            )

        if not pump_until(app, terminal, timeout_s=args.timeout_s, tick=capture):
            raise TimeoutError(
                f"cleaning scenario {args.scenario} did not finalize in "
                f"{args.timeout_s:g}s"
            )
        final_result = controller.wait_for_cleaning_finalization(5)
        capture()
        summary.update(
            {
                "scenario_injected": injected,
                "runtime": _snapshot_payload(controller),
                "finalization": (
                    None if final_result is None else asdict(final_result)
                ),
                "bundle": _bundle_evidence(controller, final_result),
                "snapshots": snapshots,
                "owner_handoff_ready": bool(
                    controller.actuation_worker.cleaning_owner_handoff_ready
                ),
                "last_flow_result": (
                    None
                    if controller._last_flow_result is None
                    else asdict(controller._last_flow_result)
                ),
            }
        )
        runtime = controller._cleaning_runtime
        last_flow = controller._last_flow_result
        safely_closed = bool(
            runtime.close_required > 0
            and runtime.close_confirmed >= runtime.close_required
            and not runtime.possibly_open
            and controller.actuation_worker.cleaning_owner_handoff_ready
            and controller.device_lease.snapshot.kind.value == "idle"
        )
        flow_zero = bool(
            last_flow is not None
            and last_flow.success
            and all(
                abs(value) <= 1e-9
                for value in (last_flow.a, last_flow.b, last_flow.c)
            )
        )
        if args.scenario == "baseline":
            passed = bool(
                final_result is not None
                and final_result.complete
                and summary["bundle"]["validation_complete"]
                and runtime.status == CleaningStatus.COMPLETED
                and final_result.outcome == "completed"
                and safely_closed
                and flow_zero
            )
        elif args.scenario == "stop":
            intervals = summary["bundle"].get("open_intervals_ms", [])
            requested_open_ms = args.trigger_after_s * 1000
            passed = bool(
                injected
                and final_result is not None
                and final_result.complete
                and summary["bundle"]["validation_complete"]
                and runtime.status == CleaningStatus.COMPLETED
                and final_result.outcome == "aborted"
                and safely_closed
                and flow_zero
                and intervals
                and float(intervals[0]["duration_ms"])
                >= requested_open_ms * 0.8
            )
        else:
            passed = bool(
                injected
                and final_result is not None
                and not final_result.complete
                and final_result.outcome == "failed"
                and runtime.status
                in {CleaningStatus.FAILED, CleaningStatus.RECOVERY_REQUIRED}
                and summary["bundle"]["staging_exists"]
                and safely_closed
                and flow_zero
            )
        summary["passed"] = passed
        return 0 if passed else 1
    except Exception as exc:
        summary.update(
            {
                "passed": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "snapshots": snapshots,
            }
        )
        if controller is not None:
            try:
                controller.handle_cleaning_stop_requested()
                if app is not None:
                    pump_until(
                        app,
                        lambda: controller._cleaning_runtime.status
                        in TERMINAL_STATUSES,
                        timeout_s=10,
                        tick=capture,
                    )
            except Exception:
                summary["emergency_stop_traceback"] = traceback.format_exc()
        return 1
    finally:
        if controller is not None:
            try:
                controller.shutdown_and_teardown()
            except Exception:
                summary["shutdown_traceback"] = traceback.format_exc()
                summary["passed"] = False
        summary["finished_at"] = time.time()
        _write_json(run_dir / "summary.json", summary)


if __name__ == "__main__":
    raise SystemExit(main())
