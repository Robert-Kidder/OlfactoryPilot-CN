from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import statistics
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QApplication

from app.main import load_effective_config
from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    AppState,
    ProtocolExecutionSnapshot,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    SelfCheckResult,
)
from app.services import ActuationDOAdapter, ActuationMetrics, MockHAL, RealHAL, SafetyManager
from app.services.hardware_check_service import HardwareCheckService
from app.services.valve_service import ValveService
from app.views.protocol_view import ProtocolView
from app.workers import ActuationInterlockIngress, ActuationWorker, HardwareWorker, InterlockSnapshot

LIVE_CONFIRMATION = "I_AUTHORIZE_LIVE_NI_HIL"


class ReceiptCollector:
    def __init__(self, jsonl_path: Path) -> None:
        self._condition = threading.Condition()
        self._receipts: list[ActuationReceipt] = []
        self._jsonl_path = jsonl_path
        self.inject_delay_ms = 0.0

    @property
    def receipts(self) -> list[ActuationReceipt]:
        with self._condition:
            return list(self._receipts)

    def wrap(self, adapter: ActuationDOAdapter):
        def writer(command: ActuationCommand) -> ActuationReceipt:
            delay_ms = self.inject_delay_ms
            if delay_ms and command.category == ActuationCategory.NORMAL:
                self.inject_delay_ms = 0.0
                time.sleep(delay_ms / 1000.0)
            receipt = adapter.execute(command)
            payload = asdict(receipt)
            payload["action"] = receipt.action.value
            payload["category"] = receipt.category.value
            payload["result"] = receipt.result.value
            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            with self._condition:
                self._receipts.append(receipt)
                self._condition.notify_all()
            return receipt

        writer.hal = adapter.hal
        return writer

    def wait_for(self, predicate, timeout_s: float, pump) -> ActuationReceipt:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pump()
            with self._condition:
                match = next((item for item in self._receipts if predicate(item)), None)
                if match is not None:
                    return match
                self._condition.wait(min(0.01, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("等待动作回执超时")


class AIOnlyHal:
    """Expose only HardwareWorker-owned AI calls; serial and DO stay elsewhere."""

    def __init__(self, hal, airflow: float) -> None:
        self._hal = hal
        self._airflow = float(airflow)

    @property
    def ttl_input_ready(self) -> bool:
        return bool(getattr(self._hal, "ttl_input_ready", False))

    def read_ai_frames(self, timestamp=None):
        return self._hal.read_ai_frames(timestamp)

    def reset_ai_input(self) -> None:
        self._hal.reset_ai_input()

    def read_flow(self) -> float:
        return self._airflow

    def flush_logs(self) -> None:
        return None


class PassingCheck:
    def run_checks(self):
        return [
            SelfCheckResult(
                name="hil_preflight",
                type="hil",
                status="PASS",
                reason="NI/MFC preflight completed before runtime ownership handoff",
                suggestion="none",
                checked_at=time.time(),
            )
        ], True


def nearest_rank_p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def load_config(args) -> dict:
    local = args.local_config if args.local_config and args.local_config.exists() else None
    config = load_effective_config(args.config, local)
    if args.live:
        config["hal_mode"] = "real"
        config["serial_port"] = args.serial_port
        config["ni_devices"] = ["Dev1", "Dev2"]
    return config


def enumerate_devices() -> list[dict]:
    from nidaqmx.system import System

    return [
        {
            "name": device.name,
            "product_type": device.product_type,
            "serial_num": int(device.serial_num),
        }
        for device in System.local().devices
    ]


def preflight(config: dict, live: bool):
    if not live:
        return MockHAL(), [{"name": "mock", "product_type": "MockHAL"}], 1000.0, []
    checks, ready = HardwareCheckService.from_config(config).run_checks()
    devices = enumerate_devices()
    if not ready:
        reasons = "; ".join(f"{item.name}: {item.reason}" for item in checks)
        raise RuntimeError(f"硬件连接自检失败：{reasons}")
    names = {item["name"] for item in devices}
    if not {"Dev1", "Dev2"}.issubset(names):
        raise RuntimeError(f"NI 映射不完整，实际设备为：{sorted(names)}")
    hal = RealHAL.from_config(config)
    airflow = float(hal.read_flow())
    if not math.isfinite(airflow):
        raise RuntimeError("MFC 返回了非有限气流读数")
    hal.release_serial_resources()
    return hal, devices, airflow, [asdict(item) for item in checks]


class Runtime:
    def __init__(self, *, config: dict, hal, airflow: float, output_dir: Path) -> None:
        self.config = config
        self.hal = hal
        self.output_dir = output_dir
        self.app = QApplication.instance() or QApplication([])
        self.view = ProtocolView()
        self.state = AppState.from_config(config)
        self.state.hardware_ready = True
        self.state.flow_setpoints_ready = True
        self.state.telemetry.connected = True
        self.state.telemetry.safety_state = "SAFE"
        self.state.telemetry.airflow = airflow
        self.state.telemetry.timestamp = time.time()
        self.safety = SafetyManager(float(config.get("low_flow_threshold", 0.2)))
        self.valves = ValveService(
            state=self.state,
            safety_manager=self.safety,
            worker=SimpleNamespace(is_connected=True),
            valve_variants=self.state.valve_variants,
            hardware_variant=self.state.hardware_variant,
            master_valve_line=self.state.master_valve_line,
        )
        self.ingress = ActuationInterlockIngress(
            InterlockSnapshot(
                connected=True,
                hardware_ready=True,
                flow_setpoints_ready=True,
                safety_state="SAFE",
                ttl_input_ready=False,
                has_protocol=True,
                device_lease="protocol",
            ),
            safety_manager=self.safety,
        )
        self.protocol_state = ProtocolExecutionState(
            status=ProtocolExecutionStatus.WAITING_TRIGGER,
            execution_epoch=1,
            arm_epoch=1,
        )
        self.metrics = ActuationMetrics(config)
        self.collector = ReceiptCollector(output_dir / "receipts.jsonl")
        self.adapter = ActuationDOAdapter(
            hal=hal,
            target_resolver=self.valves.resolve_target,
            write_timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
        )
        self.actuation = ActuationWorker(
            protocol_state=self.protocol_state,
            writer=self.collector.wrap(self.adapter),
            interlock=self.ingress,
            metrics=self.metrics,
            valve_service=self.valves,
            normal_queue_capacity=int(config.get("actuation_normal_queue_capacity", 256)),
        )
        self.hardware = HardwareWorker(
            telemetry_hz=5,
            breath_hz=100,
            ttl_config=config,
            check_service=PassingCheck(),
            hal=AIOnlyHal(hal, airflow),
            simulation=False,
        )
        self.hardware.set_actuation_sink(self.actuation, interlock_ingress=self.ingress)
        self.sequence = 0
        self._last_ui_ns = 0

    def start(self) -> None:
        self.actuation.start()
        self.hardware.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self.pump()
            if (
                self.actuation.isRunning()
                and not self.actuation._do_handed_off
                and self.hardware.isRunning()
                and getattr(self.hal, "_ai_epoch", 1) >= 1
            ):
                return
            time.sleep(0.01)
        raise RuntimeError("AI/DO owner threads did not become ready")

    def pump(self) -> None:
        self.app.processEvents()
        now_ns = time.perf_counter_ns()
        if now_ns - self._last_ui_ns < 50_000_000:
            return
        self._last_ui_ns = now_ns
        quality = self.metrics.snapshot()
        self.view.render_execution_state(
            ProtocolExecutionSnapshot(
                status=self.protocol_state.status,
                status_text="HIL 运行中",
                has_protocol=True,
                can_start=False,
                can_stop=True,
                can_advance=False,
                trial_label="HIL",
                trial_id="benchmark",
                last_jitter_ms=quality.last_jitter_ms,
                p95_open_ms=quality.open.p95_ms,
                p95_close_ms=quality.close.p95_ms,
                p95_combined_ms=quality.combined.p95_ms,
                sample_count_open=quality.open.sample_count,
                sample_count_close=quality.close.sample_count,
                sample_count_combined=quality.combined.sample_count,
                quality_block_reason=self.protocol_state.quality_block_reason,
            )
        )

    def wait_ms(self, milliseconds: float) -> None:
        deadline = time.monotonic() + milliseconds / 1000.0
        while time.monotonic() < deadline:
            self.pump()
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    def command(
        self,
        *,
        valve: int,
        action: ActuationAction,
        category: ActuationCategory,
        trial_id: str,
        duration_ms: float | None = None,
        lead_ms: float = 5.0,
        target_device: str | None = None,
        target_line: str | None = None,
    ) -> ActuationCommand:
        self.sequence += 1
        return ActuationCommand(
            command_id=f"hil-{trial_id}-{action.value}-{self.sequence}",
            execution_epoch=self.protocol_state.execution_epoch,
            arm_epoch=self.protocol_state.arm_epoch,
            sequence=self.sequence,
            trial_id=trial_id,
            trial_index=self.sequence,
            valve=valve,
            action=action,
            category=category,
            expected_ns=time.perf_counter_ns() + int(lead_ms * 1_000_000),
            duration_ns=None if duration_ms is None else int(duration_ms * 1_000_000),
            wall_timestamp=time.time(),
            safety_generation=self.ingress.read()[0],
            target_device=target_device,
            target_line=target_line,
        )

    def submit_and_wait(self, command: ActuationCommand, timeout_s: float = 2.0) -> ActuationReceipt:
        if not self.actuation.submit(command):
            raise RuntimeError(f"动作入队失败：{command.command_id}")
        receipt = self.collector.wait_for(
            lambda item: item.command_id == command.command_id,
            timeout_s,
            self.pump,
        )
        if receipt.result != ActuationResult.SUCCESS:
            raise RuntimeError(f"动作失败：{receipt.command_id}: {receipt.result}: {receipt.message}")
        return receipt

    def close_everything(self, label: str) -> list[ActuationReceipt]:
        commands = []
        for step in self.valves.emergency_close_steps():
            command = self.command(
                valve=step.logical_valve,
                action=ActuationAction.CLOSE,
                category=ActuationCategory.SAFETY,
                trial_id=label,
                lead_ms=0,
                target_device=step.device,
                target_line=step.line,
            )
            commands.append(command)
            if not self.actuation.submit(command):
                raise RuntimeError(f"安全关闭未能入队：{step.logical_valve}")
        receipts = []
        for command in commands:
            receipt = self.collector.wait_for(
                lambda item, cid=command.command_id: item.command_id == cid,
                3.0,
                self.pump,
            )
            if receipt.result != ActuationResult.SUCCESS:
                raise RuntimeError(f"安全关闭失败：{receipt.command_id}: {receipt.message}")
            receipts.append(receipt)
        return receipts

    def stop(self) -> None:
        try:
            if self.actuation.isRunning():
                closed = self.actuation.emergency_close_all(
                    int(self.config.get("actuation_emergency_close_timeout_ms", 500)) * 4
                )
                if not closed:
                    raise RuntimeError("shutdown emergency close-all 未获得全部成功回执")
        finally:
            self.actuation.shutdown(int(self.config.get("actuation_shutdown_timeout_ms", 2000)))
            self.hardware.stop()
            releaser = getattr(self.hal, "release_serial_resources", None)
            if releaser is not None:
                releaser()


def wait_trial(runtime: Runtime, command: ActuationCommand) -> tuple[ActuationReceipt, ActuationReceipt]:
    opened = runtime.submit_and_wait(command)
    closed = runtime.collector.wait_for(
        lambda item: (
            item.action == ActuationAction.CLOSE
            and (
                (item.trial_id == command.trial_id and item.category == ActuationCategory.NORMAL)
                or (
                    item.valve == command.valve
                    and item.category == ActuationCategory.SAFETY
                    and item.expected_ns >= (opened.actual_ns or 0)
                )
            )
        ),
        2.0,
        runtime.pump,
    )
    if closed.category == ActuationCategory.SAFETY:
        raise RuntimeError(
            f"trial {command.trial_id} 触发安全补偿关闭；open jitter={opened.jitter_ms}ms"
        )
    if closed.result != ActuationResult.SUCCESS:
        raise RuntimeError(f"定时关闭失败：{closed.command_id}: {closed.message}")
    return opened, closed


def run_benchmark(runtime: Runtime, args) -> dict:
    initial_closes = runtime.close_everything("initial-close")
    master_device, master_line = runtime.valves.resolve_target(0)
    runtime.submit_and_wait(
        runtime.command(
            valve=0,
            action=ActuationAction.OPEN,
            category=ActuationCategory.WARMUP,
            trial_id="master-warmup",
            lead_ms=10,
            target_device=master_device,
            target_line=master_line,
        )
    )
    runtime.wait_ms(100)

    started = time.time()
    for index in range(args.cycles):
        valve = args.valves[index % len(args.valves)]
        command = runtime.command(
            valve=valve,
            action=ActuationAction.OPEN,
            category=ActuationCategory.NORMAL,
            trial_id=f"bench-{index + 1:04d}-v{valve}",
            duration_ms=args.duration_ms,
            lead_ms=args.lead_ms,
        )
        opened, closed = wait_trial(runtime, command)
        if (opened.jitter_ms or 0) > 30.0 or (closed.jitter_ms or 0) > 30.0:
            raise RuntimeError("正式 benchmark 发生单次 >30ms 严重超限，已停止")
        runtime.wait_ms(args.inter_trial_ms)

    bench = [
        item
        for item in runtime.collector.receipts
        if item.trial_id and item.trial_id.startswith("bench-")
    ]
    opens = [float(item.jitter_ms) for item in bench if item.action == ActuationAction.OPEN]
    closes = [float(item.jitter_ms) for item in bench if item.action == ActuationAction.CLOSE]
    combined = [float(item.jitter_ms) for item in bench]
    if len(opens) != args.cycles or len(closes) != args.cycles:
        raise RuntimeError(f"样本数不足：open={len(opens)}, close={len(closes)}")
    return {
        "initial_close_count": len(initial_closes),
        "cycles": args.cycles,
        "elapsed_s": time.time() - started,
        "open": summarize(opens),
        "close": summarize(closes),
        "combined": summarize(combined),
    }


def run_safety_scenarios(runtime: Runtime, args) -> dict:
    results = {}
    runtime.protocol_state.status = ProtocolExecutionStatus.WAITING_TRIGGER
    runtime.protocol_state.quality_block_reason = ""
    runtime.metrics.acknowledge_severe()

    stop_command = runtime.command(
        valve=args.valves[0],
        action=ActuationAction.OPEN,
        category=ActuationCategory.NORMAL,
        trial_id="safety-stop",
        duration_ms=500,
    )
    opened = runtime.submit_and_wait(stop_command)
    runtime.protocol_state.active_valve = opened.valve
    runtime.actuation.invalidate_execution(reason="HIL stop injection")
    stop_close = runtime.collector.wait_for(
        lambda item: (
            item.valve == opened.valve
            and item.category == ActuationCategory.SAFETY
            and item.expected_ns >= (opened.actual_ns or 0)
        ),
        2.0,
        runtime.pump,
    )
    results["stop"] = stop_close.result.value

    runtime.protocol_state.status = ProtocolExecutionStatus.WAITING_TRIGGER
    runtime.protocol_state.quality_block_reason = ""
    runtime.ingress.update(safety_state="SAFE")
    runtime.ingress.clear_unsafe_latch()
    low_command = runtime.command(
        valve=args.valves[1],
        action=ActuationAction.OPEN,
        category=ActuationCategory.NORMAL,
        trial_id="safety-low-flow",
        duration_ms=500,
    )
    opened = runtime.submit_and_wait(low_command)
    runtime.protocol_state.active_valve = opened.valve
    runtime.ingress.update(safety_state="LOW_FLOW")
    runtime.actuation.invalidate_execution(reason="HIL LOW_FLOW/readiness injection")
    low_close = runtime.collector.wait_for(
        lambda item: (
            item.valve == opened.valve
            and item.category == ActuationCategory.SAFETY
            and item.expected_ns >= (opened.actual_ns or 0)
        ),
        2.0,
        runtime.pump,
    )
    results["low_flow"] = low_close.result.value

    runtime.protocol_state.status = ProtocolExecutionStatus.WAITING_TRIGGER
    runtime.protocol_state.quality_block_reason = ""
    runtime.ingress.update(safety_state="SAFE")
    runtime.ingress.clear_unsafe_latch()
    runtime.metrics.acknowledge_severe()
    runtime.collector.inject_delay_ms = 35.0
    severe_command = runtime.command(
        valve=args.valves[2],
        action=ActuationAction.OPEN,
        category=ActuationCategory.NORMAL,
        trial_id="safety-severe",
        duration_ms=500,
    )
    severe_open = runtime.submit_and_wait(severe_command)
    severe_close = runtime.collector.wait_for(
        lambda item: (
            item.valve == severe_open.valve
            and item.category == ActuationCategory.SAFETY
            and item.expected_ns >= (severe_open.actual_ns or 0)
        ),
        2.0,
        runtime.pump,
    )
    if severe_open.jitter_ms is None or severe_open.jitter_ms <= 30.0:
        raise RuntimeError("severe 注入未产生 >30ms jitter")
    results["severe"] = {
        "open_jitter_ms": severe_open.jitter_ms,
        "close": severe_close.result.value,
        "latched": runtime.metrics.severe_latched,
    }

    shutdown_closes = runtime.close_everything("pre-shutdown-close")
    results["pre_shutdown_close_count"] = len(shutdown_closes)
    return results


def summarize(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p95_ms": nearest_rank_p95(values),
        "max_ms": max(values) if values else None,
        "mean_ms": statistics.fmean(values) if values else None,
        "failures": 0,
    }


def write_csv(path: Path, receipts: list[ActuationReceipt]) -> None:
    rows = []
    for receipt in receipts:
        row = asdict(receipt)
        row["action"] = receipt.action.value
        row["category"] = receipt.category.value
        row["result"] = receipt.result.value
        rows.append(row)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Story 3.4 live NI actuation HIL benchmark")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config/default_config.json")
    parser.add_argument("--local-config", type=Path, default=REPO_ROOT / "config/local_config.json")
    parser.add_argument("--serial-port", default="COM6")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--phase", choices=("preflight", "close", "all"), default="all")
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--duration-ms", type=float, default=100.0)
    parser.add_argument("--inter-trial-ms", type=float, default=250.0)
    parser.add_argument("--lead-ms", type=float, default=5.0)
    parser.add_argument("--valves", type=int, nargs="+", default=[1, 9, 13])
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "logs/benchmarks")
    args = parser.parse_args()
    if args.live and args.confirm != LIVE_CONFIRMATION:
        parser.error(f"live hardware requires --confirm {LIVE_CONFIRMATION}")
    if args.cycles < 1 or args.duration_ms <= 0 or args.inter_trial_ms < 250:
        parser.error("cycles/duration must be positive and inter-trial must be >=250ms")
    if args.valves != [1, 9, 13]:
        parser.error("Story 3.4 HIL requires representative valves 1 9 13")
    return args


