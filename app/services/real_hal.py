from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.models import SelfCheckResult, normalize_digital_target
from app.services.alicat_serial import (
    AlicatResponseMismatch,
    AlicatSerialSession,
    AlicatSessionDesynchronized,
    initial_resynchronization_windows,
    quantize_setpoint_for_wire,
)
from app.services.hal import (
    AnalogInputFrame,
    DigitalWriteAck,
    FlowChannelReadback,
    FlowReadbackSnapshot,
    HalBase,
)
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
    session_id: str
    device: str
    port: str
    first_line: int
    last_line: int
    states: list[bool]
    prepared: bool = False
    safe_image_written: bool = False
    running: bool = False
    released: bool = False
    safe_write_started_ns: int | None = None
    safe_write_actual_ns: int | None = None


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
        digital_safe_levels: dict[str, bool] | None = None,
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
        self._serial_lock = threading.RLock()
        self._serial = None
        self._alicat_session: AlicatSerialSession | None = None
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
        # Retained for config/backward compatibility only. Strict transaction
        # failures and mismatched readbacks are never retried within a session.
        self._setpoint_verify_retries = max(1, int(setpoint_verify_retries))
        self._setpoint_scale = float(alicat_setpoint_scale)
        self._readback_scale = float(alicat_readback_scale)
        self._setpoint_readbacks_sccm: dict[str, float] = {}
        self._setpoint_tx_monotonic_ns: dict[str, int] = {}
        self._digital_lines = list(valve_lines or [])
        self._odor_valve_lines = list(
            self._digital_lines if odor_valve_lines is None else odor_valve_lines
        )
        configured_safe_levels = digital_safe_levels or {}
        self._digital_safe_levels = {
            normalize_digital_target(target): bool(
                configured_safe_levels.get(
                    target,
                    configured_safe_levels.get(normalize_digital_target(target), False),
                )
            )
            for target in self._digital_lines
        }
        self._do_sessions: dict[tuple[str, str], _DOPortSession] = {}
        self._do_owner_thread_id: int | None = None
        self._do_prepare_failed = False
        self._do_acquisition_sequence = 0
        # HardwareWorker lazily creates the AI task on its own thread.

    @property
    def serial_resources_in_use(self) -> bool:
        """Whether the serial owner currently holds an open COM connection."""
        with self._serial_lock:
            return bool(self._serial is not None and getattr(self._serial, "is_open", True))

    @property
    def serial_desynchronized(self) -> bool:
        with self._serial_lock:
            return bool(
                self._alicat_session is not None
                and self._alicat_session.desynchronized
            )

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
        digital_safe_levels = _collect_digital_safe_levels(
            config,
            odor_valve_lines=odor_valve_lines,
            selector_line=selector_line,
        )
        ttl_config = TtlTriggerConfig.from_mapping(config)
        _validate_alicat_deadline_compatibility(config)
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
            digital_safe_levels=digital_safe_levels,
        )

    @property
    def power_on_safe_compatible(self) -> bool:
        """Whether weak pull-down leaves every owned actuator in its safe state."""

        return not any(self._digital_safe_levels.values())

    @property
    def power_on_safety_blockers(self) -> tuple[str, ...]:
        return tuple(
            target
            for target in self._digital_lines
            if self._digital_safe_levels[normalize_digital_target(target)]
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
        frame = self._transact_poll(unit_id)
        value = self._parse_flow_value(frame.text, unit_id)
        return float(value) * self._readback_scale

    def read_flow_snapshot(self) -> FlowReadbackSnapshot:
        """Poll A/B/C serially through this HAL's one Alicat session."""

        readings: list[FlowChannelReadback] = []
        for channel in ("A", "B", "C"):
            unit_id = self._resolve_unit_id(channel)
            if not unit_id:
                raise AlicatResponseMismatch(f"Alicat {channel} unit ID is not configured")
            frame = self._transact_poll(unit_id)
            readings.append(self._channel_readback(channel, unit_id, frame))
        captured_ns = max(reading.monotonic_ns for reading in readings)
        captured_at = max(reading.wall_timestamp for reading in readings)
        return FlowReadbackSnapshot(
            readings=tuple(readings),
            wall_timestamp=captured_at,
            monotonic_ns=captured_ns,
            fresh=True,
        )

    def set_flow(self, channel: str | float, value: float | None = None, *, comp: bool = False) -> bool:
        if value is None:
            value = float(channel)
            channel = "A"
        unit_id = self._resolve_unit_id(channel)
        normalized_channel = str(channel).upper()
        self._setpoint_readbacks_sccm.pop(normalized_channel, None)
        if not unit_id:
            LOG.warning("Unknown flow channel %s; check alicat_unit_ids", channel)
            return False
        target = float(value)
        device_target = target * self._setpoint_scale
        wire_target = quantize_setpoint_for_wire(device_target)
        try:
            LOG.info(
                "Alicat setpoint command | channel=%s | unit=%s | target_sccm=%.3f | "
                "device_target=%.6f | wire_target=%.3f",
                channel,
                unit_id,
                target,
                device_target,
                wire_target,
            )
            session = self._ensure_alicat_session()
            command_frame = session.set_setpoint(
                unit_id,
                wire_target,
                tolerance=self._setpoint_verify_tolerance,
            )
            self._setpoint_tx_monotonic_ns[normalized_channel] = int(command_frame.tx_ns)
            if self._setpoint_verify_delay_s:
                time.sleep(self._setpoint_verify_delay_s)
            readback, response = self._read_setpoint(unit_id, expected=wire_target)
            self._setpoint_readbacks_sccm[normalized_channel] = (
                float(readback) * self._readback_scale
            )
            LOG.info(
                "Alicat setpoint verified | channel=%s | unit=%s | target_sccm=%.3f | "
                "device_target=%.6f | wire_target=%.3f | readback=%.3f | "
                "attempt=1 | response=%r",
                channel,
                unit_id,
                target,
                device_target,
                wire_target,
                readback,
                response,
            )
            return True
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to set flow on channel %s", channel)
            return False

    def last_setpoint_readback_sccm(self, channel: str) -> float | None:
        return self._setpoint_readbacks_sccm.get(str(channel).upper())

    def last_setpoint_tx_monotonic_ns(self, channel: str) -> int | None:
        return self._setpoint_tx_monotonic_ns.get(str(channel).upper())

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
        if not (
            session.prepared
            and session.safe_image_written
            and session.running
            and not session.released
        ):
            return DigitalWriteAck(
                success=False,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=self._wall_clock(),
                message=f"DO session 不在可写运行状态：{session.session_id}",
                uncertain=True,
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
            if getattr(exc, "error_code", None) == -200846:
                # NI-DAQmx has explicitly reported that this On-Demand task is
                # not Running. Latch that lifecycle fact; never auto-start or
                # retry an action whose first physical result is uncertain.
                session.running = False
                LOG.error(
                    "DO session no longer running | session=%s | error_code=-200846",
                    session.session_id,
                )
            # A failed close is physically uncertain.  Keeping the previous
            # cached True bit could reassert that valve on the next packed-port
            # write, so fail the target's software intent toward the safe state.
            target = normalize_digital_target(f"{device}/{normalized}")
            safe_level = self._digital_safe_levels[target]
            if bool(state) == safe_level:
                session.states[bit - session.first_line] = safe_level
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
            return self._do_owner_thread_id == owner and all(
                session.prepared
                and session.safe_image_written
                and session.running
                and not session.released
                for session in self._do_sessions.values()
            )
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
        self._do_acquisition_sequence += 1
        acquisition_sequence = self._do_acquisition_sequence
        try:
            for (device, port), bits in sorted(groups.items()):
                first = min(bits)
                last = max(bits)
                suffix = f"line{first}" if first == last else f"line{first}:{last}"
                task = nidaqmx.Task()
                safe_states = [
                    self._digital_safe_levels[
                        normalize_digital_target(f"{device}/{port}/line{bit}")
                    ]
                    for bit in range(first, last + 1)
                ]
                session = _DOPortSession(
                    task=task,
                    session_id=f"do-{acquisition_sequence}-{device}-{port}",
                    device=device,
                    port=port,
                    first_line=first,
                    last_line=last,
                    states=safe_states,
                )
                # Track the task immediately so add_do_chan/start failures also
                # participate in rollback.
                created.append(session)
                task.do_channels.add_do_chan(
                    f"{device}/{port}/{suffix}",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                session.prepared = True
            # NI-DAQmx documents that auto_start=True implicitly starts a task.
            # For software-timed DO, make that first physical drive the complete
            # safe packed image; never start a task before its safe value exists.
            for session in created:
                packed_state = sum(
                    1 << index
                    for index, enabled in enumerate(session.states)
                    if enabled
                )
                write_value: bool | int = (
                    bool(session.states[0])
                    if len(session.states) == 1
                    else packed_state
                )
                session.safe_write_started_ns = int(self._monotonic_ns_clock())
                try:
                    session.task.write(write_value, auto_start=True, timeout=1.0)
                except Exception as exc:
                    LOG.exception(
                        "DO safe image | device=%s | port=%s | lines=%s:%s | "
                        "logical_safe_states=%s | packed=0x%X | result=failed | "
                        "session=%s | started_ns=%s | actual_ns=none | error=%s",
                        session.device,
                        session.port,
                        session.first_line,
                        session.last_line,
                        "".join("1" if state else "0" for state in session.states),
                        packed_state,
                        session.session_id,
                        session.safe_write_started_ns,
                        exc,
                    )
                    raise
                session.safe_write_actual_ns = int(self._monotonic_ns_clock())
                session.safe_image_written = True
                LOG.info(
                    "DO safe image | device=%s | port=%s | lines=%s:%s | "
                    "logical_safe_states=%s | packed=0x%X | result=success | "
                    "session=%s | started_ns=%s | actual_ns=%s",
                    session.device,
                    session.port,
                    session.first_line,
                    session.last_line,
                    "".join("1" if state else "0" for state in session.states),
                    packed_state,
                    session.session_id,
                    session.safe_write_started_ns,
                    session.safe_write_actual_ns,
                )
                # An auto-started single-point On-Demand write does not provide
                # a persistent Running task for later auto_start=False writes.
                # Start only after the first physical drive is the safe image.
                session.task.start()
                session.running = True
                LOG.info("DO session running | session=%s", session.session_id)
        except Exception:
            LOG.exception("预建 NI-DAQmx DO task 失败")
            failed: list[_DOPortSession] = []
            for session in created:
                if not self._close_do_session(session, reason="prepare_rollback"):
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
            if not self._close_do_session(session, reason="owner_handoff"):
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

    def _close_do_session(self, session: _DOPortSession, *, reason: str) -> bool:
        """Close one DO task and emit one low-frequency ownership audit record."""

        close_started_ns = int(self._monotonic_ns_clock())
        try:
            session.task.close()
        except Exception as exc:
            LOG.exception(
                "DO session release | session=%s | device=%s | port=%s | "
                "reason=%s | close_started_ns=%s | close_actual_ns=none | "
                "result=failed | error=%s",
                session.session_id,
                session.device,
                session.port,
                reason,
                close_started_ns,
                exc,
            )
            return False
        close_actual_ns = int(self._monotonic_ns_clock())
        session.running = False
        session.released = True
        LOG.info(
            "DO session release | session=%s | device=%s | port=%s | "
            "reason=%s | close_started_ns=%s | close_actual_ns=%s | "
            "result=success",
            session.session_id,
            session.device,
            session.port,
            reason,
            close_started_ns,
            close_actual_ns,
        )
        return True

    @property
    def do_resources_in_use(self) -> bool:
        """Expose retained tasks without transferring their thread ownership."""

        return bool(self._do_sessions)

    def close_all(self) -> bool:
        if self._odor_valve_lines and not self._do_sessions and not self.prepare_do_output():
            return False
        success = True
        for target in self._odor_valve_lines:
            device, line = _split_target(target)
            safe_level = self._digital_safe_levels[normalize_digital_target(target)]
            if not self.write_digital(device=device, line=line, state=safe_level):
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
                self._alicat_session = None
                self._setpoint_tx_monotonic_ns.clear()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to close serial port")

    def _normalize_unit_ids(self, mapping: dict[str, str] | None) -> dict[str, str]:
        if not mapping:
            return {"A": "a", "B": "b", "C": "c"}
        normalized: dict[str, str] = {}
        for key, value in mapping.items():
            unit_id = str(value).strip()
            if len(unit_id) != 1 or not unit_id.isascii() or not unit_id.isalpha():
                raise ValueError(f"Alicat Unit ID 无效：{value!r}")
            normalized[str(key).upper()] = unit_id
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

    def _ensure_alicat_session(self) -> AlicatSerialSession:
        with self._serial_lock:
            if self._alicat_session is not None:
                if self._alicat_session.desynchronized:
                    raise AlicatSessionDesynchronized(
                        "Alicat serial session is desynchronized; explicit release is required"
                    )
                if not getattr(self._alicat_session.connection, "is_open", True):
                    raise AlicatSessionDesynchronized(
                        "Alicat serial transport closed unexpectedly; explicit release is required"
                    )
                return self._alicat_session
            connection = self._ensure_serial()
            self._alicat_session = AlicatSerialSession(
                connection,
                baud_rate=self.baud_rate,
                frame_timeout_s=self.serial_timeout_s,
                lock=self._serial_lock,
                monotonic_ns=self._monotonic_ns_clock,
            )
            return self._alicat_session

    def _transact_poll(self, unit_id: str):
        return self._ensure_alicat_session().poll(unit_id)

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
        if value is None:
            raise AlicatResponseMismatch("Alicat flow frame does not contain the configured field")
        return value

    def _read_setpoint(self, unit_id: str, *, expected: float) -> tuple[float, str]:
        frame = self._ensure_alicat_session().poll(
            unit_id,
            expected_setpoint=expected,
            setpoint_tolerance=self._setpoint_verify_tolerance,
        )
        value = self._parse_frame_value(frame.text, unit_id, FLOW_FIELD_INDEX["setpoint"])
        if value is None:
            raise AlicatResponseMismatch("Alicat setpoint poll has no setpoint field")
        return value, frame.text

    def _parse_frame_value(self, response: str, unit_id: str, index: int) -> float | None:
        tokens = response.split()
        if not tokens:
            return None
        if tokens[0].lower() != unit_id.lower():
            return None
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

    def _channel_readback(self, channel: str, unit_id: str, frame) -> FlowChannelReadback:
        tokens = frame.text.split()
        if len(tokens) < 7 or tokens[0].casefold() != unit_id.casefold():
            raise AlicatResponseMismatch("Alicat Poll frame is incomplete")
        try:
            mass_flow = float(tokens[1 + FLOW_FIELD_INDEX["mass_flow"]])
            setpoint = float(tokens[1 + FLOW_FIELD_INDEX["setpoint"]])
        except (IndexError, ValueError) as exc:
            raise AlicatResponseMismatch("Alicat Poll flow fields are invalid") from exc
        return FlowChannelReadback(
            channel=channel,
            unit_id=unit_id,
            setpoint_sccm=setpoint * self._readback_scale,
            mass_flow_sccm=mass_flow * self._readback_scale,
            gas=tokens[6],
            wall_timestamp=float(self._wall_clock()),
            monotonic_ns=int(frame.rx_ns),
            raw_frame=frame.text,
            fresh=True,
        )


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


def _validate_alicat_deadline_compatibility(config: dict) -> None:
    """Reject serial timing that cannot fit existing safety-owner deadlines."""

    frame_s = float(config.get("alicat_timeout_s", 0.2))
    verify_delay_raw = float(config.get("alicat_setpoint_verify_delay_s", 0.05))
    if not math.isfinite(frame_s) or frame_s <= 0:
        raise ValueError("alicat_timeout_s 必须大于 0")
    if not math.isfinite(verify_delay_raw) or verify_delay_raw < 0:
        raise ValueError("alicat_setpoint_verify_delay_s 必须为有限非负数")
    verify_delay_s = verify_delay_raw
    single_setpoint_budget_s = (2.0 * frame_s) + verify_delay_s
    final_zero_budget_s = 3.0 * single_setpoint_budget_s
    shutdown_timeout_ms = float(config.get("actuation_shutdown_timeout_ms", 2000))
    if not math.isfinite(shutdown_timeout_ms) or shutdown_timeout_ms <= 0:
        raise ValueError("actuation_shutdown_timeout_ms 必须为有限正数")
    shutdown_budget_s = shutdown_timeout_ms / 1000.0
    if final_zero_budget_s >= shutdown_budget_s:
        raise ValueError(
            "Alicat frame deadline 与清零验证时序无法容纳在 Global Stop deadline 内"
        )

    _, initial_sync_budget_s = initial_resynchronization_windows(
        baud_rate=int(config.get("baud_rate", 19200)),
        frame_timeout_s=frame_s,
    )
    # Self-check and a fresh readiness poll each consume one frame deadline.
    # This only guards the serial portion and deliberately leaves the remaining
    # Connect budget to NI and UI.
    startup_serial_budget_s = (
        initial_sync_budget_s + frame_s + final_zero_budget_s + frame_s
    )
    connect_timeout_ms = float(config.get("hardware_connect_timeout_ms", 10000))
    if not math.isfinite(connect_timeout_ms) or connect_timeout_ms <= 0:
        raise ValueError("hardware_connect_timeout_ms 必须为有限正数")
    connect_budget_s = connect_timeout_ms / 1000.0
    if startup_serial_budget_s >= connect_budget_s:
        raise ValueError(
            "Alicat frame deadline 与启动清零时序无法容纳在 Connect budget 内"
        )


def _collect_selector_line(valve_mapping: dict) -> str | None:
    selector = valve_mapping.get("selector") or {}
    target = (
        selector.get("target") if isinstance(selector, dict) else None
    ) or valve_mapping.get("master_valve")
    return None if not target else str(target)


def _collect_digital_safe_levels(
    config: dict,
    *,
    odor_valve_lines: Iterable[str],
    selector_line: str | None,
) -> dict[str, bool]:
    """Build the complete first-drive image from odor and selector polarity."""

    odor_targets = [normalize_digital_target(target) for target in odor_valve_lines]
    safe_levels: dict[str, bool] = {}
    profile = config.get("hardware_profile") or {}
    channels = profile.get("channels") or [] if isinstance(profile, dict) else []
    profile_polarities: dict[str, bool] = {}
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict) or not channel.get("target"):
                continue
            target = normalize_digital_target(str(channel["target"]))
            active_high = channel.get("active_high", True)
            if type(active_high) is not bool:
                raise ValueError("hardware_profile channel active_high 必须是 JSON boolean")
            profile_polarities[target] = not active_high
    # HardwareProfile overrides polarity for every target it owns. Remaining
    # legacy safety-union aliases retain the project's established active-high
    # contract, so their closed/safe level is explicitly LOW.
    safe_levels.update(
        {target: profile_polarities.get(target, False) for target in odor_targets}
    )
    if selector_line is not None:
        selector = (profile.get("selector") if isinstance(profile, dict) else None) or (
            config.get("valve_mapping") or {}
        ).get("selector") or {}
        if "safe_level" not in selector:
            raise ValueError("hardware_profile selector 缺少 safe_level")
        safe_level = selector["safe_level"]
        if type(safe_level) is not bool:
            raise ValueError("selector safe_level 必须是 JSON boolean")
        safe_levels[normalize_digital_target(selector_line)] = safe_level
    return safe_levels


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
