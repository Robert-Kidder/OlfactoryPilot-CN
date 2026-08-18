from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCENARIOS = (
    "normal",
    "a_zero_failure",
    "a_zero_timeout",
    "stale_a_receipt",
    "late_a_receipt",
    "stale_selector_receipt",
    "late_selector_receipt",
    "selector_uncertain",
    "owner_handoff_failed",
)
EXPECTED_NI = {
    "Dev1": {"product_type": "USB-6001", "serial_num": 34887710},
    "Dev2": {"product_type": "USB-6001", "serial_num": 34887797},
}
EXPECTED_A_MODEL = "MC-5NLPM-D"
EXPECTED_A_SERIAL = "486285"
EXPECTED_A_FULL_SCALE_SCCM = 5000.0
TEST_FLOW_SCCM = 2500.0
OBSERVATION_SECONDS = 20.0
FLOW_READY_FRACTION = 0.90
FLOW_UPPER_FRACTION = 1.10
ALLOWED_OPERATOR_OBSERVATIONS = ("持续气流", "短促气流", "无气流")


class Timeline:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.sequence = 0
        self.events: list[dict[str, Any]] = []
        self.audit_errors: list[str] = []

    def record(self, payload: dict[str, Any]) -> None:
        self.sequence += 1
        item = {
            "sequence": self.sequence,
            "wall_timestamp": time.time(),
            "monotonic_ns": time.perf_counter_ns(),
            **payload,
        }
        self.events.append(item)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            except OSError as exc:
                # Evidence I/O must never prevent the already-authorized safety closeout.
                self.audit_errors.append(repr(exc))


def manifest_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def candidate_snapshot(*, require_clean: bool = True) -> dict[str, Any]:
    status = _run_git("status", "--porcelain=v1", "--untracked-files=all")
    if require_clean and status:
        raise RuntimeError("HIL 候选工作树不干净；必须先创建新的本地候选 commit。")
    return {
        "commit": _run_git("rev-parse", "HEAD"),
        "tree": _run_git("rev-parse", "HEAD^{tree}"),
        "status_porcelain": status.splitlines(),
        "clean": not bool(status),
    }


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_effective_config(
    default_path: Path = PROJECT_ROOT / "config" / "default_config.json",
    local_path: Path = PROJECT_ROOT / "config" / "local_config.json",
) -> dict[str, Any]:
    with default_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if local_path.exists():
        with local_path.open(encoding="utf-8") as handle:
            config = _merge(config, json.load(handle))
    return config


