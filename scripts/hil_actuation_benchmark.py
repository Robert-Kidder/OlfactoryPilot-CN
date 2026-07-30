from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from app.main import load_effective_config
from app.models import (
    ActuationAction,
    ActuationCategory,
    ActuationCommand,
    ActuationReceipt,
    ActuationResult,
    AppState,
    ProtocolDocument,
    ProtocolExecutionReadiness,
    ProtocolExecutionSnapshot,
    ProtocolExecutionState,
    ProtocolExecutionStatus,
    ProtocolTrial,
    SelfCheckResult,
    TriggerMode,
)
from app.services import (
    ActuationDOAdapter,
    ActuationMetrics,
    FlowService,
    GatingService,
    MockHAL,
    ProtocolExecutor,
    RealHAL,
    SafetyManager,
    SessionFileService,
    ShutdownService,
)
from app.services.hardware_check_service import HardwareCheckService
from app.services.valve_service import ValveService
from app.views.protocol_view import ProtocolView
from app.workers import (
    ActuationInterlockIngress,
    ActuationWorker,
    FlowCommand,
    FlowWorker,
    HardwareWorker,
    InterlockSnapshot,
    RecorderReadinessLatch,
    SessionRecorderIngress,
    SessionWriterConfig,
    SessionWriterWorker,
)

LIVE_CONFIRMATION = "I_AUTHORIZE_LIVE_NI_HIL"


