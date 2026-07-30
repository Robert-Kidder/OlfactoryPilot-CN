from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from app.models import ProtocolExecutionReadiness, SelfCheckResult
from app.services.hal import AnalogInputFrame, BreathSampleBatch, HalInterface
from app.services.hardware_check_service import HardwareCheckService
from app.services.mock_hal import MockHAL
from app.services.ttl_trigger_service import TtlTriggerConfig, TtlTriggerService

LOG = logging.getLogger(__name__)


class HardwareWorker(QThread):
    telemetry_ready = Signal(dict)
    status_message = Signal(str)
    self_check_completed = Signal(list, bool)
    breath_samples = Signal(object)
    ttl_pulse = Signal(object)
    ttl_input_error = Signal(str)
    ttl_readiness_changed = Signal(bool)
    ttl_arm_ack = Signal(int, bool)
    ttl_disarm_ack = Signal(bool)

    def __init__(
        self,
        telemetry_hz: int = 5,
        check_service: HardwareCheckService | None = None,
        breath_hz: int = 100,
        ttl_poll_hz: int = 1000,
        ttl_config: dict | TtlTriggerConfig | None = None,
        hal: HalInterface | None = None,
        simulation: bool = False,
    ) -> None:
        super().__init__()
        self._running = False
        self.telemetry_interval_ms = self._compute_interval_ms(telemetry_hz)
        self.breath_interval_ms = self._compute_interval_ms(breath_hz)
        config = (
            ttl_config
            if isinstance(ttl_config, TtlTriggerConfig)
            else TtlTriggerConfig.from_mapping(ttl_config)
        )
        if ttl_config is None:
            config = TtlTriggerConfig.from_mapping({"ttl_poll_hz": ttl_poll_hz})
        self.ttl_service = TtlTriggerService(config)
        self.ttl_interval_ms = self._compute_interval_ms(config.poll_hz)
        self._breath_emit_every = max(1, round(config.poll_hz / max(1, breath_hz)))
        self._ai_sample_count = 0
        self._last_ai_epoch = -1
        self._last_ai_sequence = -1
        self._last_ai_monotonic_ns = -1
        self.check_service = check_service
        self.hal: HalInterface = hal or MockHAL()
        self._ttl_runtime_ready = bool(getattr(self.hal, "ttl_input_ready", False))
        self._ai_error_latched = False
        self._ai_retry_not_before = 0.0
        self._ai_error_backoff_s = 1.0
        self.simulation_mode = simulation
        self._self_check_requested = False
        self._connected = False
        self._actuation_sink = None
        self._interlock_ingress = None
        self._before_external_self_check: Callable[[], bool] | None = None
        self._after_external_self_check: Callable[[], bool] | None = None
        self._flow_sample_lock = threading.Lock()
        self._flow_sample: tuple[float, float] | None = None
        self._flow_sample_stale_after_s = 1.0
        self._ai_release_attempted = False
        self._ai_release_success = False
        self._ttl_control_lock = threading.Lock()
        self._ttl_control_queue: deque[tuple[str, Any]] = deque()
        self._session_recorder = None
        self._session_recorder_sequence = 0
        self._session_recorder_generation = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def run(self) -> None:  # noqa: D401
        """Worker loop emits telemetry placeholders over signals."""
        self._running = True
        self._ai_release_attempted = False
        self._ai_release_success = False
        mode_label = "（模拟模式）" if self.simulation_mode else "（占位）"
        self.status_message.emit(f"硬件线程已启动{mode_label}")
        self._run_self_check()
        next_telemetry = time.time()
        next_ai = time.time()
        while self._running:
            self._process_ttl_control()
            now = time.time()
            if self._self_check_requested:
                self._self_check_requested = False
                self._run_self_check()

            if now >= next_ai:
                self._emit_ai_frame(now)
                next_ai += max(self.ttl_interval_ms, 1) / 1000.0

            if now >= next_telemetry:
                self._emit_telemetry(now)
                next_telemetry += max(self.telemetry_interval_ms, 1) / 1000.0

            sleep_ms = min(self.telemetry_interval_ms, self.ttl_interval_ms)
            # Python 3.11 uses a high-resolution waitable timer on Windows;
            # QThread.msleep(1) rounds to the ~15.6 ms system timer quantum.
            time.sleep(max(1, sleep_ms) / 1000.0)
        self._apply_ttl_disarm()
        self._emit_session_fence()
        self._release_ai_owned_resources(final=True)
        self.status_message.emit("硬件线程已停止")

    def stop(self) -> bool:
        self._self_check_requested = False
        if not self.isRunning():
            self._connected = False
            if self._ai_release_attempted:
                return self._ai_release_success
            return self._release_ai_owned_resources(final=True)
        self._running = False
        self._connected = False
        stopped = bool(self.wait(2000))
        return bool(stopped and self._ai_release_attempted and self._ai_release_success)

    def request_self_check(self) -> None:
        """Allow controller/UI to trigger another self-check without blocking UI."""
        self._self_check_requested = True

    def set_actuation_sink(self, sink, *, interlock_ingress=None) -> None:
        self._actuation_sink = sink
        self._interlock_ingress = interlock_ingress

    def set_session_recorder(self, recorder) -> bool:
        return self.bind_session_recorder(recorder, generation=0)

    def bind_session_recorder(
        self,
        recorder,
        *,
        generation: int,
        timeout_ms: int = 1000,
    ) -> bool:
        ack = threading.Event()
        cancelled = threading.Event()
        result: dict[str, bool] = {}
        payload = {
            "recorder": recorder,
            "generation": int(generation),
            "ack": ack,
            "cancelled": cancelled,
            "result": result,
        }
        with self._ttl_control_lock:
            self._ttl_control_queue.append(("session_bind", payload))
        if not self.isRunning():
            self._process_ttl_control()
        if not ack.wait(max(1, int(timeout_ms)) / 1000.0):
            with self._ttl_control_lock:
                if not ack.is_set():
                    cancelled.set()
                    for index, (action, queued_payload) in enumerate(
                        self._ttl_control_queue
                    ):
                        if action == "session_bind" and queued_payload is payload:
                            del self._ttl_control_queue[index]
                            break
                    result["accepted"] = False
                    ack.set()
        return bool(result.get("accepted"))

    def post_session_fence(self) -> None:
        with self._ttl_control_lock:
            self._ttl_control_queue.append(("session_fence", None))
        if not self.isRunning():
            self._process_ttl_control()

    def set_self_check_coordinator(
        self,
        *,
        before: Callable[[], bool],
        after: Callable[[], bool],
    ) -> None:
        """Coordinate exclusive serial access around an external COM probe."""
        self._before_external_self_check = before
        self._after_external_self_check = after

    def consume_airflow_sample(
        self,
        value: float,
        timestamp: float,
        error: str | None = None,
    ) -> None:
        """Consume serial-owner telemetry without acquiring the COM handle here."""
        try:
            sampled_at = float(timestamp)
        except (TypeError, ValueError):
            sampled_at = float("nan")
        if not math.isfinite(sampled_at):
            sampled_at = time.time()
            error = error or "invalid airflow sample timestamp"
        try:
            airflow = float(value)
        except (TypeError, ValueError):
            airflow = float("nan")
            error = error or "invalid airflow sample"
        sample = (float("nan") if error is not None else airflow, sampled_at)
        with self._flow_sample_lock:
            self._flow_sample = sample
        # Every serial sample is safety-significant: LOW_FLOW and errors must
        # not wait for the next UI/telemetry tick before waking the owner.
        self._publish_interlock(sample[0], sample[1])

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        """Reject the legacy DO path; ActuationWorker is the sole DO writer."""
        LOG.error(
            "Rejected legacy HardwareWorker DO write | device=%s | line=%s | state=%s",
            device or "N/A",
            line,
            state,
        )
        return False

    def mark_disconnected(self) -> None:
        """Explicitly mark hardware as disconnected for telemetry loop."""
        self._self_check_requested = False
        self._connected = False
        if self.isRunning():
            self.post_ttl_disarm()
        else:
            self._apply_ttl_disarm()

    @property
    def ttl_input_ready(self) -> bool:
        return bool(self._ttl_runtime_ready and getattr(self.hal, "ttl_input_ready", False))

    def post_ttl_arm(self, arm_epoch: int) -> None:
        """Thread-safe producer endpoint; this worker performs the mutation."""
        with self._ttl_control_lock:
            self._ttl_control_queue.append(("arm", int(arm_epoch)))

    def post_ttl_disarm(self) -> None:
        """Thread-safe producer endpoint independent of the UI event loop."""
        with self._ttl_control_lock:
            self._ttl_control_queue.append(("disarm", None))

    def _process_ttl_control(self) -> None:
        while True:
            with self._ttl_control_lock:
                if not self._ttl_control_queue:
                    return
                action, payload = self._ttl_control_queue.popleft()
            if action == "arm":
                assert payload is not None
                self._apply_ttl_arm(payload)
            elif action == "disarm":
                self._apply_ttl_disarm()
            elif action == "session_fence":
                self._emit_session_fence()
            else:
                with self._ttl_control_lock:
                    accepted = (
                        not payload["cancelled"].is_set()
                        and self._session_recorder is None
                    )
                    if accepted:
                        self._session_recorder = payload["recorder"]
                        self._session_recorder_sequence = 0
                        self._session_recorder_generation = int(
                            payload["generation"]
                        )
                    payload["result"]["accepted"] = accepted
                    payload["ack"].set()

    def arm_ttl(self, *, arm_epoch: int) -> None:
        """Synchronous owner-local helper retained for deterministic tests."""
        self._apply_ttl_arm(int(arm_epoch))

    def _apply_ttl_arm(self, arm_epoch: int) -> None:
        try:
            self.ttl_service.arm(arm_epoch=arm_epoch)
            armed = True
        except Exception:  # pragma: no cover - defensive
            LOG.exception("TTL 布防失败")
            armed = False
        self._notify_ttl_arm_ack(int(arm_epoch), armed)

    def disarm_ttl(self) -> None:
        """Synchronous owner-local helper retained for shutdown/test callers."""
        self._apply_ttl_disarm()

    def _apply_ttl_disarm(self) -> None:
        try:
            self.ttl_service.disarm()
            self.ttl_disarm_ack.emit(True)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("TTL 解除布防失败")
            self.ttl_disarm_ack.emit(False)

    def _notify_ttl_arm_ack(self, arm_epoch: int, armed: bool) -> None:
        sink = self._actuation_sink
        consumer = getattr(sink, "consume_ttl_arm_ack", None)
        if callable(consumer):
            # The consumer only enqueues under ActuationWorker's own lock.
            consumer(arm_epoch=arm_epoch, armed=armed)
        self.ttl_arm_ack.emit(arm_epoch, armed)

    # Shutdown helpers for safe-stop scenarios.
    def close_all_channels(self) -> bool:
        """Reject the legacy DO path; use ActuationWorker emergency close."""
        LOG.error("Rejected legacy HardwareWorker close_all; ActuationWorker owns DO")
        return False

    def stop_heaters(self) -> bool:
        """Stop heaters/pumps; placeholder returns success for mock worker."""
        if not self.hal:
            return True
        try:
            return bool(self.hal.stop_heaters())
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL stop_heaters failed")
            return False

    def flush_logs(self) -> None:
        """Flush data logger/session file handles if any."""
        if not self.hal:
            return None
        try:
            self.hal.flush_logs()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL flush_logs failed")
            return None

    def release_resources(self) -> bool:
        """Compatibility alias that releases only HardwareWorker-owned AI."""
        return self.release_ai_resources()

    def release_ai_resources(self) -> bool:
        """Release only HardwareWorker-owned AI resources; DO belongs elsewhere."""
        return self.stop()

    def _run_self_check(self) -> None:
        results: list[SelfCheckResult] = []
        ready = False
        try:
            if self.simulation_mode and hasattr(self.hal, "self_check"):
                results, ready = self.hal.self_check()
            elif self.check_service:
                coordinated = False
                if self._before_external_self_check is not None:
                    coordinated = bool(self._before_external_self_check())
                    if not coordinated:
                        raise RuntimeError("无法暂停 serial owner，已取消自检以避免并发打开 COM")
                elif bool(getattr(self.hal, "serial_resources_in_use", False)):
                    raise RuntimeError("serial owner 仍持有 COM，已取消自检以避免并发打开")
                try:
                    results, ready = self.check_service.run_checks()
                finally:
                    if coordinated and self._after_external_self_check is not None:
                        if not self._after_external_self_check():
                            raise RuntimeError("serial owner 在自检后恢复失败")
            elif hasattr(self.hal, "self_check"):
                results, ready = self.hal.self_check()
            elif self.simulation_mode:
                ready = True
            else:
                ready = False
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("Hardware self-check raised an exception")
            results = [
                SelfCheckResult(
                    name="self_check",
                    type="unknown",
                    status="FAIL",
                    reason=str(exc),
                    suggestion="检查硬件连接与驱动，重试自检",
                    checked_at=time.time(),
                )
            ]
            ready = False
        self._connected = ready
        if self._interlock_ingress is not None:
            self._interlock_ingress.update(
                connected=bool(ready),
                hardware_ready=bool(ready and not self._ai_error_latched),
                ttl_input_ready=bool(ready and self.ttl_input_ready),
            )
            current = self._interlock_ingress.read()[1]
            if self._actuation_sink is not None:
                self._actuation_sink.post_readiness_update(
                    readiness=ProtocolExecutionReadiness(
                        connected=current.connected,
                        hardware_ready=current.hardware_ready,
                        flow_setpoints_ready=current.flow_setpoints_ready,
                        safety_state=current.safety_state,
                        ttl_input_ready=current.ttl_input_ready,
                    ),
                    timestamp=time.time(),
                )
        self.self_check_completed.emit(results, ready)
        if self.simulation_mode and ready:
            status = "模拟模式：自检通过"
        else:
            status = "硬件自检通过" if ready else "硬件自检失败，请检查连接"
        self.status_message.emit(status)
        LOG.info("硬件自检完成 | ready=%s | 项目数=%s", ready, len(results))

    def _emit_breath_sample(self, timestamp: float) -> None:
        """Generate breath sample via HAL (mock or real)."""
        try:
            value = float(self.hal.read_ai0(timestamp)) if self.hal else 0.0
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HAL read_ai0 failed")
            value = 0.0
        frame = AnalogInputFrame(
            timestamp=timestamp,
            ai0=value,
            monotonic_ns=time.perf_counter_ns(),
            ai_epoch=0,
            sample_sequence=self._ai_sample_count,
        )
        self._ai_sample_count += 1
        self.breath_samples.emit(BreathSampleBatch.from_frames((frame,)))

    def _emit_telemetry(self, timestamp: float) -> None:
        airflow = self._read_flow(timestamp)
        payload: dict[str, object] = {
            "connected": self._connected,
            "airflow": airflow,
            "safety_state": "DATA_STALE",
            "timestamp": timestamp,
        }
        current = self._publish_interlock(airflow, timestamp)
        if current is not None:
            payload["safety_state"] = current.safety_state
        self.telemetry_ready.emit(payload)

    def _publish_interlock(self, airflow: float, timestamp: float):
        if self._interlock_ingress is None:
            return None
        current = self._interlock_ingress.read()[1]
        self._interlock_ingress.publish_raw_telemetry(
            airflow=airflow,
            timestamp=timestamp,
            hardware_state=None,
            connected=bool(self._connected),
            hardware_ready=bool(self._connected and not self._ai_error_latched),
            flow_setpoints_ready=current.flow_setpoints_ready,
            ttl_input_ready=self.ttl_input_ready,
            has_protocol=current.has_protocol,
            device_lease=current.device_lease,
        )
        current = self._interlock_ingress.read()[1]
        if self._actuation_sink is not None:
            # This owner-to-owner notification does not wait for the UI event
            # loop. Unsafe transitions wake ActuationWorker directly.
            self._actuation_sink.post_readiness_update(
                readiness=ProtocolExecutionReadiness(
                    connected=current.connected,
                    hardware_ready=current.hardware_ready,
                    flow_setpoints_ready=current.flow_setpoints_ready,
                    safety_state=current.safety_state,
                    ttl_input_ready=current.ttl_input_ready,
                ),
                timestamp=timestamp,
            )
        return current

    def _emit_ai_frame(self, timestamp: float) -> None:
        attempt_started = time.monotonic()
        if self._ai_error_latched and attempt_started < self._ai_retry_not_before:
            return
        try:
            frames = self.hal.read_ai_frames(timestamp)
            if not frames:
                return
            normalized_frames: list[AnalogInputFrame] = []
            for frame in frames:
                frame_timestamp = float(frame.timestamp)
                monotonic_ns = int(frame.monotonic_ns)
                ai_epoch = int(frame.ai_epoch)
                sample_sequence = int(frame.sample_sequence)
                ai0 = float(frame.ai0)
                ai6 = None if frame.ai6 is None else float(frame.ai6)
                if (
                    not math.isfinite(frame_timestamp)
                    or not math.isfinite(ai0)
                    or monotonic_ns <= 0
                    or ai_epoch <= 0
                    or sample_sequence < 0
                ):
                    raise ValueError("共享 AI 帧包含非有限的时间戳或 AI0 样本")
                if ai6 is not None and not math.isfinite(ai6):
                    raise ValueError("共享 AI 帧包含非有限的 AI6 样本")
                if ai_epoch < self._last_ai_epoch:
                    raise ValueError("共享 AI epoch 倒退")
                if ai_epoch == self._last_ai_epoch and (
                    sample_sequence <= self._last_ai_sequence
                    or monotonic_ns <= self._last_ai_monotonic_ns
                ):
                    raise ValueError("共享 AI 样本 identity 重复或倒退")
                self._last_ai_epoch = ai_epoch
                self._last_ai_sequence = sample_sequence
                self._last_ai_monotonic_ns = monotonic_ns
                normalized_frames.append(
                    AnalogInputFrame(
                        timestamp=frame_timestamp,
                        ai0=ai0,
                        ai6=ai6,
                        monotonic_ns=monotonic_ns,
                        ai_epoch=ai_epoch,
                        sample_sequence=sample_sequence,
                        origin_uncertainty_ns=int(frame.origin_uncertainty_ns),
                    )
                )

            pulses = []
            breath_frames: list[AnalogInputFrame] = []
            for frame in normalized_frames:
                if self._ai_sample_count % self._breath_emit_every == 0:
                    breath_frames.append(frame)
                self._ai_sample_count += 1
                if frame.ai6 is not None:
                    pulse = self.ttl_service.process_sample(
                        frame.ai6,
                        timestamp=frame.timestamp,
                        monotonic_ns=frame.monotonic_ns,
                    )
                    if pulse is not None:
                        pulses.append(pulse)

            restored = bool(getattr(self.hal, "ttl_input_ready", False))
            if restored != self._ttl_runtime_ready:
                self._ttl_runtime_ready = restored
                self.ttl_readiness_changed.emit(restored)
            if self._ai_error_latched:
                self._ai_error_latched = False
            if breath_frames:
                batch = BreathSampleBatch.from_frames(tuple(breath_frames))
                if self._actuation_sink is not None:
                    self._actuation_sink.post_ai_batch(batch)
                self._record_raw_batch(batch)
                self.breath_samples.emit(batch)
            for pulse in pulses:
                if self._actuation_sink is not None:
                    self._actuation_sink.post_ttl_pulse(pulse)
                self.ttl_pulse.emit(pulse)
        except Exception as exc:  # pragma: no cover - hardware boundary
            first_failure = not self._ai_error_latched
            self._ai_error_latched = True
            self._ai_retry_not_before = attempt_started + self._ai_error_backoff_s
            if not self._release_ai_owned_resources(final=False):
                LOG.error("释放失效的共享 AI task 失败，保持安全阻断")
            if self._ttl_runtime_ready:
                self._ttl_runtime_ready = False
                self.ttl_readiness_changed.emit(False)
            if self._interlock_ingress is not None:
                self._interlock_ingress.update(
                    hardware_ready=False,
                    ttl_input_ready=False,
                    safety_state="DATA_STALE",
                )
            self.disarm_ttl()
            if not first_failure:
                LOG.debug("共享 AI 读取仍未恢复，保持故障锁存并延后重试：%s", exc)
                return
            message = f"TTL/共享 AI 读取失败：{exc}；协议执行已请求安全阻断。"
            if self._actuation_sink is not None:
                self._actuation_sink.post_input_error(message)
            LOG.exception(message)
            self.ttl_input_error.emit(message)

    def _record_raw_batch(self, batch: BreathSampleBatch) -> None:
        with self._ttl_control_lock:
            recorder = self._session_recorder
            if recorder is None:
                return
            self._session_recorder_sequence += 1
            sequence = self._session_recorder_sequence
        if not recorder.post_raw_batch(batch, producer_sequence=sequence):
            sink = self._actuation_sink
            if sink is not None:
                sink.post_recorder_failed(
                    "呼吸原始 batch 无法进入会话记录队列，已请求安全阻断。"
                )

    def _emit_session_fence(self) -> None:
        with self._ttl_control_lock:
            recorder = self._session_recorder
            sequence = self._session_recorder_sequence
            self._session_recorder = None
            self._session_recorder_generation = 0
        if recorder is not None:
            recorder.post_fence("hardware", producer_sequence=sequence)

    def _release_ai_owned_resources(self, *, final: bool) -> bool:
        try:
            result = self.hal.reset_ai_input()
            success = result is True
        except Exception:  # pragma: no cover - defensive
            LOG.exception("HardwareWorker 线程释放 AI 资源失败")
            success = False
        if final:
            self._ai_release_attempted = True
            self._ai_release_success = success
        return success

    def _read_flow(self, timestamp: float | None = None) -> float:
        """Read the serial owner's cached sample; never access HAL serial here."""
        now = time.time() if timestamp is None else float(timestamp)
        with self._flow_sample_lock:
            sample = self._flow_sample
        if sample is None:
            return float("nan")
        airflow, sampled_at = sample
        age = now - sampled_at
        if not math.isfinite(airflow) or not math.isfinite(age):
            return float("nan")
        if age < 0 or age > self._flow_sample_stale_after_s:
            return float("nan")
        return airflow

    @staticmethod
    def _compute_interval_ms(telemetry_hz: int) -> int:
        if telemetry_hz <= 0:
            LOG.warning("Invalid telemetry_hz=%s, falling back to 1 Hz", telemetry_hz)
            return 1000
        interval = int(1000 / telemetry_hz)
        if interval <= 0:
            LOG.warning("telemetry_hz=%s too high, clamping interval to 1 ms", telemetry_hz)
            return 1
        return interval