def main() -> int:
    args = parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_root / f"story-3-4-{stamp}-{'live' if args.live else 'mock'}"
    output_dir.mkdir(parents=True, exist_ok=False)
    config = load_config(args)
    metadata = {
        "story": "3.4",
        "started_at": time.time(),
        "live": args.live,
        "authorization": "explicit user authorization received" if args.live else "mock smoke",
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "parameters": {
            "valves": args.valves,
            "duration_ms": args.duration_ms,
            "inter_trial_ms": args.inter_trial_ms,
            "cycles": args.cycles,
            "lead_ms": args.lead_ms,
            "ai0_external_signal": False,
            "ai6_external_signal": False,
            "gas_load": "clean/inert, odor-free, operator confirmed",
        },
    }
    runtime = None
    exit_code = 1
    try:
        hal, devices, airflow, checks = preflight(config, args.live)
        metadata.update({"devices": devices, "mfc_airflow": airflow, "checks": checks})
        if args.phase == "preflight":
            (output_dir / "summary.json").write_text(
                json.dumps({"metadata": metadata, "preflight": "passed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            exit_code = 0
            return exit_code
        runtime = Runtime(config=config, hal=hal, airflow=airflow, output_dir=output_dir)
        runtime.start()
        metadata["resource_groups"] = [
            {"device": key[0], "port": key[1]}
            for key in sorted(getattr(hal, "_do_sessions", {}))
        ]
        if args.phase == "close":
            closed = runtime.close_everything("authorized-close-check")
            (output_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "metadata": metadata,
                        "close_check": {
                            "count": len(closed),
                            "success": all(item.result == ActuationResult.SUCCESS for item in closed),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            exit_code = 0
            return exit_code
        benchmark = run_benchmark(runtime, args)
        safety = run_safety_scenarios(runtime, args)
        summary = {"metadata": metadata, "benchmark": benchmark, "safety": safety}
        target_met = all(
            benchmark[name]["p95_ms"] is not None and benchmark[name]["p95_ms"] < 20.0
            for name in ("open", "close", "combined")
        )
        required_samples = 200 if args.live else args.cycles
        summary["acceptance"] = {
            "p95_strictly_below_20ms": target_met,
            "sample_counts_complete": benchmark["open"]["count"] >= required_samples
            and benchmark["close"]["count"] >= required_samples,
            "no_action_failures": all(
                item.result == ActuationResult.SUCCESS for item in runtime.collector.receipts
            ),
            "external_ai_ttl_signal_limitation": True,
        }
        exit_code = 0 if target_met and summary["acceptance"]["sample_counts_complete"] else 2
        (output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        metadata["failure"] = f"{type(exc).__name__}: {exc}"
        (output_dir / "failure.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(metadata["failure"], file=sys.stderr)
    finally:
        if runtime is not None:
            try:
                runtime.stop()
            except Exception as exc:
                metadata["shutdown_failure"] = f"{type(exc).__name__}: {exc}"
                (output_dir / "shutdown-failure.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                exit_code = 1
            write_csv(output_dir / "receipts.csv", runtime.collector.receipts)
        metadata["finished_at"] = time.time()
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(output_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
