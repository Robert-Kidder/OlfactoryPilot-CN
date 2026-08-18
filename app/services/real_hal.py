from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.models import SelfCheckResult, normalize_digital_target
from app.services.hal import AnalogInputFrame, DigitalWriteAck, HalBase
from app.services.ttl_trigger_service import TtlTriggerConfig

try:  # Local hardware drivers; keep import errors explicit for clear startup failures.
    import nidaqmx
    from nidaqmx.constants import READ_ALL_AVAILABLE, AcquisitionType, LineGrouping, TerminalConfiguration
except Exception as exc:  # pragma: no cover - runtime-only dependency
    nidaqmx = None
    READ_ALL_AVAILABLE = None
    AcquisitionType = None
    LineGrouping = None
    TerminalConfiguration = None
    _NIDAQMX_IMPORT_ERROR = exc
else:  # pragma: no cover - runtime-only dependency
    _NIDAQMX_IMPORT_ERROR = None

try:
    import serial
except Exception as exc:  # pragma: no cover - runtime-only dependency
    serial = None
    _SERIAL_IMPORT_ERROR = exc
else:  # pragma: no cover - runtime-only dependency
    _SERIAL_IMPORT_ERROR = None

LOG = logging.getLogger(__name__)

FLOW_FIELD_INDEX = {
    "abs_pressure": 0,
    "temperature": 1,
    "volumetric_flow": 2,
    "mass_flow": 3,
    "setpoint": 4,
}


@dataclass
class _DOPortSession:
    task: object
    device: str
    port: str
    first_line: int
    last_line: int
    states: list[bool]