def safety_config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Canonical safety-critical config bound into the signed manifest."""

    selector, odors = _mapping(config)
    keys = (
        "serial_port",
        "baud_rate",
        "alicat_timeout_s",
        "alicat_setpoint_tolerance",
        "alicat_setpoint_verify_delay_s",
        "alicat_setpoint_verify_retries",
        "alicat_setpoint_scale",
        "alicat_readback_scale",
        "alicat_flow_unit",
        "alicat_flow_field",
        "actuation_write_timeout_ms",
        "actuation_emergency_close_timeout_ms",
        "actuation_shutdown_timeout_ms",
        "hardware_variant",
    )
    return {
        "values": {key: config.get(key) for key in keys},
        "alicat_unit_ids": dict(config.get("alicat_unit_ids") or {}),
        "selector": selector,
        "odor_targets": odors,
        "expected_ni": EXPECTED_NI,
    }


def _mapping(config: dict[str, Any]) -> tuple[str, list[str]]:
    mapping = config["valve_mapping"]
    selector = mapping["selector"]
    if (
        selector.get("target") != "Dev2/P1.0"
        or selector.get("safe_level") is not False
        or selector.get("odor_level") is not True
    ):
        raise ValueError("selector 必须保持 Dev2/P1.0、LOW=补偿、HIGH=气味。")
    variant = str(config.get("hardware_variant", "20-channel"))
    raw = mapping["variants"][variant]
    if set(raw) != {str(item) for item in range(1, 21)}:
        raise ValueError("真实 HIL 要求精确的气味阀 1–20 映射。")
    odors = [str(raw[str(item)]) for item in range(1, 21)]
    if len({item.lower() for item in odors}) != 20:
        raise ValueError("气味阀映射必须唯一。")
    if selector["target"].lower() in {item.lower() for item in odors}:
        raise ValueError("selector 不得出现在 1–20 映射中。")
    return str(selector["target"]), odors


def _write(
    writes: list[dict[str, Any]],
    phase: str,
    kind: str,
    target: str,
    value: float | bool,
    *,
    comp: bool = False,
    optional: bool = False,
) -> None:
    writes.append(
        {
            "sequence": len(writes) + 1,
            "phase": phase,
            "kind": kind,
            "target": target,
            "value": value,
            "comp": comp,
            "optional": optional,
        }
    )


def scenario_writes(
    scenario: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    if scenario not in SCENARIOS:
        raise ValueError(f"未知场景：{scenario}")
    selector, odors = _mapping(config)
    writes: list[dict[str, Any]] = []
    if scenario == "normal":
        for target in odors:
            _write(writes, "setup", "digital", target, False, optional=True)
        _write(writes, "setup", "digital", selector, False, optional=True)
        _write(writes, "setup", "digital", selector, True, optional=True)
        _write(writes, "setup", "digital", odors[1], True, optional=True)
        _write(writes, "setup", "flow", "A", TEST_FLOW_SCCM, optional=True)

    _write(writes, "shutdown", "flow", "A", 0.0)
    a_fault = scenario in {
        "a_zero_failure",
        "a_zero_timeout",
        "stale_a_receipt",
        "late_a_receipt",
    }
    if not a_fault:
        _write(
            writes,
            "shutdown",
            "digital",
            selector,
            False,
            optional=True,
        )
    for target in odors:
        _write(writes, "shutdown", "digital", target, False)
    for channel in ("B", "C", "A"):
        _write(writes, "shutdown", "flow", channel, 0.0)
    # ShutdownService may retry the complete odor-close block only after DO
    # handoff when any initial close receipt is uncertain.
    for target in odors:
        _write(writes, "fallback", "digital", target, False, optional=True)
    return tuple(writes)


def build_manifest(
    scenario: str,
    *,
    candidate: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    selector, odors = _mapping(config)
    payload: dict[str, Any] = {
        "schema": "story-4.5-live-manifest-v2",
        "scenario": scenario,
        "candidate": candidate,
        "gas": "Air",
        "no_odor_material": True,
        "no_subject": True,
        "normal_parameters": {
            "flow_channel": "A",
            "flow_sccm": TEST_FLOW_SCCM,
            "confirmed_full_scale_sccm": EXPECTED_A_FULL_SCALE_SCCM,
            "representative_valve": 2,
            "stable_fraction": FLOW_READY_FRACTION,
            "observation_seconds": OBSERVATION_SECONDS,
            "operator_distance_cm": [2, 5],
            "must_not_block_outlet": True,
        },
        "selector": {
            "target": selector,
            "safe_level": False,
            "odor_level": True,
        },
        "odor_targets": odors,
        "expected_ni": EXPECTED_NI,
        "expected_a": {
            "model": EXPECTED_A_MODEL,
            "serial": EXPECTED_A_SERIAL,
            "full_scale_sccm": EXPECTED_A_FULL_SCALE_SCCM,
        },
        "effective_config": safety_config_snapshot(config),
        "effective_config_sha256": manifest_sha256(safety_config_snapshot(config)),
        "writes": list(scenario_writes(scenario, config)),
    }
    payload["manifest_sha256"] = manifest_sha256(payload)
    payload["authorization_token"] = authorization_token(payload)
    return payload


def authorization_token(manifest: dict[str, Any]) -> str:
    return "STORY45:{scenario}:{commit}:{digest}".format(
        scenario=manifest["scenario"],
        commit=manifest["candidate"]["commit"],
        digest=manifest.get("manifest_sha256", ""),
    )


def validate_manifest(manifest: dict[str, Any], token: str) -> None:
    if manifest.get("schema") != "story-4.5-live-manifest-v2":
        raise ValueError("manifest schema 不受支持。")
    supplied_digest = str(manifest.get("manifest_sha256", ""))
    supplied_token = str(manifest.get("authorization_token", ""))
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    unsigned.pop("authorization_token", None)
    actual_digest = manifest_sha256(unsigned)
    if supplied_digest != actual_digest:
        raise ValueError("manifest SHA-256 不匹配。")
    expected_token = authorization_token(
        {**manifest, "manifest_sha256": actual_digest}
    )
    if supplied_token != expected_token or token != expected_token:
        raise ValueError("授权 token 与候选/场景/manifest 不匹配。")
    snapshot = candidate_snapshot(require_clean=True)
    if snapshot["commit"] != manifest["candidate"]["commit"]:
        raise ValueError("当前 HEAD 不是 manifest 固定的候选 commit。")
    if snapshot["tree"] != manifest["candidate"]["tree"]:
        raise ValueError("当前候选 tree 与 manifest 不匹配。")


def validate_effective_config(manifest: dict[str, Any], config: dict[str, Any]) -> None:
    snapshot = safety_config_snapshot(config)
    digest = manifest_sha256(snapshot)
    if snapshot != manifest.get("effective_config"):
        raise ValueError("当前有效硬件配置与已授权 manifest 不一致。")
    if digest != manifest.get("effective_config_sha256"):
        raise ValueError("当前有效硬件配置 SHA-256 与 manifest 不一致。")


def _serial_query_lines(port: Any, command: str, *, multiline: bool = False) -> list[str]:
    port.reset_input_buffer()
    port.write(command.encode("ascii"))
    port.flush()
    lines: list[str] = []
    deadline = time.monotonic() + (1.5 if multiline else 0.7)
    while time.monotonic() < deadline:
        raw = port.readline()
        if not raw:
            if lines:
                break
            continue
        decoded = raw.decode("ascii", errors="replace").strip()
        if decoded:
            lines.append(decoded)
        if not multiline:
            break
    return lines


def _parse_status(response: str, expected_unit: str) -> dict[str, Any]:
    parts = response.split()
    if len(parts) < 7 or parts[0].lower() != expected_unit.lower():
        raise ValueError(f"Alicat {expected_unit} 状态帧无效：{response!r}")
    numeric = [float(item) for item in parts[1:6]]
    if not all(math.isfinite(item) for item in numeric):
        raise ValueError(f"Alicat {expected_unit} 状态帧含非有限数值：{response!r}")
    return {
        "raw": response,
        "mass_flow_sccm": numeric[3] * 1000.0,
        "setpoint_sccm": numeric[4] * 1000.0,
        "gas": parts[6] if len(parts) > 6 else "",
        "status_codes": parts[7:],
    }


def _validate_data_frame_description(unit: str, lines: list[str]) -> None:
    text = " ".join(lines).casefold()
    mass_flow_position = text.find("mass flow")
    setpoint_positions = [
        position
        for position in (text.find("mass flow setpt"), text.find("setpoint"))
        if position >= 0
    ]
    setpoint_position = min(setpoint_positions) if setpoint_positions else -1
    positions = [mass_flow_position, setpoint_position, text.find("gas")]
    if not lines or any(item < 0 for item in positions) or positions != sorted(positions):
        raise RuntimeError(
            f"Alicat {unit} 数据帧字段不是已批准的 Mass Flow/Setpoint/Gas 顺序：{lines!r}"
        )
    if "nlpm" not in text:
        raise RuntimeError(f"Alicat {unit} Mass Flow/Setpoint 单位未确认为 NLPM：{lines!r}")


def _parse_full_scale_nlpm(response: str, unit: str) -> float:
    match = re.fullmatch(
        rf"\s*{re.escape(unit)}(?:\s+5)?\s+([-+]?\d+(?:\.\d+)?)\s+37\s+NLPM\s*",
        response,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError(f"满量程响应结构无效：{response!r}")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise ValueError(f"满量程响应不是有限数值：{response!r}")
    return value


def read_only_preflight(config: dict[str, Any]) -> dict[str, Any]:
    import serial
    from nidaqmx.system import System

    actual_ni = {
        device.name: {
            "product_type": str(device.product_type),
            "serial_num": int(device.serial_num),
        }
        for device in System.local().devices
        if device.name in EXPECTED_NI
    }
    if set(actual_ni) != set(EXPECTED_NI):
        raise RuntimeError(f"NI 设备不完整：{actual_ni}")
    for name, expected in EXPECTED_NI.items():
        actual = actual_ni[name]
        if expected["product_type"] not in actual["product_type"]:
            raise RuntimeError(f"{name} 型号不匹配：{actual}")
        if actual["serial_num"] != expected["serial_num"]:
            raise RuntimeError(f"{name} 序列号不匹配：{actual}")

    serial_port = str(config.get("serial_port") or "")
    if not serial_port:
        raise RuntimeError("local_config.json 未配置 Alicat serial_port。")
    baud = int(config.get("baud_rate", 19200))
    timeout = max(0.2, float(config.get("alicat_timeout_s", 0.2)))
    ids = config.get("alicat_unit_ids") or {"A": "a", "B": "b", "C": "c"}
    with serial.Serial(serial_port, baud, timeout=timeout) as port:
        statuses = {}
        data_frames = {}
        for channel in ("A", "B", "C"):
            unit = str(ids[channel])
            data_frames[channel] = _serial_query_lines(
                port, f"{unit}??D*\r", multiline=True
            )
            _validate_data_frame_description(unit, data_frames[channel])
            lines = _serial_query_lines(port, f"{unit}\r")
            if not lines:
                raise RuntimeError(f"Alicat {channel} 未返回状态。")
            statuses[channel] = _parse_status(lines[0], unit)
        unit_a = str(ids["A"])
        manufacturing = _serial_query_lines(port, f"{unit_a}??M*\r", multiline=True)
        full_scale = _serial_query_lines(port, f"{unit_a}FPF 5\r")

    manufacturing_text = "\n".join(manufacturing)
    if EXPECTED_A_MODEL not in manufacturing_text or EXPECTED_A_SERIAL not in manufacturing_text:
        raise RuntimeError(
            "A 型号/序列号与批准基线不符：" + manufacturing_text
        )
    full_scale_text = " ".join(full_scale)
    try:
        full_scale_nlpm = _parse_full_scale_nlpm(full_scale_text, str(ids["A"]))
    except ValueError as exc:
        raise RuntimeError(f"A 满量程响应无效：{full_scale_text!r}") from exc
    if not math.isclose(full_scale_nlpm, 5.0, abs_tol=1e-6):
        raise RuntimeError(f"A 满量程查询不是 5 NLPM：{full_scale_text!r}")
    for channel, status in statuses.items():
        if str(status["gas"]).casefold() != "air":
            raise RuntimeError(f"{channel} 当前气体不是 Air：{status}")
        if status["status_codes"]:
            raise RuntimeError(f"{channel} 报告了状态/错误码：{status}")
        if abs(float(status["setpoint_sccm"])) > 0.5:
            raise RuntimeError(f"{channel} setpoint 不是 0：{status}")
        if abs(float(status["mass_flow_sccm"])) > 5.0:
            raise RuntimeError(f"{channel} 实测流量未收敛到 0：{status}")
    return {
        "hardware_access": "read_only_queries",
        "ni": actual_ni,
        "alicat": statuses,
        "alicat_data_frames": data_frames,
        "a_manufacturing": manufacturing,
        "a_full_scale": full_scale,
        "serial_port": serial_port,
        "baud_rate": baud,
    }


class _FlowFaultProxy:
    def __init__(self, owner: Any, scenario: str, timeline: Timeline) -> None:
        self._owner = owner
        self._scenario = scenario
        self._timeline = timeline

    def zero_a_for_safe_stop(self, identity: Any, timeout_ms: int) -> Any:
        receipt = self._owner.zero_a_for_safe_stop(identity, timeout_ms)
        if receipt is None:
            return None
        scenario = self._scenario
        if scenario == "a_zero_failure":
            receipt = replace(receipt, success=False, message="injected A receipt failure")
        elif scenario == "a_zero_timeout":
            receipt = None
        elif scenario == "stale_a_receipt":
            receipt = replace(receipt, stale=True, message="injected stale A receipt")
        elif scenario == "late_a_receipt":
            time.sleep((int(timeout_ms) + 50) / 1000.0)
            receipt = replace(receipt, stale=True, message="injected late A receipt")
        self._timeline.record(
            {
                "event": "a_zero_receipt",
                "scenario": scenario,
                "receipt": None if receipt is None else asdict(receipt),
            }
        )
        return receipt

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._owner, name)
        if name not in {
            "zero_all_for_safe_stop",
            "release_lease_for_safe_stop",
            "shutdown",
        } or not callable(value):
            return value

        def audited(*args: Any, **kwargs: Any) -> Any:
            self._timeline.record({"event": "owner_call_enter", "owner_call": name})
            result = value(*args, **kwargs)
            self._timeline.record(
                {"event": "owner_call_exit", "owner_call": name, "result": bool(result)}
            )
            return result

        return audited


class _ActuationFaultProxy:
    def __init__(self, owner: Any, scenario: str, timeline: Timeline) -> None:
        self._owner = owner
        self._scenario = scenario
        self._timeline = timeline
        self.maintenance_actual: bool | None = None
        self.maintenance_reported: bool | None = None

    def route_selector_safe(self, plan: Any, timeout_ms: int) -> Any:
        receipt = self._owner.route_selector_safe(plan, timeout_ms)
        scenario = self._scenario
        if receipt is not None and scenario == "stale_selector_receipt":
            receipt = replace(receipt, stale=True, message="injected stale selector receipt")
        elif receipt is not None and scenario == "late_selector_receipt":
            time.sleep((int(timeout_ms) + 50) / 1000.0)
            receipt = replace(receipt, stale=True, message="injected late selector receipt")
        self._timeline.record(
            {
                "event": "selector_receipt",
                "scenario": scenario,
                "receipt": None if receipt is None else asdict(receipt),
            }
        )
        return receipt

    def handoff_maintenance_for_safe_stop(self) -> bool:
        actual = bool(self._owner.handoff_maintenance_for_safe_stop())
        self.maintenance_actual = actual
        if self._scenario == "owner_handoff_failed":
            self._timeline.record(
                {"event": "injected_owner_handoff_failure", "actual": actual}
            )
            self.maintenance_reported = False
            return False
        self.maintenance_reported = actual
        self._timeline.record(
            {
                "event": "owner_call_exit",
                "owner_call": "handoff_maintenance_for_safe_stop",
                "result": actual,
            }
        )
        return actual

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._owner, name)
        if name not in {
            "fence_for_safe_stop",
            "close_odors_for_safe_stop",
            "shutdown",
            "fallback_close_all_after_handoff",
        } or not callable(value):
            return value

        def audited(*args: Any, **kwargs: Any) -> Any:
            self._timeline.record({"event": "owner_call_enter", "owner_call": name})
            result = value(*args, **kwargs)
            self._timeline.record(
                {"event": "owner_call_exit", "owner_call": name, "result": bool(result)}
            )
            return result

        return audited


class _HardwareAuditProxy:
    def __init__(self, owner: Any, timeline: Timeline) -> None:
        self._owner = owner
        self._timeline = timeline
        self.ai_release_result: bool | None = None

    def stop_heaters(self) -> bool:
        result = bool(self._owner.stop_heaters())
        self._timeline.record(
            {"event": "owner_call_exit", "owner_call": "stop_heaters", "result": result}
        )
        return result

    def flush_logs(self) -> None:
        self._owner.flush_logs()
        self._timeline.record(
            {"event": "owner_call_exit", "owner_call": "flush_logs", "result": True}
        )

    def release_ai_resources(self) -> bool:
        self.ai_release_result = bool(self._owner.release_ai_resources())
        self._timeline.record(
            {
                "event": "owner_call_exit",
                "owner_call": "release_ai_resources",
                "result": self.ai_release_result,
            }
        )
        return self.ai_release_result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner, name)


def _split_target(target: str) -> tuple[str, str]:
    device, token = target.split("/", 1)
    port, line = token.upper().split(".", 1)
    return device, f"port{port[1:]}/line{line}"


def _build_runtime(config: dict[str, Any], hal: Any, timeline: Timeline) -> dict[str, Any]:
    from app.models import AppState, ProtocolExecutionState, ProtocolExecutionStatus
    from app.services import FlowService, SafetyManager, ShutdownService, ValveService
    from app.services.actuation_do_adapter import ActuationDOAdapter
    from app.workers import ActuationWorker, FlowWorker, HardwareWorker
    from app.workers.actuation_worker import ActuationInterlockIngress, InterlockSnapshot

    state = AppState.from_config(config)
    state.hardware_ready = True
    state.flow_setpoints_ready = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.telemetry.airflow = 0.0
    state.telemetry.timestamp = time.time()
    safety = SafetyManager(float(config.get("low_flow_threshold", 0.2)))
    valve_service = ValveService(
        state=state,
        safety_manager=safety,
        worker=SimpleNamespace(is_connected=True),
        valve_variants=state.valve_variants,
        hardware_variant=state.hardware_variant,
        master_valve_line=state.master_valve_line,
    )
    ingress = ActuationInterlockIngress(
        InterlockSnapshot(
            connected=True,
            hardware_ready=True,
            flow_setpoints_ready=True,
            safety_state="SAFE",
            ttl_input_ready=False,
            has_protocol=False,
            device_lease="idle",
        ),
        safety_manager=safety,
    )
    protocol_state = ProtocolExecutionState(
        status=ProtocolExecutionStatus.WAITING_TRIGGER,
        execution_epoch=1,
        arm_epoch=1,
    )
    adapter = ActuationDOAdapter(
        hal=hal,
        target_resolver=valve_service.resolve_target,
        selector_target=valve_service.selector_target,
        selector_odor_level=True,
        write_timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
    )
    flow_service = FlowService(hal, master_target=None, master_writer=None)
    flow_owner = FlowWorker(flow_service)
    actuation_owner = ActuationWorker(
        protocol_state=protocol_state,
        writer=adapter.execute,
        interlock=ingress,
        valve_service=valve_service,
        flow_submitter=flow_owner.submit,
        normal_queue_capacity=int(config.get("actuation_normal_queue_capacity", 256)),
    )
    hardware_owner = HardwareWorker(
        telemetry_hz=5,
        breath_hz=100,
        ttl_config=config,
        check_service=None,
        hal=hal,
        simulation=False,
    )
    return {
        "state": state,
        "safety": safety,
        "valve_service": valve_service,
        "flow_owner": flow_owner,
        "actuation_owner": actuation_owner,
        "hardware_owner": hardware_owner,
        "ShutdownService": ShutdownService,
        "timeline": timeline,
    }


def _normal_setup(
    hal: Any,
    config: dict[str, Any],
    timeline: Timeline,
    *,
    sleep_func: Any = time.sleep,
) -> list[dict[str, Any]]:
    selector, odors = _mapping(config)
    if not hal.prepare_do_output():
        raise RuntimeError("NI DO owner 准备失败。")
    for target in odors:
        device, line = _split_target(target)
        ack = hal.write_digital_ack(
            device=device,
            line=line,
            state=False,
            timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
        )
        if not ack.success:
            raise RuntimeError(f"初始关闭失败：{target}: {ack.message}")
    device, line = _split_target(selector)
    for state in (False, True):
        ack = hal.write_digital_ack(
            device=device,
            line=line,
            state=state,
            timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
        )
        if not ack.success:
            raise RuntimeError(f"selector setup 写入失败：{ack.message}")
    device, line = _split_target(odors[1])
    ack = hal.write_digital_ack(
        device=device,
        line=line,
        state=True,
        timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
    )
    if not ack.success:
        raise RuntimeError(f"代表阀 2 打开失败：{ack.message}")
    if not hal.set_flow("A", TEST_FLOW_SCCM, comp=False):
        raise RuntimeError("A=2500 sccm 未获得 Alicat 确认。")

    samples: list[dict[str, Any]] = []
    lower = TEST_FLOW_SCCM * FLOW_READY_FRACTION
    upper = TEST_FLOW_SCCM * FLOW_UPPER_FRACTION

    def sample_flow(phase: str, *, require_stable: bool) -> float:
        value = float(hal.read_flow())
        sample = {"event": "flow_sample", "a_mass_sccm": value, "phase": phase}
        samples.append(sample)
        timeline.record(sample)
        if not math.isfinite(value) or value < 0.0 or value > upper:
            raise RuntimeError(f"A 流量越过批准安全范围 0..{upper:g} sccm：{value!r}")
        if require_stable and value < lower:
            raise RuntimeError(f"20 秒观察期间 A 流量低于 {lower:g} sccm：{value!r}")
        return value

    ready_deadline = time.monotonic() + 5.0
    consecutive = 0
    while time.monotonic() < ready_deadline:
        value = sample_flow("ready", require_stable=False)
        consecutive = consecutive + 1 if value >= lower else 0
        if consecutive >= 3:
            break
        sleep_func(0.2)
    if consecutive < 3:
        raise RuntimeError("A 在 5 秒内未稳定达到 2500 sccm 的 90%。")
    timeline.record(
        {
            "event": "operator_observation_window_started",
            "seconds": OBSERVATION_SECONDS,
            "valve": 2,
        }
    )
    end = time.monotonic() + OBSERVATION_SECONDS
    while time.monotonic() < end:
        sample_flow("observe", require_stable=True)
        sleep_func(min(0.5, max(0.0, end - time.monotonic())))
    timeline.record({"event": "operator_observation_window_finished"})
    return samples


def _writes_from_manifest(manifest: dict[str, Any]) -> tuple[Any, ...]:
    from app.services.authorized_hal import AuthorizedWrite

    return tuple(AuthorizedWrite(**item) for item in manifest["writes"])


def execute_live(
    manifest: dict[str, Any],
    *,
    output_dir: Path,
    config: dict[str, Any],
    delegate_hal: Any | None = None,
    preflight_result: dict[str, Any] | None = None,
    sleep_func: Any = time.sleep,
    operator_observation: str | None = None,
) -> dict[str, Any]:
    from app.services.authorized_hal import (
        AuthorizationViolation,
        AuthorizedHAL,
        AuthorizedWrite,
    )

    output_dir.mkdir(parents=True, exist_ok=False)
    timeline = Timeline(output_dir / "timeline.jsonl")
    (output_dir / "run-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "preflight.json").write_text(
        json.dumps(preflight_result or {}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if delegate_hal is None:
        from app.services.real_hal import RealHAL

        delegate_hal = RealHAL.from_config(config)
    scenario = str(manifest["scenario"])

    def transform_ack(intent: AuthorizedWrite, ack: Any) -> Any:
        if (
            scenario == "selector_uncertain"
            and intent.phase == "shutdown"
            and intent.kind == "digital"
            and intent.target == manifest["selector"]["target"]
            and intent.value is False
        ):
            return replace(
                ack,
                success=False,
                uncertain=True,
                message=(
                    "injected selector write uncertainty; "
                    f"delegate_success={ack.success}"
                ),
            )
        return ack

    hal = AuthorizedHAL(
        delegate_hal,
        _writes_from_manifest(manifest),
        audit_sink=timeline.record,
        digital_ack_transform=transform_ack,
    )
    runtime = _build_runtime(config, hal, timeline)
    flow_owner = _FlowFaultProxy(runtime["flow_owner"], scenario, timeline)
    actuation_owner = _ActuationFaultProxy(
        runtime["actuation_owner"], scenario, timeline
    )
    hardware_owner = _HardwareAuditProxy(runtime["hardware_owner"], timeline)
    shutdown_service = runtime["ShutdownService"](
        state=runtime["state"],
        worker=hardware_owner,
        safety_manager=runtime["safety"],
        retry_limit=0,
        record_path=output_dir / "shutdown-event.json",
        actuation_worker=actuation_owner,
        flow_worker=flow_owner,
        actuation_timeout_ms=int(config.get("actuation_shutdown_timeout_ms", 2000)),
        emergency_close_timeout_ms=int(
            config.get("actuation_emergency_close_timeout_ms", 500)
        ),
        selector=runtime["state"].selector,
    )
    shutdown_event: dict[str, Any] | None = None
    setup_error = ""
    samples: list[dict[str, Any]] = []
    try:
        if scenario == "normal":
            samples = _normal_setup(
                hal,
                config,
                timeline,
                sleep_func=sleep_func,
            )
    except Exception as exc:
        setup_error = repr(exc)
        timeline.record({"event": "setup_exception", "error": setup_error})
    finally:
        try:
            hal.advance_to_phase("shutdown")
        except Exception as exc:
            setup_error = setup_error or repr(exc)
        try:
            shutdown_event = shutdown_service.shutdown(
                source="story-4.5-live-hil",
                reason=setup_error or f"scenario:{scenario}",
                airflow=float(samples[-1]["a_mass_sccm"]) if samples else 0.0,
            )
        except Exception as exc:  # defensive: still emit recovery evidence
            setup_error = setup_error or repr(exc)
            shutdown_event = {
                "result": "recovery_required",
                "safe_stop_status": "recovery_required",
                "recovery_reason": f"shutdown exception: {exc!r}",
            }
        timeline.record({"event": "shutdown_complete", "payload": shutdown_event})

    expected_result = "success" if scenario == "normal" and not setup_error else "recovery_required"
    fault_oracle_passed = bool(
        shutdown_event
        and shutdown_event.get("result") == expected_result
        and not hal.violations
    )
    try:
        hal.assert_required_consumed()
    except AuthorizationViolation as exc:
        fault_oracle_passed = False
        setup_error = setup_error or repr(exc)
    final_state = _derive_final_state(
        manifest,
        timeline.events,
        shutdown_event or {},
        operator_observation=operator_observation,
    )
    assertions = _scenario_assertions(scenario, timeline.events, shutdown_event or {})
    automated_verification_passed = bool(
        fault_oracle_passed and all(assertions.values()) and not timeline.audit_errors
    )
    operator_observation_required = scenario == "normal"
    operator_observation_passed = (
        operator_observation == "持续气流" if operator_observation_required else True
    )
    verification_passed = bool(
        automated_verification_passed
        and scenario == "normal"
        and expected_result == "success"
        and operator_observation_passed
    )
    lease = runtime["flow_owner"].lease_snapshot
    owner_handoff = {
        "maintenance_actual": actuation_owner.maintenance_actual,
        "maintenance_reported": actuation_owner.maintenance_reported,
        "do_handed_off": bool(getattr(runtime["actuation_owner"], "_do_handed_off", False)),
        "lease_kind": getattr(getattr(lease, "kind", None), "value", str(getattr(lease, "kind", ""))),
        "lease_released": getattr(getattr(lease, "kind", None), "value", "") == "idle",
        "ai_released": hardware_owner.ai_release_result,
        "flow_accepting": runtime["flow_owner"].execution_context[2],
        "serial_resources_in_use": bool(hal.serial_resources_in_use),
    }
    owner_handoff["complete"] = bool(
        owner_handoff["maintenance_reported"]
        and owner_handoff["do_handed_off"]
        and owner_handoff["lease_released"]
        and owner_handoff["ai_released"]
        and not owner_handoff["flow_accepting"]
        and not owner_handoff["serial_resources_in_use"]
    )
    summary = {
        "scenario": scenario,
        "expected_result": expected_result,
        "actual_result": None if shutdown_event is None else shutdown_event.get("result"),
        "overall_result": (
            "recovery_required"
            if setup_error or not automated_verification_passed
            else shutdown_event.get("result") if shutdown_event else "recovery_required"
        ),
        "safe_stop_status": None
        if shutdown_event is None
        else shutdown_event.get("safe_stop_status"),
        "setup_error": setup_error,
        "authorization_cursor": hal.cursor,
        "authorization_total": len(manifest["writes"]),
        "authorization_violations": list(hal.violations),
        "owner_handoff": owner_handoff,
        "assertions": assertions,
        "final_state": final_state,
        "audit_errors": list(timeline.audit_errors),
        "fault_oracle_passed": fault_oracle_passed,
        "automated_verification_passed": automated_verification_passed,
        "operator_observation_required": operator_observation_required,
        "operator_observation": operator_observation,
        "operator_observation_passed": operator_observation_passed,
        "verification_passed": verification_passed,
    }
    (output_dir / "final-state.json").write_text(
        json.dumps(final_state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "owner-handoff.json").write_text(
        json.dumps(summary["owner_handoff"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_event_index(
        output_dir / "commands.jsonl",
        timeline.events,
        {"authorization_consumed", "hardware_write_result"},
    )
    _write_event_index(
        output_dir / "receipts.jsonl",
        timeline.events,
        {"a_zero_receipt", "selector_receipt", "hardware_write_result"},
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_hashes(output_dir)
    return summary


def _derive_final_state(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    shutdown_event: dict[str, Any],
    *,
    operator_observation: str | None,
) -> dict[str, Any]:
    successful = [
        item
        for item in events
        if item.get("event") == "hardware_write_result"
        and item.get("success") is True
    ]
    flows: dict[str, float | None] = {"A": None, "B": None, "C": None}
    digital: dict[str, bool] = {}
    for item in successful:
        write = item["write"]
        if write["kind"] == "flow":
            flows[str(write["target"])] = float(write["value"])
        elif write["kind"] == "digital":
            digital[str(write["target"])] = bool(write["value"])
    selector_target = str(manifest["selector"]["target"])
    selector_requested = digital.get(selector_target)
    selector_attempts = [
        item
        for item in events
        if item.get("event") == "hardware_write_result"
        and item["write"]["kind"] == "digital"
        and item["write"]["target"] == selector_target
    ]
    selector_last_attempt = selector_attempts[-1] if selector_attempts else None
    selector_software = (
        "compensation"
        if shutdown_event.get("selector_safe_confirmed") is True
        else "unknown"
    )
    odors = {
        str(index): digital.get(target)
        for index, target in enumerate(manifest["odor_targets"], start=1)
    }
    return {
        "flows_sccm_from_confirmed_writes": flows,
        "selector": {
            "target": selector_target,
            "last_requested_level": None
            if selector_last_attempt is None
            else bool(selector_last_attempt["write"]["value"]),
            "delegate_success": None
            if selector_last_attempt is None
            else selector_last_attempt.get("delegate_success"),
            "exposed_success": None
            if selector_last_attempt is None
            else selector_last_attempt.get("success"),
            "uncertain": None
            if selector_last_attempt is None
            else selector_last_attempt.get("uncertain", False),
            "last_successful_requested_level": selector_requested,
            "requested_route": (
                "compensation"
                if selector_requested is False
                else "odor"
                if selector_requested is True
                else "not_written"
            ),
            "software_evidence": selector_software,
            "mechanical_observation": operator_observation or "not_provided",
        },
        "odor_valves_last_successful_requested_level": odors,
        "all_odors_requested_low": all(value is False for value in odors.values()),
        "electronic_ack_is_not_mechanical_evidence": True,
    }


def _scenario_assertions(
    scenario: str,
    events: list[dict[str, Any]],
    shutdown_event: dict[str, Any],
) -> dict[str, bool]:
    writes = [
        item
        for item in events
        if item.get("event") == "hardware_write_result"
    ]
    shutdown_a = next(
        (
            item
            for item in writes
            if item["write"]["phase"] == "shutdown"
            and item["write"]["kind"] == "flow"
            and item["write"]["target"] == "A"
        ),
        None,
    )
    selectors = [
        item
        for item in writes
        if item["write"]["phase"] == "shutdown"
        and item["write"]["kind"] == "digital"
        and item["write"]["target"] == "Dev2/P1.0"
    ]
    a_receipt_event = next(
        (item for item in events if item.get("event") == "a_zero_receipt"),
        None,
    )
    receipt = None if a_receipt_event is None else a_receipt_event.get("receipt")
    valid_a_receipt = bool(
        receipt
        and receipt.get("success") is True
        and receipt.get("stale") is False
        and receipt.get("command_id")
        and receipt.get("source") == "safety:safe-stop"
        and receipt.get("mode") == "safe_stop_a_zero"
        and math.isfinite(float(receipt.get("confirmed_a")))
        and abs(float(receipt.get("confirmed_a"))) <= 1e-9
    )
    a_fault = scenario in {
        "a_zero_failure",
        "a_zero_timeout",
        "stale_a_receipt",
        "late_a_receipt",
    }
    a_before_selector = bool(
        shutdown_a is not None
        and a_receipt_event is not None
        and valid_a_receipt
        and selectors
        and shutdown_a["sequence"] < a_receipt_event["sequence"] < selectors[0]["sequence"]
    )
    shutdown_odors = [
        item
        for item in writes
        if item["write"]["phase"] == "shutdown"
        and item["write"]["kind"] == "digital"
        and item["write"]["target"] != "Dev2/P1.0"
    ]
    terminal_flows = [
        item
        for item in writes
        if item["write"]["phase"] == "shutdown"
        and item["write"]["kind"] == "flow"
        and item["write"]["target"] in {"B", "C", "A"}
        and item is not shutdown_a
    ]
    closeout_order = bool(
        len(shutdown_odors) == 20
        and len(terminal_flows) == 3
        and max(item["sequence"] for item in shutdown_odors)
        < min(item["sequence"] for item in terminal_flows)
    )
    expected_result = "success" if scenario == "normal" else "recovery_required"
    return {
        "expected_shutdown_result": shutdown_event.get("result") == expected_result,
        "a_fault_never_writes_selector": (
            len(selectors) == 0 if a_fault else True
        ),
        "selector_write_at_most_once": len(selectors) <= 1,
        "valid_a_write_precedes_selector": (
            a_before_selector if not a_fault else True
        ),
        "valid_a_receipt_precedes_selector": (
            a_before_selector if not a_fault else not valid_a_receipt
        ),
        "odor_close_precedes_terminal_zero_all": closeout_order,
    }


def _write_event_index(
    path: Path,
    events: list[dict[str, Any]],
    names: set[str],
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in events:
            if item.get("event") in names:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def _write_hashes(output_dir: Path) -> None:
    paths = sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.name != "hashes.sha256"
    )
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in paths
    ]
    (output_dir / "hashes.sha256").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _verify_hashes(output_dir: Path) -> None:
    for line in (output_dir / "hashes.sha256").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        actual = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"证据文件哈希不匹配：{name}")


def _output_dir(root: Path, scenario: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return root / f"story45-{scenario}-{stamp}-{os.getpid()}"


def _inside_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return True


def _command_plan(args: argparse.Namespace) -> int:
    if _inside_project(args.output):
        raise ValueError("manifest 必须写到仓库外，避免污染候选工作树。")
    config = load_effective_config(args.config, args.local_config)
    manifest = build_manifest(
        args.scenario,
        candidate=candidate_snapshot(require_clean=True),
        config=config,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"manifest={args.output}")
    print(f"authorization_token={manifest['authorization_token']}")
    return 0


def _command_preflight(args: argparse.Namespace) -> int:
    if args.confirm != "I-CONFIRM-READ-ONLY":
        raise ValueError("只读 preflight 确认字符串不匹配。")
    config = load_effective_config(args.config, args.local_config)
    result = read_only_preflight(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"preflight={args.output}")
    return 0


def _command_run(args: argparse.Namespace) -> int:
    if _inside_project(args.output_root):
        raise ValueError("HIL 证据目录必须位于仓库外。")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_manifest(manifest, args.authorization)
    config = load_effective_config(args.config, args.local_config)
    validate_effective_config(manifest, config)
    preflight = read_only_preflight(config)
    output_dir = _output_dir(args.output_root, str(manifest["scenario"]))
    summary = execute_live(
        manifest,
        output_dir=output_dir,
        config=config,
        preflight_result=preflight,
    )
    final_readback: dict[str, Any] = {}
    final_error = ""
    try:
        final_readback = read_only_preflight(config)
    except Exception as exc:
        final_error = repr(exc)
        print(
            "RECOVERY_REQUIRED：最终 A/B/C 只读核对失败，请立即关闭上游 Air。",
            file=sys.stderr,
            flush=True,
        )
    (output_dir / "final-readback.json").write_text(
        json.dumps(
            {"result": final_readback, "error": final_error},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    summary["final_readback"] = final_readback.get("alicat", {})
    summary["final_readback_error"] = final_error
    if final_error:
        summary["verification_passed"] = False
        summary["automated_verification_passed"] = False
    final_state_path = output_dir / "final-state.json"
    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    final_state["alicat_readback"] = summary["final_readback"]
    final_state["alicat_readback_error"] = final_error
    final_state_path.write_text(
        json.dumps(final_state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_hashes(output_dir)
    print(f"evidence={output_dir}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print("硬件动作已结束，请现在关闭上游 Air。", flush=True)
    if manifest["scenario"] == "normal" and not final_error:
        print(
            "然后报告阀 2 观察结果：持续气流 / 短促气流 / 无气流；记录前验证保持 pending。",
            flush=True,
        )
    return 0 if summary["verification_passed"] else 2


def _command_record_observation(args: argparse.Namespace) -> int:
    output_dir = args.evidence_dir.resolve()
    if _inside_project(output_dir):
        raise ValueError("HIL 证据目录必须位于仓库外。")
    _verify_hashes(output_dir)
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("scenario") != "normal":
        raise ValueError("机械气流观察只适用于 normal 场景。")
    if not summary.get("automated_verification_passed"):
        raise ValueError("自动化安全核验未通过，不能用人工观察覆盖。")
    observation = str(args.observation)
    if observation not in ALLOWED_OPERATOR_OBSERVATIONS:
        raise ValueError("观察结果不受支持。")
    passed = observation == "持续气流"
    observation_payload = {
        "observation": observation,
        "expected": "持续气流",
        "passed": passed,
        "recorded_wall_timestamp": time.time(),
        "hardware_access": False,
    }
    (output_dir / "operator-observation.json").write_text(
        json.dumps(observation_payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary["operator_observation"] = observation
    summary["operator_observation_passed"] = passed
    summary["verification_passed"] = bool(passed)
    final_state_path = output_dir / "final-state.json"
    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    final_state["selector"]["mechanical_observation"] = observation
    final_state_path.write_text(
        json.dumps(final_state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary["final_state"] = final_state
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_hashes(output_dir)
    print(json.dumps(observation_payload, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Story 4.5 真实硬件 HIL：单场景、manifest 逐写授权。"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "default_config.json",
    )
    parser.add_argument(
        "--local-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "local_config.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="生成 manifest；不导入硬件驱动。")
    plan.add_argument("--scenario", choices=SCENARIOS, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(func=_command_plan)

    preflight = subparsers.add_parser("preflight", help="只读查询 NI/Alicat。")
    preflight.add_argument("--confirm", required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.set_defaults(func=_command_preflight)

    run = subparsers.add_parser("run", help="执行一个已授权 live 场景。")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--authorization", required=True)
    run.add_argument(
        "--output-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "OlfactoryPilot-HIL",
    )
    run.set_defaults(func=_command_run)

    observation = subparsers.add_parser(
        "record-observation",
        help="安全停止后记录人工气流观察；不导入硬件驱动。",
    )
    observation.add_argument("--evidence-dir", type=Path, required=True)
    observation.add_argument(
        "--observation",
        choices=ALLOWED_OPERATOR_OBSERVATIONS,
        required=True,
    )
    observation.set_defaults(func=_command_record_observation)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
