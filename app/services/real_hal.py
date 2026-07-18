from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Iterable

from app.models import SelfCheckResult
from app.services.hal import AnalogInputFrame, HalBase
from app.services.ttl_trigger_service import TtlTriggerConfig

try:  # Local hardware drivers; keep import errors explicit for clear startup failures.
    import nidaqmx
    from nidaqmx.constants import LineGrouping
except Exception as exc:  # pragma: no cover - runtime-only dependency
    nidaqmx = None
    LineGrouping = None
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
        self._unit_ids = self._normalize_unit_ids(alicat_unit_ids)
        self._flow_unit_id = self._resolve_flow_unit_id(alicat_flow_unit)
        self._flow_field_index = FLOW_FIELD_INDEX.get(alicat_flow_field, 3)
        self._setpoint_verify_tolerance = max(0.0, float(setpoint_verify_tolerance))
        self._setpoint_verify_delay_s = max(0.0, float(setpoint_verify_delay_s))
        self._setpoint_verify_retries = max(1, int(setpoint_verify_retries))
        self._setpoint_scale = float(alicat_setpoint_scale)
        self._readback_scale = float(alicat_readback_scale)
        self._digital_lines = list(valve_lines or [])
        # Eagerly validate NI-DAQmx channel to surface hardware issues on startup.
        self._ensure_ai_task()

    @classmethod
    def from_config(cls, config: dict) -> RealHAL:
        valve_lines = _collect_valve_lines(config.get("valve_mapping") or {})
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
        )

    def read_ai0(self, timestamp: float | None = None) -> float:
        return self.read_ai_frame(timestamp).ai0

    @property
    def ttl_input_ready(self) -> bool:
        return self._ttl_input_ready

    def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
        task = self._ensure_ai_task()
        captured_at = float(timestamp if timestamp is not None else time.time())
        values = task.read()
        if self._ttl_input_ready:
            if not isinstance(values, list | tuple) or len(values) < 2:
                raise RuntimeError("共享 AI task 未返回 AI0/AI6 两通道样本")
            return AnalogInputFrame(timestamp=captured_at, ai0=float(values[0]), ai6=float(values[1]))
        value = values[0] if isinstance(values, list | tuple) else values
        return AnalogInputFrame(timestamp=captured_at, ai0=float(value), ai6=None)

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

    def close_all(self) -> bool:
        success = True
        for target in self._digital_lines:
            device, line = _split_target(target)
            if not self.write_digital(device=device, line=line, state=False):
                success = False
        return success

    def stop_heaters(self) -> bool:
        return True

    def flush_logs(self) -> None:
        self._close_resources()

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
        if self._ai_task is None:
            task = nidaqmx.Task()
            try:
                task.ai_channels.add_ai_voltage_chan(self.ai0_channel)
                task.ai_channels.add_ai_voltage_chan(self.ttl_input_channel)
                if hasattr(task, "timing"):
                    task.timing.cfg_samp_clk_timing(rate=self.ttl_poll_hz)
                self._ai_task = task
                self._ttl_input_ready = True
            except Exception as exc:
                LOG.warning("AI6 初始化失败，已安全降级为 AI0-only：%s", exc)
                try:
                    task.close()
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("关闭部分创建的共享 AI task 失败")
                fallback = nidaqmx.Task()
                fallback.ai_channels.add_ai_voltage_chan(self.ai0_channel)
                if hasattr(fallback, "timing"):
                    fallback.timing.cfg_samp_clk_timing(rate=self.ttl_poll_hz)
                self._ai_task = fallback
                self._ttl_input_ready = False
        return self._ai_task

    def _close_resources(self) -> None:
        try:
            if self._ai_task is not None:
                self._ai_task.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to close NI-DAQmx task")
        self._ai_task = None
        self._ttl_input_ready = False
        try:
            if self._serial is not None:
                self._serial.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to close serial port")
        self._serial = None

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


def _collect_valve_lines(valve_mapping: dict) -> list[str]:
    lines: set[str] = set()
    master = valve_mapping.get("master_valve")
    if master:
        lines.add(str(master))
    variants = valve_mapping.get("variants") or {}
    if isinstance(variants, dict):
        for mapping in variants.values():
            if not isinstance(mapping, dict):
                continue
            for line in mapping.values():
                if line:
                    lines.add(str(line))
    return sorted(lines)


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