class RealHAL(HalBase):
    """Real hardware HAL using NI-DAQmx for IO and Alicat RS232 for flow control."""

    def __init__(
        self,
        *,
        ai0_channel: str = "Dev1/ai0",
        ttl_input_channel: str = "Dev1/ai6",
        ttl_poll_hz: int = 1000,
        serial_port: str | None = None,
        baud_rate: int = 19200,
        serial_timeout_s: float = 0.2,
        alicat_unit_ids: dict[str, str] | None = None,
        alicat_flow_unit: str | None = None,
        alicat_flow_field: str = "mass_flow",
        setpoint_verify_tolerance: float = 0.05,
        setpoint_verify_delay_s: float = 0.05,
        setpoint_verify_retries: int = 1,
        alicat_setpoint_scale: float = 0.001,
        alicat_readback_scale: float = 1000.0,
        valve_lines: Iterable[str] | None = None,
        odor_valve_lines: Iterable[str] | None = None,
        monotonic_ns_clock: Callable[[], int] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        if _NIDAQMX_IMPORT_ERROR:
            raise RuntimeError(f"nidaqmx import failed: {_NIDAQMX_IMPORT_ERROR}") from _NIDAQMX_IMPORT_ERROR
        if _SERIAL_IMPORT_ERROR:
            raise RuntimeError(f"pyserial import failed: {_SERIAL_IMPORT_ERROR}") from _SERIAL_IMPORT_ERROR
        if not serial_port:
            raise ValueError("serial_port is required for RealHAL")

        self.ai0_channel = ai0_channel
        self.ttl_input_channel = ttl_input_channel
        self.ttl_poll_hz = max(1, int(ttl_poll_hz))
        self._ttl_input_ready = False
        self.serial_port = serial_port
        self.baud_rate = int(baud_rate)
        self.serial_timeout_s = float(serial_timeout_s)
        self._serial_lock = threading.Lock()
        self._serial = None
        self._ai_task = None
        self._ai_release_failed = False
        self._monotonic_ns_clock = monotonic_ns_clock or time.perf_counter_ns
        self._wall_clock = wall_clock or time.time
        self._ai_epoch = 0
        self._ai_sequence = 0
        self._ai_origin_ns = 0
        self._ai_wall_origin = 0.0
        self._ai_origin_uncertainty_ns = 0
        self._unit_ids = self._normalize_unit_ids(alicat_unit_ids)
        self._flow_unit_id = self._resolve_flow_unit_id(alicat_flow_unit)
        self._flow_field_index = FLOW_FIELD_INDEX.get(alicat_flow_field, 3)
        self._setpoint_verify_tolerance = max(0.0, float(setpoint_verify_tolerance))
        self._setpoint_verify_delay_s = max(0.0, float(setpoint_verify_delay_s))
        self._setpoint_verify_retries = max(1, int(setpoint_verify_retries))
        self._setpoint_scale = float(alicat_setpoint_scale)
        self._readback_scale = float(alicat_readback_scale)
        self._digital_lines = list(valve_lines or [])
        self._odor_valve_lines = list(
            self._digital_lines if odor_valve_lines is None else odor_valve_lines
        )
        self._do_sessions: dict[tuple[str, str], _DOPortSession] = {}
        self._do_owner_thread_id: int | None = None
        self._do_prepare_failed = False
        # HardwareWorker lazily creates the AI task on its own thread.

    @property
    def serial_resources_in_use(self) -> bool:
        """Whether the serial owner currently holds an open COM connection."""
        with self._serial_lock:
            return bool(self._serial is not None and getattr(self._serial, "is_open", True))

    @classmethod
    def from_config(cls, config: dict) -> RealHAL:
        valve_mapping = config.get("valve_mapping") or {}
        odor_valve_lines = _collect_valve_lines(
            valve_mapping,
            hardware_variant=str(config.get("hardware_variant", "20-channel")),
        )
        selector_line = _collect_selector_line(valve_mapping)
        valve_lines = list(odor_valve_lines)
        if selector_line is not None:
            valve_lines.append(selector_line)
        ttl_config = TtlTriggerConfig.from_mapping(config)
        return cls(
            ai0_channel=str(config.get("ai0_channel", "Dev1/ai0")),
            ttl_input_channel=str(config.get("ttl_input_channel", "Dev1/ai6")),
            ttl_poll_hz=ttl_config.poll_hz,
            serial_port=config.get("serial_port"),
            baud_rate=int(config.get("baud_rate", 19200)),
            serial_timeout_s=float(config.get("alicat_timeout_s", 0.2)),
            alicat_unit_ids=config.get("alicat_unit_ids") or None,
            alicat_flow_unit=config.get("alicat_flow_unit"),
            alicat_flow_field=str(config.get("alicat_flow_field", "mass_flow")),
            setpoint_verify_tolerance=float(config.get("alicat_setpoint_tolerance", 0.05)),
            setpoint_verify_delay_s=float(config.get("alicat_setpoint_verify_delay_s", 0.05)),
            setpoint_verify_retries=int(config.get("alicat_setpoint_verify_retries", 3)),
            alicat_setpoint_scale=float(config.get("alicat_setpoint_scale", 0.001)),
            alicat_readback_scale=float(config.get("alicat_readback_scale", 1000.0)),
            valve_lines=valve_lines,
            odor_valve_lines=odor_valve_lines,
        )

    def read_ai0(self, timestamp: float | None = None) -> float:
        return self.read_ai_frame(timestamp).ai0

    @property
    def ttl_input_ready(self) -> bool:
        return self._ttl_input_ready

    def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
        task = self._ensure_ai_task()
        values = task.read()
        frame_timestamp, monotonic_ns, sequence = self._next_ai_identity()
        if self._ttl_input_ready:
            if not isinstance(values, list | tuple) or len(values) < 2:
                raise RuntimeError("共享 AI task 未返回 AI0/AI6 两通道样本")
            return AnalogInputFrame(
                timestamp=frame_timestamp,
                ai0=float(values[0]),
                ai6=float(values[1]),
                monotonic_ns=monotonic_ns,
                ai_epoch=self._ai_epoch,
                sample_sequence=sequence,
                origin_uncertainty_ns=self._ai_origin_uncertainty_ns,
            )
        value = values[0] if isinstance(values, list | tuple) else values
        return AnalogInputFrame(
            timestamp=frame_timestamp,
            ai0=float(value),
            ai6=None,
            monotonic_ns=monotonic_ns,
            ai_epoch=self._ai_epoch,
            sample_sequence=sequence,
            origin_uncertainty_ns=self._ai_origin_uncertainty_ns,
        )

    def read_ai_frames(self, timestamp: float | None = None) -> list[AnalogInputFrame]:
        """Drain every currently buffered sample so the 1 kHz producer cannot outrun the worker."""
        task = self._ensure_ai_task()
        values = task.read(number_of_samples_per_channel=READ_ALL_AVAILABLE)
        if self._ttl_input_ready:
            if not isinstance(values, list | tuple) or len(values) < 2:
                raise RuntimeError("共享 AI task 未返回 AI0/AI6 两通道样本")
            ai0_values = list(values[0])
            ai6_values = list(values[1])
            if len(ai0_values) != len(ai6_values):
                raise RuntimeError("共享 AI task 返回的 AI0/AI6 样本数不一致")
            return self._build_ai_frames(ai0_values, ai6_values)
        ai0_values = list(values) if isinstance(values, list | tuple) else [values]
        return self._build_ai_frames(ai0_values, None)

    def reset_ai_input(self) -> bool:
        task = self._ai_task
        if task is None:
            self._ttl_input_ready = False
            self._ai_sequence = 0
            return True
        try:
            task.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to reset NI-DAQmx AI task")
            # Keep the reference: the driver may still reserve the device and
            # shutdown must not report a successful ownership handoff.
            self._ttl_input_ready = False
            self._ai_release_failed = True
            return False
        self._ai_task = None
        self._ai_release_failed = False
        self._ttl_input_ready = False
        self._ai_sequence = 0
        return True

    def _build_ai_frames(
        self,
        ai0_values: list,
        ai6_values: list | None,
    ) -> list[AnalogInputFrame]:
        count = len(ai0_values)
        if count == 0:
            return []
        frames: list[AnalogInputFrame] = []
        for index, ai0 in enumerate(ai0_values):
            timestamp, monotonic_ns, sequence = self._next_ai_identity()
            frames.append(
                AnalogInputFrame(
                    timestamp=timestamp,
                    ai0=float(ai0),
                    ai6=float(ai6_values[index]) if ai6_values is not None else None,
                    monotonic_ns=monotonic_ns,
                    ai_epoch=self._ai_epoch,
                    sample_sequence=sequence,
                    origin_uncertainty_ns=self._ai_origin_uncertainty_ns,
                )
            )
        return frames

    def _next_ai_identity(self) -> tuple[float, int, int]:
        sequence = self._ai_sequence
        interval_ns = 1_000_000_000 // self.ttl_poll_hz
        monotonic_ns = self._ai_origin_ns + sequence * interval_ns
        timestamp = self._ai_wall_origin + sequence / self.ttl_poll_hz
        self._ai_sequence += 1
        return timestamp, monotonic_ns, sequence

    def read_flow(self) -> float:
        unit_id = self._flow_unit_id
        if not unit_id:
            return 0.0
        response = self._query_serial(f"{unit_id}\r")
        if not response:
            return 0.0
        value = self._parse_flow_value(response, unit_id)
        return float(value) * self._readback_scale

    def set_flow(self, channel: str | float, value: float | None = None, *, comp: bool = False) -> bool:
        if value is None:
            value = float(channel)
            channel = "A"
        unit_id = self._resolve_unit_id(channel)
        if not unit_id:
            LOG.warning("Unknown flow channel %s; check alicat_unit_ids", channel)
            return False
        target = float(value)
        device_target = target * self._setpoint_scale
        try:
            command = f"{unit_id}s{device_target:.3f}\r"
            LOG.info(
                "Alicat setpoint command | channel=%s | unit=%s | target_sccm=%.3f | device_target=%.3f",
                channel,
                unit_id,
                target,
                device_target,
            )
            self._write_serial(command)
            readback = None
            response = None
            for attempt in range(1, self._setpoint_verify_retries + 1):
                if self._setpoint_verify_delay_s:
                    time.sleep(self._setpoint_verify_delay_s)
                readback, response = self._read_setpoint(unit_id)
                if readback is None:
                    LOG.warning(
                        "Alicat setpoint no readback | channel=%s | unit=%s | target_sccm=%.3f | device_target=%.3f | attempt=%s | response=%r",
                        channel,
                        unit_id,
                        target,
                        device_target,
                        attempt,
                        response,
                    )
                    continue
                if math.isclose(readback, device_target, abs_tol=self._setpoint_verify_tolerance):
                    LOG.info(
                        "Alicat setpoint verified | channel=%s | unit=%s | target_sccm=%.3f | device_target=%.3f | readback=%.3f | attempt=%s",
                        channel,
                        unit_id,
                        target,
                        device_target,
                        readback,
                        attempt,
                    )
                    return True
            LOG.warning(
                "Alicat setpoint mismatch | channel=%s | unit=%s | target_sccm=%.3f | device_target=%.3f | readback=%s | response=%r",
                channel,
                unit_id,
                target,
                device_target,
                readback,
                response,
            )
            return False
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to set flow on channel %s", channel)
            return False

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        if self._digital_lines:
            return self.write_digital_ack(
                device=device,
                line=line,
                state=state,
                timeout_ms=100,
            ).success
        if LineGrouping is None:
            raise RuntimeError("nidaqmx is unavailable for digital output")
        line = _normalize_digital_line(line)
        channel = f"{device}/{line}" if device else line
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(channel, line_grouping=LineGrouping.CHAN_PER_LINE)
                task.write(bool(state))
            return True
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Digital write failed for %s", channel)
            return False

    def write_digital_ack(
        self,
        *,
        device: str | None,
        line: str,
        state: bool,
        timeout_ms: int,
    ) -> DigitalWriteAck:
        owner = threading.get_ident()
        if self._do_owner_thread_id != owner:
            return DigitalWriteAck(
                success=False,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                message="DO task 所有权不属于当前线程，已拒绝跨线程写入。",
            )
        normalized = _normalize_digital_line(line)
        parsed = _parse_port_line(normalized)
        if device is None or parsed is None:
            return DigitalWriteAck(
                success=False,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                message=f"数字输出目标无效：{device}/{line}",
            )
        port, bit = parsed
        session = self._do_sessions.get((device, port))
        if session is None or bit < session.first_line or bit > session.last_line:
            return DigitalWriteAck(
                success=False,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                message=f"DO task 尚未准备或不包含目标：{device}/{normalized}",
            )
        candidate = list(session.states)
        candidate[bit - session.first_line] = bool(state)
        # CHAN_FOR_ALL_LINES expects a packed integer for a multi-line port
        # channel, but nidaqmx's single-line writer accepts only a scalar bool.
        # A list[bool] is samples, not the simultaneous port state.
        packed_state = sum(1 << index for index, enabled in enumerate(candidate) if enabled)
        write_value: bool | int = bool(candidate[0]) if len(candidate) == 1 else packed_state
        started_ns = int(self._monotonic_ns_clock())
        try:
            session.task.write(
                write_value,
                auto_start=False,
                timeout=max(0.001, int(timeout_ms) / 1000),
            )
        except Exception as exc:
            # A failed close is physically uncertain.  Keeping the previous
            # cached True bit could reassert that valve on the next packed-port
            # write, so fail the target's software intent toward the safe state.
            if not state:
                session.states[bit - session.first_line] = False
            return DigitalWriteAck(
                success=False,
                started_ns=started_ns,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                message=f"NI-DAQmx 数字输出异常：{exc}",
                uncertain=True,
            )
        actual_ns = int(self._monotonic_ns_clock())
        session.states = candidate
        return DigitalWriteAck(
            success=True,
            started_ns=started_ns,
            actual_ns=actual_ns,
            wall_timestamp=self._wall_clock(),
            message="ok",
        )

    def prepare_do_output(self) -> bool:
        owner = threading.get_ident()
        if self._do_sessions:
            if self._do_prepare_failed:
                return False
            return self._do_owner_thread_id == owner
        if LineGrouping is None or not hasattr(LineGrouping, "CHAN_FOR_ALL_LINES"):
            return False
        groups: dict[tuple[str, str], set[int]] = {}
        for target in self._digital_lines:
            device, line = _split_target(target)
            parsed = _parse_port_line(_normalize_digital_line(line))
            if device is None or parsed is None:
                LOG.error("无法准备 DO 映射：%s", target)
                return False
            port, bit = parsed
            groups.setdefault((device, port), set()).add(bit)
        for (device, port), bits in sorted(groups.items()):
            first = min(bits)
            last = max(bits)
            if bits != set(range(first, last + 1)):
                LOG.error(
                    "DO 映射 %s/%s 必须连续；拒绝隐式占用未配置线路：%s",
                    device,
                    port,
                    sorted(bits),
                )
                return False
        created: list[_DOPortSession] = []
        try:
            for (device, port), bits in sorted(groups.items()):
                first = min(bits)
                last = max(bits)
                suffix = f"line{first}" if first == last else f"line{first}:{last}"
                task = nidaqmx.Task()
                session = _DOPortSession(
                    task=task,
                    device=device,
                    port=port,
                    first_line=first,
                    last_line=last,
                    states=[False] * (last - first + 1),
                )
                # Track the task immediately so add_do_chan/start failures also
                # participate in rollback.
                created.append(session)
                task.do_channels.add_do_chan(
                    f"{device}/{port}/{suffix}",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                if hasattr(task, "start"):
                    task.start()
        except Exception:
            LOG.exception("预建 NI-DAQmx DO task 失败")
            failed: list[_DOPortSession] = []
            for session in created:
                try:
                    session.task.close()
                except Exception:
                    LOG.exception("回滚 DO task 失败")
                    failed.append(session)
            if failed:
                self._do_sessions = {(item.device, item.port): item for item in failed}
                self._do_owner_thread_id = owner
                self._do_prepare_failed = True
            return False
        self._do_sessions = {(item.device, item.port): item for item in created}
        self._do_owner_thread_id = owner
        self._do_prepare_failed = False
        return True

    def release_do_output(self) -> bool:
        owner = threading.get_ident()
        if self._do_sessions and self._do_owner_thread_id != owner:
            raise RuntimeError("DO task 所有权不属于当前线程，不能跨线程释放。")
        sessions = list(self._do_sessions.values())
        failed: list[_DOPortSession] = []
        for session in sessions:
            try:
                session.task.close()
            except Exception:
                LOG.exception("释放 NI-DAQmx DO task 失败")
                failed.append(session)
        self._do_sessions = {(item.device, item.port): item for item in failed}
        if failed:
            # Preserve ownership: another thread must not create a replacement
            # task while NI-DAQmx may still hold the original reservation.
            self._do_prepare_failed = True
            return False
        self._do_owner_thread_id = None
        self._do_prepare_failed = False
        return True

    def close_all(self) -> bool:
        if self._odor_valve_lines and not self._do_sessions and not self.prepare_do_output():
            return False
        success = True
        for target in self._odor_valve_lines:
            device, line = _split_target(target)
            if not self.write_digital(device=device, line=line, state=False):
                success = False
        return success

    def stop_heaters(self) -> bool:
        return True

    def flush_logs(self) -> None:
        return None

    def self_check(self) -> tuple[list[SelfCheckResult], bool]:
        now_ts = time.time()
        results: list[SelfCheckResult] = []
        ready = True

        try:
            _ = self.read_ai0()
            results.append(
                SelfCheckResult(
                    name="ai0",
                    type="nidaq",
                    status="PASS",
                    reason="AI read OK",
                    suggestion="none",
                    checked_at=now_ts,
                )
            )
        except Exception as exc:
            ready = False
            results.append(
                SelfCheckResult(
                    name="ai0",
                    type="nidaq",
                    status="FAIL",
                    reason=f"AI read failed: {exc}",
                    suggestion="check NI-DAQmx and AI channel mapping",
                    checked_at=now_ts,
                )
            )

        try:
            if self._flow_unit_id:
                _ = self.read_flow()
            results.append(
                SelfCheckResult(
                    name="alicat",
                    type="serial",
                    status="PASS",
                    reason="RS232 polling OK",
                    suggestion="none",
                    checked_at=now_ts,
                )
            )
        except Exception as exc:
            ready = False
            results.append(
                SelfCheckResult(
                    name="alicat",
                    type="serial",
                    status="FAIL",
                    reason=f"RS232 polling failed: {exc}",
                    suggestion="check COM port and cabling",
                    checked_at=now_ts,
                )
            )

        return results, ready

    def _ensure_ai_task(self):
        if self._ai_release_failed:
            raise RuntimeError("previous NI-DAQmx AI task release failed; refusing task reuse")
        if self._ai_task is None:
            task = nidaqmx.Task()
            try:
                task.ai_channels.add_ai_voltage_chan(
                    self.ai0_channel,
                    terminal_config=TerminalConfiguration.RSE,
                )
                task.ai_channels.add_ai_voltage_chan(
                    self.ttl_input_channel,
                    terminal_config=TerminalConfiguration.RSE,
                )
                if hasattr(task, "timing"):
                    task.timing.cfg_samp_clk_timing(
                        rate=self.ttl_poll_hz,
                        sample_mode=AcquisitionType.CONTINUOUS,
                        samps_per_chan=self.ttl_poll_hz,
                    )
                self._start_ai_task(task)
                self._ai_task = task
                self._ttl_input_ready = True
            except Exception as exc:
                LOG.warning("AI6 初始化失败，已安全降级为 AI0-only：%s", exc)
                try:
                    task.close()
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("关闭部分创建的共享 AI task 失败")
                fallback = nidaqmx.Task()
                fallback.ai_channels.add_ai_voltage_chan(
                    self.ai0_channel,
                    terminal_config=TerminalConfiguration.RSE,
                )
                if hasattr(fallback, "timing"):
                    fallback.timing.cfg_samp_clk_timing(
                        rate=self.ttl_poll_hz,
                        sample_mode=AcquisitionType.CONTINUOUS,
                        samps_per_chan=self.ttl_poll_hz,
                    )
                self._start_ai_task(fallback)
                self._ai_task = fallback
                self._ttl_input_ready = False
            self._ai_release_failed = False
        return self._ai_task

    def _start_ai_task(self, task) -> None:
        before_ns = int(self._monotonic_ns_clock())
        wall_origin = float(self._wall_clock())
        if hasattr(task, "start"):
            task.start()
        after_ns = int(self._monotonic_ns_clock())
        if after_ns < before_ns:
            raise RuntimeError("AI task 启动期间单调时钟倒退")
        self._ai_epoch += 1
        self._ai_sequence = 0
        self._ai_origin_ns = before_ns + ((after_ns - before_ns) // 2)
        self._ai_origin_uncertainty_ns = (after_ns - before_ns) // 2
        self._ai_wall_origin = wall_origin

    def _close_resources(self) -> None:
        self.reset_ai_input()
        self.release_serial_resources()

    def release_serial_resources(self) -> None:
        try:
            with self._serial_lock:
                if self._serial is not None:
                    self._serial.close()
                self._serial = None
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to close serial port")

    def _normalize_unit_ids(self, mapping: dict[str, str] | None) -> dict[str, str]:
        if not mapping:
            return {"A": "a", "B": "b", "C": "c"}
        normalized: dict[str, str] = {}
        for key, value in mapping.items():
            normalized[str(key).upper()] = str(value)
        return normalized

    def _resolve_unit_id(self, channel: str | float) -> str | None:
        if isinstance(channel, str):
            key = channel.strip().upper()
        else:
            key = str(channel).strip().upper()
        return self._unit_ids.get(key)

    def _resolve_flow_unit_id(self, flow_unit: str | None) -> str | None:
        if not flow_unit:
            return next(iter(self._unit_ids.values()), None)
        flow_unit = str(flow_unit).strip()
        mapped = self._unit_ids.get(flow_unit.upper())
        return mapped or flow_unit

    def _write_serial(self, command: str) -> None:
        payload = command.encode("ascii", errors="ignore")
        with self._serial_lock:
            connection = self._ensure_serial()
            connection.reset_input_buffer()
            connection.write(payload)
            connection.flush()

    def _query_serial(self, command: str) -> str | None:
        payload = command.encode("ascii", errors="ignore")
        with self._serial_lock:
            connection = self._ensure_serial()
            connection.reset_input_buffer()
            connection.write(payload)
            connection.flush()
            response = connection.readline()
        if not response:
            return None
        return response.decode("ascii", errors="ignore").strip()

    def _ensure_serial(self):
        if self._serial is not None and getattr(self._serial, "is_open", True):
            return self._serial
        self._serial = serial.Serial(
            port=self.serial_port,
            baudrate=self.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.serial_timeout_s,
        )
        return self._serial

    def _parse_flow_value(self, response: str, unit_id: str) -> float:
        value = self._parse_frame_value(response, unit_id, self._flow_field_index)
        return 0.0 if value is None else value

    def _read_setpoint(self, unit_id: str) -> tuple[float | None, str | None]:
        response = self._query_serial(f"{unit_id}\r")
        if not response:
            return None, response
        return self._parse_frame_value(response, unit_id, FLOW_FIELD_INDEX["setpoint"]), response

    def _parse_frame_value(self, response: str, unit_id: str, index: int) -> float | None:
        tokens = response.split()
        if not tokens:
            return None
        if tokens[0].lower() == unit_id.lower():
            tokens = tokens[1:]
        values: list[float] = []
        for token in tokens:
            try:
                values.append(float(token))
            except ValueError:
                continue
        if len(values) <= index:
            return None
        return float(values[index])


def _collect_valve_lines(
    valve_mapping: dict, *, hardware_variant: str = "20-channel"
) -> list[str]:
    lines: set[str] = set()
    selector_target = _collect_selector_line(valve_mapping)
    selector_identity = (
        None
        if selector_target is None
        else normalize_digital_target(selector_target)
    )
    variants = valve_mapping.get("variants") or {}
    if isinstance(variants, dict):
        for mapping in variants.values():
            if not isinstance(mapping, dict):
                continue
            for channel, line in mapping.items():
                try:
                    channel_id = int(channel)
                    identity = normalize_digital_target(str(line))
                except (TypeError, ValueError, OverflowError):
                    continue
                if 1 <= channel_id <= 20 and identity != selector_identity:
                    lines.add(str(line))
    return sorted(lines)


def _collect_selector_line(valve_mapping: dict) -> str | None:
    selector = valve_mapping.get("selector") or {}
    target = (
        selector.get("target") if isinstance(selector, dict) else None
    ) or valve_mapping.get("master_valve")
    return None if not target else str(target)


def _split_target(target: str) -> tuple[str | None, str]:
    if "/" in target:
        device, line = target.split("/", 1)
        return device, line
    return None, target


def _normalize_digital_line(line: str) -> str:
    """Convert config shorthand like P1.0 to NI-DAQmx port1/line0."""
    normalized = str(line).strip()
    lower = normalized.lower()
    if lower.startswith("port") or "/" in normalized:
        return normalized
    if lower.startswith("p") and "." in lower:
        port, bit = lower[1:].split(".", 1)
        if port.isdigit() and bit.isdigit():
            return f"port{int(port)}/line{int(bit)}"
    return normalized


def _parse_port_line(line: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(port\d+)/line(\d+)", str(line).strip().lower())
    if match is None:
        return None
    return match.group(1), int(match.group(2))