class ReceiptCollector:
    def __init__(self, jsonl_path: Path, *, latency_trace=None) -> None:
        self._condition = threading.Condition()
        self._receipts: list[ActuationReceipt] = []
        self._jsonl_path = jsonl_path
        self._latency_trace = latency_trace
        self.inject_delay_ms = 0.0

    @property
    def receipts(self) -> list[ActuationReceipt]:
        with self._condition:
            return list(self._receipts)

    def wrap(self, adapter: ActuationDOAdapter):
        def writer(command: ActuationCommand) -> ActuationReceipt:
            tracing = (
                self._latency_trace is not None
                and self._latency_trace.trial_label is not None
            )
            entered_ns = time.perf_counter_ns() if tracing else None
            if tracing:
                self._latency_trace.record(
                    "writer_enter",
                    at_ns=entered_ns,
                    command_id=command.command_id,
                    expected_ns=command.expected_ns,
                    action=command.action.value,
                    category=command.category.value,
                )
            delay_ms = self.inject_delay_ms
            if delay_ms and command.category == ActuationCategory.NORMAL:
                self.inject_delay_ms = 0.0
                time.sleep(delay_ms / 1000.0)
            receipt = adapter.execute(command)
            if tracing:
                self._latency_trace.record(
                    "writer_return",
                    command_id=command.command_id,
                    expected_ns=command.expected_ns,
                    writer_enter_ns=entered_ns,
                    hal_started_ns=receipt.started_ns,
                    hal_actual_ns=receipt.actual_ns,
                    result=receipt.result.value,
                )
            return receipt

        writer.hal = adapter.hal
        return writer

    def record(self, receipt: ActuationReceipt) -> None:
        with self._condition:
            self._receipts.append(receipt)
            self._condition.notify_all()

    def write_jsonl(self) -> None:
        """Persist the diagnostic copy only after the actuation owner has stopped."""
        with self._condition:
            receipts = list(self._receipts)
        with self._jsonl_path.open("w", encoding="utf-8") as handle:
            for receipt in receipts:
                payload = asdict(receipt)
                payload["action"] = receipt.action.value
                payload["category"] = receipt.category.value
                payload["result"] = receipt.result.value
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def wait_for(
        self,
        predicate,
        timeout_s: float,
        pump,
        *,
        after_index: int = 0,
    ) -> ActuationReceipt:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            pump()
            with self._condition:
                match = next(
                    (item for item in self._receipts[after_index:] if predicate(item)),
                    None,
                )
                if match is not None:
                    return match
                self._condition.wait(min(0.01, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("等待动作回执超时")


class LatencyTrace:
    """Optional in-memory side trace; never changes production timestamps."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        run_id: str = "",
        max_events: int = 50_000,
    ) -> None:
        self.enabled = bool(enabled)
        self.run_id = str(run_id)
        self.max_events = max(1, int(max_events))
        self._lock = threading.Lock()
        self._events: list[dict[str, object]] = []
        self._trial_label: str | None = None
        self._command_trials: dict[str, str] = {}
        self._dropped_events = 0

    @property
    def trial_label(self) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            if self._trial_label is not None:
                return self._trial_label
            labels = set(self._command_trials.values())
            return next(iter(labels)) if len(labels) == 1 else None

    def should_trace_command(self, command_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return (
                self._trial_label is not None
                or str(command_id) in self._command_trials
            )

    @property
    def events(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._events)

    def begin_trial(self, label: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._trial_label = str(label)
        self.record("trial_trace_begin")

    def end_trial(self) -> None:
        if not self.enabled:
            return
        self.record("trial_trace_end")
        with self._lock:
            self._trial_label = None

    def record(self, event: str, *, at_ns: int | None = None, **fields) -> None:
        if not self.enabled:
            return
        event_ns = time.perf_counter_ns() if at_ns is None else int(at_ns)
        with self._lock:
            command_id = str(fields.get("command_id") or "")
            label = self._trial_label
            if (
                label is not None
                and event == "actuation_submit_return"
                and command_id
                and fields.get("accepted") is True
            ):
                self._command_trials[command_id] = label
            if label is None and command_id:
                label = self._command_trials.get(command_id)
            if label is None:
                labels = set(self._command_trials.values())
                if len(labels) == 1:
                    label = next(iter(labels))
            if label is None:
                return
            if len(self._events) >= self.max_events:
                self._dropped_events += 1
                if event == "writer_return" and command_id:
                    self._command_trials.pop(command_id, None)
                return
            self._events.append(
                {
                    "schema": "story-3.4.hil-latency.v1",
                    "run_id": self.run_id,
                    "event": str(event),
                    "at_ns": event_ns,
                    "trial_label": label,
                    "thread_id": threading.get_ident(),
                    **fields,
                }
            )
            if event == "writer_return" and command_id:
                self._command_trials.pop(command_id, None)

    def write_jsonl(self, path: Path) -> None:
        if not self.enabled:
            return
        with self._lock:
            events = list(self._events)
            dropped_events = self._dropped_events
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.write(
                json.dumps(
                    {
                        "schema": "story-3.4.hil-latency.v1",
                        "run_id": self.run_id,
                        "event": "trace_complete",
                        "event_count": len(events),
                        "dropped_events": dropped_events,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


class AIOnlyHal:
    """Expose only HardwareWorker-owned AI calls; serial and DO stay elsewhere."""

    def __init__(
        self,
        hal,
        airflow: float,
        *,
        monotonic_ns_clock: Callable[[], int] = time.perf_counter_ns,
        latency_trace: LatencyTrace | None = None,
    ) -> None:
        self._hal = hal
        self._airflow = float(airflow)
        self._monotonic_ns_clock = monotonic_ns_clock
        self._latency_trace = latency_trace
        self._override_lock = threading.Lock()
        self._ai0_override: float | None = None
        self._ai0_override_started_ns: int | None = None

    @property
    def ttl_input_ready(self) -> bool:
        return bool(getattr(self._hal, "ttl_input_ready", False))

    def read_ai_frames(self, timestamp=None):
        tracing = (
            self._latency_trace is not None
            and self._latency_trace.trial_label is not None
        )
        read_started_ns = time.perf_counter_ns() if tracing else 0
        frames = self._hal.read_ai_frames(timestamp)
        read_returned_ns = time.perf_counter_ns() if tracing else 0
        with self._override_lock:
            override = self._ai0_override
            override_started_ns = self._ai0_override_started_ns
        overridden = []
        if override is None or override_started_ns is None:
            if tracing:
                self._trace_ai_read(
                    frames,
                    read_started_ns=read_started_ns,
                    read_returned_ns=read_returned_ns,
                    override_started_ns=None,
                    overridden=overridden,
                )
            return frames
        # A continuous NI task may return buffered frames acquired before the
        # software stimulus began.  Never rewrite those historical samples or
        # the benchmark would backdate its expected actuation timestamp.
        result = [
            replace(frame, ai0=override)
            if frame.monotonic_ns >= override_started_ns
            else frame
            for frame in frames
        ]
        overridden = [
            frame.sample_sequence
            for frame in frames
            if frame.monotonic_ns >= override_started_ns
        ]
        if self._latency_trace is not None and overridden:
            self._latency_trace.record(
                "software_override_applied",
                external_signal=False,
                override_started_ns=override_started_ns,
                frame_sequences=overridden,
                frame_monotonic_ns=[
                    int(frame.monotonic_ns)
                    for frame in frames
                    if frame.monotonic_ns >= override_started_ns
                ],
            )
        if tracing:
            self._trace_ai_read(
                frames,
                read_started_ns=read_started_ns,
                read_returned_ns=read_returned_ns,
                override_started_ns=override_started_ns,
                overridden=overridden,
            )
        return result

    def set_ai0_software_stimulus(self, value: float | None) -> None:
        """Apply an explicit non-external AI0 stimulus at the HAL read boundary."""
        changed_ns = self._monotonic_ns_clock()
        with self._override_lock:
            if value is None:
                self._ai0_override = None
                self._ai0_override_started_ns = None
            else:
                self._ai0_override = float(value)
                self._ai0_override_started_ns = changed_ns
        if self._latency_trace is not None:
            self._latency_trace.record(
                "stimulus_clear" if value is None else "stimulus_set",
                at_ns=changed_ns,
                external_signal=False,
                stimulus_value=None if value is None else float(value),
            )

    def _trace_ai_read(
        self,
        frames,
        *,
        read_started_ns: int,
        read_returned_ns: int,
        override_started_ns: int | None,
        overridden: list[int],
    ) -> None:
        trace = self._latency_trace
        if trace is None or trace.trial_label is None:
            return
        trace.record(
            "ai_read_return",
            at_ns=read_returned_ns,
            read_started_ns=read_started_ns,
            read_returned_ns=read_returned_ns,
            frame_count=len(frames),
            frame_monotonic_ns=[int(frame.monotonic_ns) for frame in frames],
            frame_sequences=[int(frame.sample_sequence) for frame in frames],
            frame_epochs=[int(frame.ai_epoch) for frame in frames],
            frame_origin_uncertainty_ns=[
                int(frame.origin_uncertainty_ns) for frame in frames
            ],
            override_started_ns=override_started_ns,
            overridden_sequences=overridden,
            oldest_frame_delivery_age_ms=(
                None
                if not frames
                else (read_returned_ns - int(frames[0].monotonic_ns)) / 1_000_000
            ),
            newest_frame_delivery_age_ms=(
                None
                if not frames
                else (read_returned_ns - int(frames[-1].monotonic_ns)) / 1_000_000
            ),
        )

    def reset_ai_input(self) -> bool:
        return self._hal.reset_ai_input() is True

    def read_flow(self) -> float:
        raise AssertionError("HardwareWorker must not access the serial airflow owner")

    def flush_logs(self) -> None:
        return None

    def stop_heaters(self) -> bool:
        return bool(self._hal.stop_heaters())


class TracingActuationWorker(ActuationWorker):
    """HIL-only side tracing around the unchanged ActuationWorker behavior."""

    def __init__(self, *args, latency_trace: LatencyTrace, **kwargs) -> None:
        self._latency_trace = latency_trace
        super().__init__(*args, **kwargs)

    @staticmethod
    def _batch_identity(batch) -> dict[str, object]:
        samples = tuple(getattr(batch, "samples", ()))
        return {
            "sample_count": len(samples),
            "sample_monotonic_ns": [int(sample.monotonic_ns) for sample in samples],
            "sample_sequences": [int(sample.sample_sequence) for sample in samples],
            "sample_epochs": [int(sample.ai_epoch) for sample in samples],
        }

    def post_ai_batch(self, batch, *, readiness=None) -> None:
        entered_ns = time.perf_counter_ns()
        self._latency_trace.record(
            "hardware_post_ai_batch_enter",
            at_ns=entered_ns,
            **self._batch_identity(batch),
        )
        super().post_ai_batch(batch, readiness=readiness)
        self._latency_trace.record(
            "hardware_post_ai_batch_return",
            entered_ns=entered_ns,
            **self._batch_identity(batch),
        )

    def _handle_message(self, kind: str, payload: dict) -> None:
        self._latency_trace.record(
            "actuation_dequeue_message",
            message_kind=kind,
            normal_queue_size=self.normal_queue_size,
            emergency_queue_size=self.emergency_queue_size,
        )
        if kind == "ai_batch":
            self._latency_trace.record(
                "actuation_dequeue_ai_batch",
                **self._batch_identity(payload["batch"]),
            )
        super()._handle_message(kind, payload)

    def submit(self, command: ActuationCommand) -> bool:
        entered_ns = time.perf_counter_ns()
        accepted = super().submit(command)
        self._latency_trace.record(
            "actuation_submit_return",
            command_id=command.command_id,
            expected_ns=command.expected_ns,
            action=command.action.value,
            category=command.category.value,
            entered_ns=entered_ns,
            accepted=accepted,
        )
        return accepted

    def _execute(self, command: ActuationCommand) -> None:
        entered_ns = time.perf_counter_ns()
        self._latency_trace.record(
            "actuation_execute_enter",
            at_ns=entered_ns,
            command_id=command.command_id,
            expected_ns=command.expected_ns,
            action=command.action.value,
            category=command.category.value,
            dispatch_lateness_ms=(entered_ns - command.expected_ns) / 1_000_000,
            normal_queue_size=self.normal_queue_size,
            emergency_queue_size=self.emergency_queue_size,
        )
        super()._execute(command)


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


def preflight(config: dict, live: bool, *, require_flow: bool = True):
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
    airflow = 0.0
    try:
        if require_flow:
            airflow = float(hal.read_flow())
            threshold = float(config.get("low_flow_threshold", 0.2))
            if not math.isfinite(airflow):
                raise RuntimeError("MFC 返回了非有限气流读数")
            if not math.isfinite(threshold) or airflow <= threshold:
                raise RuntimeError(
                    f"MFC 气流未达到安全阈值：{airflow:.6g} <= {threshold:.6g}"
                )
    finally:
        hal.release_serial_resources()
    return hal, devices, airflow, [asdict(item) for item in checks]


class Runtime:
    def __init__(
        self,
        *,
        config: dict,
        hal,
        airflow: float,
        output_dir: Path,
        protocol_mode: bool = False,
        collector: ReceiptCollector | None = None,
        latency_trace: bool = False,
        story_35_recording: bool = False,
        live: bool = False,
    ) -> None:
        self.config = config
        self.protocol_mode = bool(protocol_mode)
        self.airflow = float(airflow)
        self.hal = hal
        self.output_dir = output_dir
        self._story_35_recording_enabled = bool(story_35_recording)
        self._live = bool(live)
        self.latency_trace = LatencyTrace(
            enabled=latency_trace,
            run_id=output_dir.name,
        )
        self.app = QApplication.instance() or QApplication([])
        self.view = ProtocolView()
        self.state = AppState.from_config(config)
        self.state.hardware_ready = True
        self.state.flow_setpoints_ready = True
        self.state.telemetry.connected = True
        self.state.telemetry.safety_state = "DATA_STALE"
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
                safety_state="DATA_STALE",
                ttl_input_ready=False,
                has_protocol=True,
                device_lease="protocol",
            ),
            safety_manager=self.safety,
        )
        self.executor = None
        if protocol_mode:
            self.executor = ProtocolExecutor(
                gating_service=GatingService(
                    inhale_threshold=float(config.get("inhale_threshold", 0.47)),
                    exhale_threshold=float(config.get("exhale_threshold", -0.44)),
                ),
                valve_writer=lambda *_: (_ for _ in ()).throw(
                    AssertionError("synchronous protocol DO is forbidden")
                ),
                config=config,
                deferred_actuation=True,
            )
            self.protocol_state = self.executor.state
        else:
            self.protocol_state = ProtocolExecutionState(
                status=ProtocolExecutionStatus.WAITING_TRIGGER,
                execution_epoch=1,
                arm_epoch=1,
            )
        self.metrics = ActuationMetrics(config)
        self.collector = collector or ReceiptCollector(
            output_dir / "receipts.jsonl",
            latency_trace=self.latency_trace if latency_trace else None,
        )
        self.adapter = ActuationDOAdapter(
            hal=hal,
            target_resolver=self.valves.resolve_target,
            write_timeout_ms=int(config.get("actuation_write_timeout_ms", 100)),
        )
        self.flow_service = FlowService(hal, master_target=None, master_writer=None)
        self.flow = FlowWorker(
            self.flow_service,
            airflow_poll_interval_s=1.0 / max(1.0, float(config.get("telemetry_hz", 5))),
        )
        actuation_type = TracingActuationWorker if latency_trace else ActuationWorker
        actuation_kwargs = {
            "protocol_state": None if self.executor is not None else self.protocol_state,
            "protocol_executor": self.executor,
            "writer": self.collector.wrap(self.adapter),
            "interlock": self.ingress,
            "metrics": self.metrics,
            "valve_service": self.valves,
            "flow_submitter": self.flow.submit,
            "normal_queue_capacity": int(
                config.get("actuation_normal_queue_capacity", 256)
            ),
        }
        if latency_trace:
            actuation_kwargs["latency_trace"] = self.latency_trace
        self.actuation = actuation_type(
            **actuation_kwargs,
        )
        self.actuation.receipt_ready.connect(self.collector.record)
        self.ai_hal = AIOnlyHal(
            hal,
            airflow,
            latency_trace=self.latency_trace if latency_trace else None,
        )
        self.hardware = HardwareWorker(
            telemetry_hz=5,
            breath_hz=100,
            ttl_config=config,
            check_service=PassingCheck(),
            hal=self.ai_hal,
            simulation=not self._live,
        )
        self.hardware.telemetry_ready.connect(self.state.update_telemetry)
        self.hardware.set_actuation_sink(self.actuation, interlock_ingress=self.ingress)
        self.flow.set_airflow_sink(self.hardware.consume_airflow_sample)
        self._flow_restore_confirmed = not self.protocol_mode
        self._hil_flow_results = {}
        self.flow.result_ready.connect(self._handle_hil_flow_result)
        self.flow.result_ready.connect(self.actuation.post_flow_result)
        self.actuation.flow_result_ready.connect(self._handle_hil_flow_result)
        self.sequence = 0
        self._last_ui_ns = 0
        self._shutdown_completed = False
        self._abort_close_confirmed = False
        self._story_35_file_service = None
        self._story_35_writer = None
        self._story_35_ingress = None
        self._story_35_descriptor = None
        self._story_35_controller_sequence = 0
        self._story_35_bound_document = None
        self._story_35_generation = 0
        self._story_35_candidate_commit = ""
        self._story_35_run_id = ""
        self._story_35_live = False
        self._story_35_bundle_results: list[dict] = []

    def _handle_hil_flow_result(self, wrapped) -> None:
        self._hil_flow_results[wrapped.command.source] = wrapped
        if wrapped.command.source == "safety:hil-restore" and wrapped.result.success:
            self._flow_restore_confirmed = True

    def start(self) -> None:
        if getattr(self, "_story_35_recording_enabled", False):
            self.view.show()
        self.actuation.start(QThread.Priority.HighPriority)
        self.flow.start()
        self.hardware.start(QThread.Priority.HighPriority)
        deadline = time.monotonic() + 8.0
        flow_restored = not self.protocol_mode
        stable_since = None
        while time.monotonic() < deadline:
            self.pump()
            if (
                not flow_restored
                and self.flow.isRunning()
                and bool(getattr(self.hardware, "_connected", False))
            ):
                self.sequence += 1
                flow_restored = self.flow.submit(
                    FlowCommand(
                        command_id=f"hil-restore-flow-{self.sequence}",
                        execution_epoch=0,
                        sequence=self.sequence,
                        mode="rest",
                        a=self.airflow,
                        b=0.0,
                        c=0.0,
                        source="safety:hil-restore",
                    )
                )
            interlock_snapshot = self.ingress.read()[1]
            if (
                self.actuation.isRunning()
                and not self.actuation._do_handed_off
                and self.hardware.isRunning()
                and self.flow.isRunning()
                and getattr(self.hal, "_ai_epoch", 1) >= 1
                and not interlock_snapshot.unsafe_reason()
                and flow_restored
                and self._flow_restore_confirmed
            ):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 0.3:
                    if not self.ingress.clear_unsafe_latch():
                        raise RuntimeError("preflight SAFE snapshot could not clear the interlock latch")
                    return
            else:
                stable_since = None
            time.sleep(0.01)
        raise RuntimeError("AI/DO owner threads did not become ready")

    def begin_story_35_recording(
        self,
        document: ProtocolDocument,
        *,
        candidate_commit: str,
        run_id: str,
        live: bool,
    ):
        if self._story_35_writer is not None:
            raise RuntimeError("Story 3.5 session recording 已经建立")
        self._story_35_generation = (
            getattr(self, "_story_35_generation", 0) + 1
        )
        self._story_35_candidate_commit = str(candidate_commit)
        self._story_35_run_id = str(run_id)
        self._story_35_live = bool(live)
        session_output = self.output_dir / "session-output"
        session_output.mkdir(parents=True, exist_ok=True)
        file_service = SessionFileService(
            master_valve_line=self.state.master_valve_line
        )
        protocol_metadata = {
            key: str(value)
            for key, value in dict(document.metadata).items()
        }
        protocol_metadata.update(
            {
                "story": "3.5",
                "candidate_commit": str(candidate_commit),
                "run_id": str(run_id),
            }
        )
        descriptor = file_service.reserve(
            output_dir=session_output,
            subject="HIL-NO-SUBJECT",
            condition="Story-3.5-Windows-NI",
            generation=self._story_35_generation,
            protocol_source=document.source_name,
            protocol_metadata=protocol_metadata,
        )
        readiness_latch = RecorderReadinessLatch()
        quality_config = self.metrics.config
        writer = SessionWriterWorker(
            descriptor=descriptor,
            config=SessionWriterConfig.from_mapping(self.config),
            expected_producers=("hardware", "actuation", "controller"),
            session_started_payload={
                "recording_started_at": datetime.now()
                .astimezone()
                .isoformat(timespec="milliseconds"),
                "declared_trigger_mode": "manual",
                "current_trigger_mode": "manual",
                "inhale_threshold": float(self.config.get("inhale_threshold", 0.47)),
                "exhale_threshold": float(self.config.get("exhale_threshold", -0.44)),
                "low_flow_threshold": float(
                    self.config.get("low_flow_threshold", 0.2)
                ),
                "hardware_variant": self.state.hardware_variant,
                "hardware_mode": "real" if live else "simulation",
                "ai_epoch_available": True,
                "candidate_commit": str(candidate_commit),
                "run_id": str(run_id),
                "subject_connected": False,
                "actuation_quality_config": {
                    "target_ms": quality_config.target_ms,
                    "single_limit_ms": quality_config.single_limit_ms,
                    "window_size": quality_config.window_size,
                    "min_samples": quality_config.min_samples,
                },
            },
            readiness_latch=readiness_latch,
            master_valve_line=self.state.master_valve_line,
            failure_callback=self._handle_story_35_writer_failure,
        )
        ingress = SessionRecorderIngress(writer, readiness_latch)
        self._story_35_file_service = file_service
        self._story_35_writer = writer
        self._story_35_ingress = ingress
        self._story_35_descriptor = descriptor
        self._story_35_bound_document = document
        self._story_35_controller_sequence = 0
        timeout_ms = SessionWriterConfig.from_mapping(self.config).close_timeout_ms
        if not writer.start_and_wait(timeout_ms):
            file_service.mark_inactive(descriptor.paths.staging_dir)
            raise RuntimeError("Story 3.5 SessionWriter 初始化失败")
        if not self.actuation.bind_session_recorder(
            ingress,
            generation=descriptor.generation,
            timeout_ms=timeout_ms,
        ):
            writer.fail_from_producer(
                stage="actuation_recorder_bind",
                message="动作 owner 未确认 Story 3.5 recorder bind。",
            )
            writer.wait(timeout_ms)
            file_service.mark_inactive(descriptor.paths.staging_dir)
            raise RuntimeError("动作 owner 未确认 Story 3.5 recorder bind")
        if not self.hardware.bind_session_recorder(
            ingress,
            generation=descriptor.generation,
            timeout_ms=timeout_ms,
        ):
            self.actuation.post_recorder_fence(wait=True, timeout_ms=timeout_ms)
            writer.fail_from_producer(
                stage="hardware_recorder_bind",
                message="采集 owner 未确认 Story 3.5 recorder bind。",
            )
            writer.wait(timeout_ms)
            file_service.mark_inactive(descriptor.paths.staging_dir)
            raise RuntimeError("采集 owner 未确认 Story 3.5 recorder bind")
        self.ingress.update(
            recording_ready=True,
            recorder_failed=False,
            recorder_generation=descriptor.generation,
            session_closing=False,
        )
        current_interlock = self.ingress.read()[1]
        if current_interlock.unsafe_reason():
            raise RuntimeError(
                "Story 3.5 recording bind 后安全状态不是 SAFE，禁止正式动作"
            )
        if not self.ingress.clear_unsafe_latch():
            raise RuntimeError(
                "Story 3.5 recording bind 后安全联锁无法重新布防，禁止正式动作"
            )
        if not self.actuation.post_recorder_ready(
            descriptor.generation,
            wait=True,
            timeout_ms=timeout_ms,
        ):
            raise RuntimeError(
                "动作 owner 未确认 Story 3.5 recording-ready generation"
            )
        self._story_35_controller_sequence += 1
        if not ingress.post_session_event(
            event="protocol_bound",
            producer_sequence=self._story_35_controller_sequence,
            source="session",
            result="success",
            message="Story 3.5 HIL 协议已绑定到会话。",
            payload={
                "protocol_source": document.source_name,
                "protocol_metadata": protocol_metadata,
            },
        ):
            raise RuntimeError("Story 3.5 protocol_bound 无法进入 recorder queue")
        return descriptor

    def _handle_story_35_writer_failure(self, failure) -> None:
        self.ingress.update(
            recording_ready=False,
            recorder_failed=True,
            recorder_generation=failure.session_generation,
        )
        self.actuation.post_recorder_failed(failure.message)
        self.actuation.post_stop(
            message="Story 3.5 会话写入失败，HIL runner 已请求安全停止。"
        )

    def finalize_story_35_recording(
        self,
        *,
        reason: str,
        aborted: bool,
        final_quality=None,
        fence_producers: bool = False,
    ):
        writer = self._story_35_writer
        ingress = self._story_35_ingress
        descriptor = self._story_35_descriptor
        file_service = self._story_35_file_service
        if writer is None or ingress is None or descriptor is None or file_service is None:
            raise RuntimeError("Story 3.5 session recording 尚未建立")
        self.ingress.update(recording_ready=False, session_closing=True)
        if fence_producers:
            self.hardware.post_session_fence()
            if not self.actuation.post_recorder_fence(
                wait=True,
                timeout_ms=SessionWriterConfig.from_mapping(
                    self.config
                ).close_timeout_ms,
            ):
                writer.fail_from_producer(
                    stage="actuation_recorder_fence",
                    message="动作 owner 未确认 Story 3.5 recorder fence。",
                )
        self._story_35_controller_sequence += 1
        ingress.post_session_event(
            event="hil_run_aborted" if aborted else "hil_run_completed",
            producer_sequence=self._story_35_controller_sequence,
            source="hil_runner",
            result="aborted" if aborted else "success",
            message=(
                "Story 3.5 HIL 已中止，安全全关后结束记录。"
                if aborted
                else "Story 3.5 HIL 已完成，安全全关后结束记录。"
            ),
            payload={"reason": str(reason)},
        )
        ingress.post_fence(
            "controller",
            producer_sequence=self._story_35_controller_sequence,
        )
        metrics = getattr(self, "metrics", None)
        if final_quality is None and metrics is not None:
            final_quality = metrics.snapshot()
        result = writer.close(
            reason=str(reason),
            final_quality=final_quality,
            timeout_ms=SessionWriterConfig.from_mapping(self.config).close_timeout_ms,
        )
        validation_path = (
            descriptor.paths.final_dir
            if descriptor.paths.final_dir.is_dir()
            else descriptor.paths.staging_dir
        )
        validation = file_service.validate_complete_bundle(validation_path)
        if not writer.isRunning():
            file_service.mark_inactive(descriptor.paths.staging_dir)
        bundle_result = {
            "protocol_source": (
                None
                if self._story_35_bound_document is None
                else self._story_35_bound_document.source_name
            ),
            "path": str(validation.path),
            "writer_complete": result.complete,
            "validator_complete": validation.complete,
            "validator_reason": validation.reason,
            "last_session_sequence": validation.last_sequence,
            "aborted": bool(aborted),
        }
        if not hasattr(self, "_story_35_bundle_results"):
            self._story_35_bundle_results = []
        self._story_35_bundle_results.append(bundle_result)
        self._story_35_file_service = None
        self._story_35_writer = None
        self._story_35_ingress = None
        self._story_35_descriptor = None
        self._story_35_bound_document = None
        self._story_35_controller_sequence = 0
        self.ingress.update(session_closing=False)
        return result, validation

    def _ensure_story_35_recording_for_document(
        self,
        document: ProtocolDocument,
    ) -> None:
        if not getattr(self, "_story_35_recording_enabled", False):
            return
        if self._story_35_writer is None:
            self.begin_story_35_recording(
                document,
                candidate_commit=self._story_35_candidate_commit,
                run_id=self._story_35_run_id,
                live=self._story_35_live,
            )
            return
        if self._story_35_bound_document is not document:
            raise RuntimeError(
                "Story 3.5 单 session 只能绑定一份协议；"
                "必须先完成当前 bundle 再加载安全场景协议。"
            )

    def pump(self) -> None:
        self.app.processEvents()
        now_ns = time.perf_counter_ns()
        if now_ns - self._last_ui_ns < 50_000_000:
            return
        self._last_ui_ns = now_ns
        quality = self.metrics.snapshot()
        self.latency_trace.record("ui_render_enter", at_ns=now_ns)
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
        self.latency_trace.record("ui_render_return")

    def wait_ms(self, milliseconds: float) -> None:
        deadline = time.monotonic() + milliseconds / 1000.0
        while time.monotonic() < deadline:
            self.pump()
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    def readiness(self) -> ProtocolExecutionReadiness:
        snapshot = self.ingress.read()[1]
        return ProtocolExecutionReadiness(
            connected=snapshot.connected,
            hardware_ready=snapshot.hardware_ready,
            flow_setpoints_ready=snapshot.flow_setpoints_ready,
            safety_state=snapshot.safety_state,
            ttl_input_ready=snapshot.ttl_input_ready,
        )

    def wait_status(
        self,
        statuses: set[ProtocolExecutionStatus],
        timeout_s: float = 2.0,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump()
            self.protocol_state = self.actuation.protocol_state
            if self.protocol_state.status in statuses:
                return
            time.sleep(0.005)
        raise TimeoutError(
            f"等待协议状态超时：current={self.actuation.protocol_state.status.value}, "
            f"expected={[item.value for item in statuses]}, "
            f"recent={getattr(self.actuation.protocol_state.recent_event, 'message', '')}, "
            f"events={[event.message for event in self.actuation.protocol_state.events[-3:]]}"
        )

    def recover_low_flow_via_owner(self, timeout_s: float = 3.0) -> None:
        """Restore MFC/readiness through the production serial and actuation owners."""
        close_deadline = time.monotonic() + timeout_s
        while time.monotonic() < close_deadline:
            self.pump()
            if (
                self.actuation.protocol_state.active_valve is None
                and not self.actuation.protocol_state.possibly_open_valves
            ):
                break
            time.sleep(0.01)
        else:
            raise TimeoutError("LOW_FLOW recovery did not first reach physically closed state")

        # MFC recovery is forbidden while a protocol owns the device lease.
        self.actuation.post_stop(message="HIL LOW_FLOW closed; release protocol flow lease")
        self.wait_status({ProtocolExecutionStatus.STOPPED}, timeout_s=timeout_s)
        epoch = self.actuation.protocol_state.execution_epoch
        held_epoch = self.flow.execution_context[0]
        if held_epoch is None or not self.flow.release_protocol_lease(
            held_epoch,
            next_execution_epoch=epoch,
        ):
            raise RuntimeError(
                "LOW_FLOW recovery could not release FlowWorker lease: "
                f"context={self.flow.execution_context}, epoch={epoch}"
            )
        self.ingress.update(device_lease="idle")
        source = f"safety:hil-low-recovery-{self.sequence + 1}"
        self._hil_flow_results.pop(source, None)
        self.actuation.post_flow_intent(
            mode="rest",
            a=self.airflow,
            b=0.0,
            c=0.0,
            source=source,
        )
        deadline = time.monotonic() + timeout_s
        stable_since = None
        while time.monotonic() < deadline:
            self.pump()
            wrapped = self._hil_flow_results.get(source)
            snapshot = self.ingress.read()[1]
            physically_closed = (
                self.actuation.protocol_state.active_valve is None
                and not self.actuation.protocol_state.possibly_open_valves
            )
            if wrapped is not None and not wrapped.result.success:
                raise RuntimeError(f"LOW_FLOW owner recovery failed: {wrapped.result.message}")
            if (
                wrapped is not None
                and wrapped.result.success
                and not snapshot.unsafe_reason()
                and physically_closed
            ):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 0.3:
                    break
            else:
                stable_since = None
            time.sleep(0.01)
        else:
            raise TimeoutError(
                "LOW_FLOW owner recovery did not reach stable SAFE/closed state: "
                f"interlock={self.ingress.read()[1]}, "
                f"status={self.actuation.protocol_state.status.value}, "
                f"active={self.actuation.protocol_state.active_valve}, "
                f"possibly_open={sorted(self.actuation.protocol_state.possibly_open_valves)}, "
                f"flow_result={self._hil_flow_results.get(source)}"
            )

        # The next scenario must explicitly start a fresh protocol epoch.

    def start_protocol_document(self, document: ProtocolDocument) -> None:
        if (
            getattr(self, "_story_35_writer", None) is not None
            and getattr(self, "_story_35_bound_document", None) is not document
        ):
            raise RuntimeError(
                "Story 3.5 单 session 只能使用已绑定协议，禁止加载不同 ProtocolDocument。"
            )
        if self.executor is None:
            raise RuntimeError("production HIL requires ProtocolExecutor")
        self.ingress.update(has_protocol=True, device_lease="idle")
        self.actuation.post_load(document)
        self.wait_status({ProtocolExecutionStatus.READY})
        recovery_deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < recovery_deadline
            and self.ingress.read()[1].safety_state != "SAFE"
        ):
            self.pump()
            time.sleep(0.01)
        if not self.ingress.clear_unsafe_latch():
            raise RuntimeError("protocol trial could not clear a SAFE interlock latch")
        lease_epoch = self.actuation.protocol_state.execution_epoch
        if not self.flow.acquire_protocol_lease(lease_epoch):
            raise RuntimeError("protocol trial could not acquire FlowWorker device lease")
        self.ingress.update(device_lease="protocol")
        self.actuation.post_start(document=None, readiness=self.readiness())
        self.wait_status({ProtocolExecutionStatus.WAITING_TRIGGER})
        if not self.flow.acquire_protocol_lease(
            self.actuation.protocol_state.execution_epoch
        ):
            raise RuntimeError("FlowWorker lease epoch did not synchronize after protocol start")

    def trigger_current_trial_via_ai0(self, *, label: str) -> ActuationReceipt:
        """Trigger through HardwareWorker's acquired monotonic AI0 frame pipeline."""
        marker = len(self.collector.receipts)
        exhale = float(self.config.get("exhale_threshold", -0.44)) - 0.5
        self.latency_trace.begin_trial(label)
        try:
            self.actuation.post_manual_trigger(readiness=self.readiness())
            self.wait_status({ProtocolExecutionStatus.WAITING_EXHALE})
            self.ai_hal.set_ai0_software_stimulus(exhale)
            return self.collector.wait_for(
                lambda item: (
                    item.trial_id == label
                    and item.action == ActuationAction.OPEN
                    and item.category == ActuationCategory.NORMAL
                ),
                2.0,
                self.pump,
                after_index=marker,
            )
        finally:
            self.ai_hal.set_ai0_software_stimulus(None)
            self.latency_trace.end_trial()

    def start_protocol_trial(self, *, label: str, valve: int, duration_ms: float) -> ActuationReceipt:
        document = ProtocolDocument(
            source_path=Path(f"{label}.csv"),
            source_name=f"{label}.csv",
            trials=[
                ProtocolTrial(
                    trial_id=label,
                    timing_ms=0,
                    duration_ms=duration_ms,
                    valve=valve,
                    trigger=TriggerMode.MANUAL,
                )
            ],
        )
        self._ensure_story_35_recording_for_document(document)
        self.start_protocol_document(document)
        return self.trigger_current_trial_via_ai0(label=label)

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

    def _fail_story_35_shutdown_close(self) -> None:
        writer = getattr(self, "_story_35_writer", None)
        if writer is not None:
            writer.fail_from_producer(
                stage="shutdown_emergency_close",
                message=(
                    "中止后未取得全部配置目标关闭回执，"
                    "Story 3.5 bundle 禁止标记完整。"
                ),
            )

    def stop(self) -> None:
        try:
            if (
                self.actuation.isRunning()
                and not self._shutdown_completed
                and not self._abort_close_confirmed
            ):
                try:
                    closed = self.actuation.emergency_close_all(
                        int(
                            self.config.get(
                                "actuation_emergency_close_timeout_ms",
                                500,
                            )
                        )
                        * 4
                    )
                except Exception:
                    self._fail_story_35_shutdown_close()
                    raise
                if not closed:
                    self._fail_story_35_shutdown_close()
                    raise RuntimeError("shutdown emergency close-all 未获得全部成功回执")
        finally:
            self.actuation.shutdown(int(self.config.get("actuation_shutdown_timeout_ms", 2000)))
            self.hardware.stop()
            self.flow.shutdown(int(self.config.get("actuation_shutdown_timeout_ms", 2000)))

    def shutdown_via_service(self) -> dict:
        service = ShutdownService(
            state=self.state,
            worker=self.hardware,
            safety_manager=self.safety,
            retry_limit=0,
            retry_interval=0.0,
            record_path=self.output_dir / "shutdown-event.json",
            actuation_worker=self.actuation,
            flow_worker=self.flow,
            actuation_timeout_ms=int(
                self.config.get("actuation_shutdown_timeout_ms", 2000)
            ),
            emergency_close_timeout_ms=int(
                self.config.get("actuation_emergency_close_timeout_ms", 500)
            ),
        )
        event = service.shutdown(
            source="hil_production_shutdown",
            reason="Story 3.4 shutdown path verification",
            force=True,
        )
        self._shutdown_completed = event.get("result") == "success"
        return event


def _target_key(item) -> tuple[int, str | None, str | None]:
    valve = item.valve if hasattr(item, "valve") else item.logical_valve
    device = item.target_device if hasattr(item, "target_device") else item.device
    line = item.target_line if hasattr(item, "target_line") else item.line
    return int(valve), device, line


def evaluate_full_close(runtime: Runtime, *, after_index: int, scenario: str) -> dict:
    """Build auditable evidence for every configured odor target plus master."""
    expected = {_target_key(step) for step in runtime.valves.emergency_close_steps()}
    latest: dict[tuple[int, str | None, str | None], ActuationReceipt] = {}
    for receipt in runtime.collector.receipts[after_index:]:
        if receipt.action != ActuationAction.CLOSE or receipt.category != ActuationCategory.SAFETY:
            continue
        key = _target_key(receipt)
        if key in expected:
            latest[key] = receipt

    missing = sorted(expected - set(latest), key=lambda item: item[0])
    failed = sorted(
        (key for key, receipt in latest.items() if receipt.result != ActuationResult.SUCCESS),
        key=lambda item: item[0],
    )
    mock_states_closed: bool | None = None
    if isinstance(runtime.hal, MockHAL):
        mock_states_closed = all(
            runtime.hal.get_line_state(f"{device}/{line}" if device else line) is False
            for _, device, line in expected
        )
    state = runtime.actuation.protocol_state
    protocol_state_closed = state.active_valve is None and not state.possibly_open_valves
    all_closed = bool(
        expected
        and not missing
        and not failed
        and protocol_state_closed
        and mock_states_closed is not False
    )

    def serialize(keys):
        return [
            {"valve": valve, "device": device, "line": line}
            for valve, device, line in keys
        ]

    return {
        "scenario": scenario,
        "expected_target_count": len(expected),
        "successful_close_receipt_count": sum(
            receipt.result == ActuationResult.SUCCESS for receipt in latest.values()
        ),
        "missing_targets": serialize(missing),
        "failed_targets": serialize(failed),
        "protocol_state_closed": protocol_state_closed,
        "mock_do_state_closed": mock_states_closed,
        "all_configured_targets_closed": all_closed,
    }


def wait_for_full_close(
    runtime: Runtime,
    *,
    after_index: int,
    scenario: str,
    timeout_s: float = 3.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    evidence = evaluate_full_close(runtime, after_index=after_index, scenario=scenario)
    while not evidence["all_configured_targets_closed"] and time.monotonic() < deadline:
        runtime.pump()
        time.sleep(0.005)
        evidence = evaluate_full_close(runtime, after_index=after_index, scenario=scenario)
    return evidence


def confirm_severe_abort_close(
    runtime: Runtime,
    *,
    after_index: int,
    timeout_s: float = 3.0,
) -> dict:
    """Wait for owner severe closes before fencing and finalizing the bundle."""
    evidence = wait_for_full_close(
        runtime,
        after_index=after_index,
        scenario="severe-abort",
        timeout_s=timeout_s,
    )
    if not evidence["all_configured_targets_closed"]:
        runtime.close_everything("severe-abort-recovery")
        evidence = wait_for_full_close(
            runtime,
            after_index=after_index,
            scenario="severe-abort",
            timeout_s=timeout_s,
        )
    if not evidence["all_configured_targets_closed"]:
        raise RuntimeError(
            "正式 benchmark severe 中止未取得全部配置目标关闭回执，禁止完成 bundle"
        )
    runtime._abort_close_confirmed = True
    return evidence


def run_authorized_close_check(runtime: Runtime, *, timeout_ms: int) -> dict:
    """Close every DO target without requiring AI or serial owners to become ready."""
    marker = len(runtime.collector.receipts)
    owner_success = runtime.actuation.emergency_close_all(max(1, int(timeout_ms)))
    evidence = evaluate_full_close(
        runtime,
        after_index=marker,
        scenario="authorized_close_check",
    )
    return {
        "count": evidence["successful_close_receipt_count"],
        "success": bool(owner_success and evidence["all_configured_targets_closed"]),
        **evidence,
    }


def wait_protocol_trial(
    runtime: Runtime,
    *,
    label: str,
    opened: ActuationReceipt,
) -> tuple[ActuationReceipt, ActuationReceipt]:
    closed = runtime.collector.wait_for(
        lambda item: (
            item.action == ActuationAction.CLOSE
            and (
                (item.trial_id == label and item.category == ActuationCategory.NORMAL)
                or (
                    item.valve == opened.valve
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
            f"trial {label} 触发安全补偿关闭；open jitter={opened.jitter_ms}ms"
        )
    if closed.result != ActuationResult.SUCCESS:
        raise RuntimeError(f"定时关闭失败：{closed.command_id}: {closed.message}")
    return opened, closed


def build_benchmark_document(args) -> ProtocolDocument:
    trials = [
        ProtocolTrial(
            trial_id=f"bench-{index + 1:04d}-v{args.valves[index % len(args.valves)]}",
            timing_ms=0,
            duration_ms=args.duration_ms,
            valve=args.valves[index % len(args.valves)],
            trigger=TriggerMode.MANUAL,
        )
        for index in range(args.cycles)
    ]
    story = "3.5" if getattr(args, "story_3_5_recording", False) else "3.4"
    return ProtocolDocument(
        source_path=Path(f"story-{story.replace('.', '-')}-hil-benchmark.csv"),
        source_name=f"story-{story.replace('.', '-')}-hil-benchmark.csv",
        trials=trials,
        metadata={"story": story},
    )


def run_benchmark(runtime: Runtime, args) -> dict:
    initial_closes = runtime.close_everything("initial-close")
    document = build_benchmark_document(args)
    if getattr(args, "story_3_5_recording", False):
        runtime.begin_story_35_recording(
            document,
            candidate_commit=args.candidate_commit,
            run_id=runtime.output_dir.name,
            live=bool(args.live),
        )
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

    trials = list(document.trials)
    runtime.start_protocol_document(document)
    started = time.time()
    for index, trial in enumerate(trials):
        abort_close_marker = len(runtime.collector.receipts)
        opened = runtime.trigger_current_trial_via_ai0(label=trial.trial_id)
        try:
            _, closed = wait_protocol_trial(
                runtime,
                label=trial.trial_id,
                opened=opened,
            )
        except Exception:
            if runtime.metrics.severe_latched:
                confirm_severe_abort_close(
                    runtime,
                    after_index=abort_close_marker,
                )
            raise
        if (opened.jitter_ms or 0) > 30.0 or (closed.jitter_ms or 0) > 30.0:
            confirm_severe_abort_close(
                runtime,
                after_index=abort_close_marker,
            )
            raise RuntimeError("正式 benchmark 发生单次 >30ms 严重超限，已停止")
        expected_status = (
            ProtocolExecutionStatus.COMPLETED
            if index == len(trials) - 1
            else ProtocolExecutionStatus.WAITING_TRIGGER
        )
        runtime.wait_status({expected_status})
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
        "open": summarize(
            opens,
            window_size=runtime.metrics.config.window_size,
            min_samples=runtime.metrics.config.min_samples,
        ),
        "close": summarize(
            closes,
            window_size=runtime.metrics.config.window_size,
            min_samples=runtime.metrics.config.min_samples,
        ),
        "combined": summarize(
            combined,
            window_size=runtime.metrics.config.window_size,
            min_samples=runtime.metrics.config.min_samples,
        ),
    }


def run_safety_scenarios(runtime: Runtime, args) -> dict:
    """Exercise stop, readiness loss, severe jitter, and shutdown owner paths."""
    results = {}

    runtime.start_protocol_trial(
        label="safety-stop", valve=args.valves[0], duration_ms=500
    )
    marker = len(runtime.collector.receipts)
    runtime.actuation.post_stop(message="HIL production stop path")
    stop_evidence = wait_for_full_close(
        runtime,
        after_index=marker,
        scenario="stop",
    )
    runtime.wait_status({ProtocolExecutionStatus.STOPPED})
    results["stop"] = stop_evidence
    if not stop_evidence["all_configured_targets_closed"]:
        runtime.close_everything("stop-safety-recovery")
    if (
        getattr(args, "story_3_5_recording", False)
        and runtime._story_35_writer is not None
    ):
        finalization, validation = runtime.finalize_story_35_recording(
            reason="safety_stop_completed",
            aborted=False,
            final_quality=runtime.metrics.snapshot(),
            fence_producers=True,
        )
        if not finalization.complete or not validation.complete:
            raise RuntimeError(
                "Story 3.5 stop safety bundle 未通过 writer/validator。"
            )

    runtime.start_protocol_trial(
        label="safety-low-flow", valve=args.valves[1], duration_ms=500
    )
    marker = len(runtime.collector.receipts)
    runtime.hardware.consume_airflow_sample(0.0, time.time(), None)
    low_flow_evidence = wait_for_full_close(
        runtime,
        after_index=marker,
        scenario="low_flow",
    )
    results["low_flow"] = low_flow_evidence
    if not low_flow_evidence["all_configured_targets_closed"]:
        runtime.close_everything("low-flow-safety-recovery")
    runtime.recover_low_flow_via_owner()
    if (
        getattr(args, "story_3_5_recording", False)
        and runtime._story_35_writer is not None
    ):
        finalization, validation = runtime.finalize_story_35_recording(
            reason="safety_low_flow_completed",
            aborted=False,
            final_quality=runtime.metrics.snapshot(),
            fence_producers=True,
        )
        if not finalization.complete or not validation.complete:
            raise RuntimeError(
                "Story 3.5 low-flow safety bundle 未通过 writer/validator。"
            )

    runtime.collector.inject_delay_ms = runtime.metrics.config.single_limit_ms + 5.0
    marker = len(runtime.collector.receipts)
    severe_open = runtime.start_protocol_trial(
        label="safety-severe", valve=args.valves[2], duration_ms=500
    )
    severe_evidence = wait_for_full_close(
        runtime,
        after_index=marker,
        scenario="severe",
    )
    severe_limit_ms = runtime.metrics.config.single_limit_ms
    if severe_open.jitter_ms is None or severe_open.jitter_ms <= severe_limit_ms:
        raise RuntimeError(
            f"severe injection did not exceed {severe_limit_ms:g}ms jitter"
        )
    results["severe"] = {
        "open_jitter_ms": severe_open.jitter_ms,
        "latched": runtime.metrics.severe_latched,
        **severe_evidence,
    }
    if not severe_evidence["all_configured_targets_closed"]:
        runtime.close_everything("severe-safety-recovery")
    if (
        getattr(args, "story_3_5_recording", False)
        and runtime._story_35_writer is not None
    ):
        finalization, validation = runtime.finalize_story_35_recording(
            reason="safety_severe_completed",
            aborted=False,
            final_quality=runtime.metrics.snapshot(),
            fence_producers=True,
        )
        if not finalization.complete or not validation.complete:
            raise RuntimeError(
                "Story 3.5 severe safety bundle 未通过 writer/validator。"
            )

    runtime.start_protocol_trial(
        label="safety-shutdown", valve=args.valves[0], duration_ms=500
    )
    marker = len(runtime.collector.receipts)
    shutdown_event = runtime.shutdown_via_service()
    shutdown_evidence = wait_for_full_close(
        runtime,
        after_index=marker,
        scenario="shutdown",
        timeout_s=0.25,
    )
    results["shutdown"] = {
        "result": shutdown_event.get("result"),
        "valves_closed": shutdown_event.get("valves_closed"),
        "heaters_off": shutdown_event.get("heaters_off"),
        "error": shutdown_event.get("error"),
        **shutdown_evidence,
    }
    if (
        getattr(args, "story_3_5_recording", False)
        and runtime._story_35_writer is not None
    ):
        finalization, validation = runtime.finalize_story_35_recording(
            reason="safety_shutdown_completed",
            aborted=False,
            final_quality=runtime.metrics.snapshot(),
            fence_producers=False,
        )
        if not finalization.complete or not validation.complete:
            raise RuntimeError(
                "Story 3.5 shutdown safety bundle 未通过 writer/validator。"
            )
    return results


def evaluate_performance_gates(runtime: Runtime, args, benchmark: dict) -> dict:
    target_ms = float(runtime.metrics.config.target_ms)
    streams = ("open", "close", "combined")
    aggregate_met = all(
        benchmark[name]["p95_ms"] is not None
        and benchmark[name]["p95_ms"] < target_ms
        for name in streams
    )
    rolling_met = all(
        bool(benchmark[name]["rolling_p95_ms"])
        and all(
            value is not None and value < target_ms
            for value in benchmark[name]["rolling_p95_ms"]
        )
        for name in streams
    )
    final_windows_met = all(
        benchmark[name]["final_window_p95_ms"] is not None
        and benchmark[name]["final_window_p95_ms"] < target_ms
        for name in streams
    )
    required_samples = 200 if args.live else args.cycles
    samples_complete = bool(
        benchmark["open"]["count"] >= required_samples
        and benchmark["close"]["count"] >= required_samples
    )
    return {
        "target_ms": target_ms,
        "aggregate_p95_strictly_below_target": aggregate_met,
        "every_rolling_p95_strictly_below_target": rolling_met,
        "final_window_p95_strictly_below_target": final_windows_met,
        "sample_counts_complete": samples_complete,
        "passed": bool(
            aggregate_met
            and rolling_met
            and final_windows_met
            and samples_complete
        ),
    }


def run_acceptance_scenarios(runtime: Runtime, args) -> tuple[dict, dict]:
    """Run safety scenarios only after the formal benchmark returns successfully."""
    benchmark = run_benchmark(runtime, args)
    performance_gate = evaluate_performance_gates(runtime, args, benchmark)
    benchmark["performance_gate"] = performance_gate
    if not performance_gate["passed"]:
        runtime.close_everything("performance-gate-abort")
        failed = [
            name
            for name, passed in (
                (
                    "aggregate_p95",
                    performance_gate["aggregate_p95_strictly_below_target"],
                ),
                (
                    "rolling_p95",
                    performance_gate["every_rolling_p95_strictly_below_target"],
                ),
                (
                    "final_window_p95",
                    performance_gate["final_window_p95_strictly_below_target"],
                ),
                ("sample_counts", performance_gate["sample_counts_complete"]),
            )
            if not passed
        ]
        raise RuntimeError(
            "正式 benchmark 性能 Gate 未通过，已安全全关且不执行后续场景："
            + ", ".join(failed)
        )
    if (
        getattr(args, "story_3_5_recording", False)
        and runtime._story_35_writer is not None
    ):
        benchmark_quality = runtime.metrics.snapshot()
        finalization, validation = runtime.finalize_story_35_recording(
            reason="benchmark_performance_gate_passed",
            aborted=False,
            final_quality=benchmark_quality,
            fence_producers=True,
        )
        if not finalization.complete or not validation.complete:
            runtime.close_everything("benchmark-bundle-failure")
            raise RuntimeError(
                "Story 3.5 benchmark bundle 未通过 writer/validator，"
                "已安全全关且不执行后续场景。"
            )
    safety = run_safety_scenarios(runtime, args)
    return benchmark, safety


def production_safety_paths_succeeded(safety: dict) -> bool:
    """Require complete configured-target closure evidence for every safety path."""
    return bool(
        safety.get("stop", {}).get("all_configured_targets_closed") is True
        and safety.get("low_flow", {}).get("all_configured_targets_closed") is True
        and safety.get("severe", {}).get("all_configured_targets_closed") is True
        and safety.get("severe", {}).get("latched") is True
        and safety.get("shutdown", {}).get("all_configured_targets_closed") is True
        and safety.get("shutdown", {}).get("result") == "success"
        and safety.get("shutdown", {}).get("valves_closed") is True
        and safety.get("shutdown", {}).get("heaters_off") is True
    )


def summarize(values: list[float], *, window_size: int, min_samples: int) -> dict:
    rolling = [
        nearest_rank_p95(values[max(0, end - window_size) : end])
        for end in range(min_samples, len(values) + 1)
    ]
    final_values = values[-window_size:]
    return {
        "count": len(values),
        "p95_ms": nearest_rank_p95(values),
        "max_ms": max(values) if values else None,
        "mean_ms": statistics.fmean(values) if values else None,
        "failures": 0,
        "rolling_window_size": window_size,
        "rolling_min_samples": min_samples,
        "rolling_p95_ms": rolling,
        "max_rolling_p95_ms": max(rolling) if rolling else None,
        "final_window_count": len(final_values),
        "final_window_p95_ms": nearest_rank_p95(final_values),
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


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _validate_live_story35_candidate(
    parser: argparse.ArgumentParser,
    candidate_commit: str,
) -> None:
    try:
        _git_output("cat-file", "-e", f"{candidate_commit}^{{commit}}")
    except (OSError, subprocess.CalledProcessError):
        parser.error(
            "live Story 3.5 candidate 必须是当前仓库中存在的 commit object"
        )
    try:
        head = _git_output("rev-parse", "HEAD")
        status = _git_output(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    except (OSError, subprocess.CalledProcessError):
        parser.error("live Story 3.5 无法读取当前 Git HEAD/worktree 状态")
    if candidate_commit.lower() != head.lower():
        parser.error(
            "live Story 3.5 --candidate-commit 必须精确等于当前 HEAD"
        )
    if status:
        parser.error(
            "live Story 3.5 正式 Gate 要求 index/worktree clean；"
            "不得把未提交内容绑定到 candidate evidence"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Story 3.4/3.5 live NI actuation HIL benchmark"
    )
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config/default_config.json")
    parser.add_argument("--local-config", type=Path, default=REPO_ROOT / "config/local_config.json")
    parser.add_argument("--serial-port", default="COM6")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--story-3-5-recording",
        action="store_true",
        help=(
            "enable native Story 3.5 SessionWriter, owner bind/fences, visible "
            "ProtocolView, and final complete-bundle validation"
        ),
    )
    parser.add_argument(
        "--candidate-commit",
        default="",
        help="40-character candidate commit recorded in Story 3.5 evidence",
    )
    parser.add_argument(
        "--latency-trace",
        action="store_true",
        help="record in-memory diagnostic stage timestamps and flush them on exit",
    )
    parser.add_argument("--confirm", default="")
    parser.add_argument("--phase", choices=("preflight", "close", "all"), default="all")
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--duration-ms", type=float, default=100.0)
    parser.add_argument("--inter-trial-ms", type=float, default=250.0)
    parser.add_argument("--valves", type=int, nargs="+", default=[1, 9, 13])
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "logs/benchmarks")
    args = parser.parse_args()
    if args.live and args.confirm != LIVE_CONFIRMATION:
        parser.error(f"live hardware requires --confirm {LIVE_CONFIRMATION}")
    if args.cycles < 1 or args.duration_ms <= 0 or args.inter_trial_ms < 250:
        parser.error("cycles/duration must be positive and inter-trial must be >=250ms")
    if args.valves != [1, 9, 13]:
        parser.error("Story 3.4 HIL requires representative valves 1 9 13")
    if args.story_3_5_recording:
        if args.phase != "all":
            parser.error("Story 3.5 recording gate requires --phase all")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.candidate_commit):
            parser.error(
                "Story 3.5 recording gate requires --candidate-commit with 40 hex characters"
            )
        if args.live:
            _validate_live_story35_candidate(
                parser,
                args.candidate_commit,
            )
        args.latency_trace = True
    return args


def main() -> int:
    args = parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    story_key = "3-5" if args.story_3_5_recording else "3-4"
    output_dir = (
        args.output_root
        / f"story-{story_key}-{stamp}-{'live' if args.live else 'mock'}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    config = load_config(args)
    metadata = {
        "story": "3.5" if args.story_3_5_recording else "3.4",
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
            "ai0_external_signal": False,
            "ai6_external_signal": False,
            "latency_trace_enabled": args.latency_trace,
            "latency_trace_is_diagnostic_only": True,
            "session_recording": args.story_3_5_recording,
            "structured_logging": args.story_3_5_recording,
            "ui_component": "ProtocolView",
            "ui_visible": args.story_3_5_recording,
            "hardware_mode": "real" if args.live else "simulation",
            "candidate_commit": args.candidate_commit or None,
            "performance_ai0_source": (
                "software stimulus applied to HAL frames acquired by HardwareWorker; "
                "monotonic timestamp/epoch/sequence remain HAL-owned; not an external sensor stimulus"
            ),
            "low_flow_safety_source": (
                "software-injected sample through production HardwareWorker ingress; "
                "not an external sensor stimulus"
            ),
            "gas_load": "clean/inert, odor-free, operator confirmed",
        },
    }
    runtime = None
    exit_code = 1
    try:
        hal, devices, airflow, checks = preflight(
            config,
            args.live,
            require_flow=args.phase != "close",
        )
        metadata.update({"devices": devices, "mfc_airflow": airflow, "checks": checks})
        if args.phase == "preflight":
            (output_dir / "summary.json").write_text(
                json.dumps({"metadata": metadata, "preflight": "passed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            exit_code = 0
            return exit_code
        runtime = Runtime(
            config=config,
            hal=hal,
            airflow=airflow,
            output_dir=output_dir,
            protocol_mode=args.phase == "all",
            latency_trace=args.latency_trace,
            story_35_recording=args.story_3_5_recording,
            live=args.live,
        )
        if args.phase == "close":
            # Emergency close must remain available when serial/MFC readiness is
            # unavailable.  Acquire only the ActuationWorker DO owner; do not
            # start AI or FlowWorker and do not require flow setpoint readback.
            close_check = run_authorized_close_check(
                runtime,
                timeout_ms=int(config.get("actuation_emergency_close_timeout_ms", 500))
                * 4,
            )
            metadata["resource_groups"] = [
                {"device": key[0], "port": key[1]}
                for key in sorted(getattr(hal, "_do_sessions", {}))
            ]
            (output_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "metadata": metadata,
                        "close_check": close_check,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            exit_code = 0 if close_check["success"] else 2
            return exit_code
        runtime.start()
        metadata["resource_groups"] = [
            {"device": key[0], "port": key[1]}
            for key in sorted(getattr(hal, "_do_sessions", {}))
        ]
        benchmark, safety = run_acceptance_scenarios(runtime, args)
        summary = {"metadata": metadata, "benchmark": benchmark, "safety": safety}
        performance_gate = benchmark["performance_gate"]
        target_ms = performance_gate["target_ms"]
        target_met = performance_gate[
            "aggregate_p95_strictly_below_target"
        ]
        rolling_met = performance_gate[
            "every_rolling_p95_strictly_below_target"
        ]
        final_windows_met = performance_gate[
            "final_window_p95_strictly_below_target"
        ]
        samples_complete = performance_gate["sample_counts_complete"]
        write_failure_results = {
            ActuationResult.FAILED,
            ActuationResult.TIMEOUT,
            ActuationResult.MEASUREMENT_FAULT,
            ActuationResult.UNCERTAIN,
        }
        write_failures = [
            item for item in runtime.collector.receipts if item.result in write_failure_results
        ]
        cancelled_commands = sum(
            item.result == ActuationResult.CANCELLED for item in runtime.collector.receipts
        )
        actions_succeeded = not write_failures
        safety_succeeded = production_safety_paths_succeeded(safety)
        summary["acceptance"] = {
            "target_ms": target_ms,
            "aggregate_p95_strictly_below_target": target_met,
            "every_rolling_p95_strictly_below_target": rolling_met,
            "final_window_p95_strictly_below_target": final_windows_met,
            "sample_counts_complete": samples_complete,
            "no_action_failures": actions_succeeded,
            "cancelled_commands": cancelled_commands,
            "production_safety_paths_succeeded": safety_succeeded,
            "external_ai_ttl_signal_limitation": True,
        }
        exit_code = (
            0
            if all(
                (
                    target_met,
                    rolling_met,
                    final_windows_met,
                    samples_complete,
                    actions_succeeded,
                    safety_succeeded,
                )
            )
            else 2
        )
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
            if (
                args.story_3_5_recording
                and getattr(runtime, "_story_35_writer", None) is not None
            ):
                aborted = exit_code != 0 or "failure" in metadata
                try:
                    finalization, validation = runtime.finalize_story_35_recording(
                        reason=(
                            metadata.get("failure", "acceptance_failed")
                            if aborted
                            else "acceptance_completed"
                        ),
                        aborted=aborted,
                    )
                    metadata["session_bundle"] = {
                        "path": str(validation.path),
                        "writer_complete": finalization.complete,
                        "validator_complete": validation.complete,
                        "validator_reason": validation.reason,
                        "last_session_sequence": validation.last_sequence,
                    }
                    if not finalization.complete or not validation.complete:
                        exit_code = 1
                except Exception as exc:
                    metadata["session_finalization_failure"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    exit_code = 1
            bundle_results = list(
                getattr(runtime, "_story_35_bundle_results", ())
            )
            if bundle_results:
                metadata["session_bundles"] = bundle_results
                metadata.setdefault("session_bundle", bundle_results[0])
                if any(
                    not item["writer_complete"]
                    or not item["validator_complete"]
                    for item in bundle_results
                ):
                    exit_code = 1
            write_csv(output_dir / "receipts.csv", runtime.collector.receipts)
            runtime.collector.write_jsonl()
            runtime.latency_trace.write_jsonl(output_dir / "latency-trace.jsonl")
        metadata["finished_at"] = time.time()
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(output_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
