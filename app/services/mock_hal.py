from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from app.models import SelfCheckResult
from app.services.hal import (
    AnalogInputFrame,
    DigitalWriteAck,
    FlowChannelReadback,
    FlowReadbackSnapshot,
    HalBase,
)


class MockHAL(HalBase):
    """软仿真 HAL：生成合成波形并记录虚拟阀门状态。"""

    def __init__(
        self,
        *,
        base_flow_sccm: float = 1000.0,
        signal_amplitude: float = 0.5,
        signal_freq_hz: float = 0.2,
        monotonic_ns_clock: Callable[[], int] | None = None,
    ) -> None:
        self.base_flow_sccm = base_flow_sccm
        self.signal_amplitude = signal_amplitude
        self.signal_freq_hz = signal_freq_hz
        self._monotonic_ns_clock = monotonic_ns_clock or time.perf_counter_ns
        self._phase = 0.0
        self._digital_state: dict[str, bool] = {}
        self._flow = float(base_flow_sccm)
        self.flow_commands: list[tuple[str, float, bool]] = []
        self._setpoint_readbacks_sccm: dict[str, float] = {}
        self._mass_flows_sccm: dict[str, float] = {
            "A": float(base_flow_sccm),
            "B": 0.0,
            "C": 0.0,
        }
        self._setpoint_tx_monotonic_ns: dict[str, int] = {}
        self.fail_on: set[str] = set()
        self.master_events: list[tuple[str, bool]] = []
        self._ttl_level = 0.0
        self._ai_epoch = 1
        self._ai_sequence = 0
        self._do_owner_thread_id: int | None = None

    @property
    def ttl_input_ready(self) -> bool:
        return True

    def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
        ts = float(timestamp if timestamp is not None else time.time())
        frame = AnalogInputFrame(
            timestamp=ts,
            ai0=self.read_ai0(ts),
            ai6=float(self._ttl_level),
            monotonic_ns=int(self._monotonic_ns_clock()),
            ai_epoch=self._ai_epoch,
            sample_sequence=self._ai_sequence,
        )
        self._ai_sequence += 1
        return frame

    def read_ai_frames(self, timestamp: float | None = None) -> list[AnalogInputFrame]:
        return [self.read_ai_frame(timestamp)]

    def reset_ai_input(self) -> bool:
        self._ai_epoch += 1
        self._ai_sequence = 0
        return True

    def set_ttl_level(self, value: float) -> None:
        self._ttl_level = float(value)

    def read_ai0(self, timestamp: float | None = None) -> float:
        ts = timestamp if timestamp is not None else time.time()
        value = self.signal_amplitude * math.sin(
            2 * math.pi * self.signal_freq_hz * ts + self._phase
        )
        self._phase += 0.05
        return value

    def read_flow(self) -> float:
        return float(self._flow)

    def read_flow_snapshot(self) -> FlowReadbackSnapshot:
        wall_timestamp = time.time()
        readings = []
        for channel, unit_id in (("A", "a"), ("B", "b"), ("C", "c")):
            monotonic_ns = int(self._monotonic_ns_clock())
            readings.append(
                FlowChannelReadback(
                    channel=channel,
                    unit_id=unit_id,
                    setpoint_sccm=self._setpoint_readbacks_sccm.get(channel, 0.0),
                    mass_flow_sccm=self._mass_flows_sccm.get(channel, 0.0),
                    gas="Air",
                    wall_timestamp=wall_timestamp,
                    monotonic_ns=monotonic_ns,
                    raw_frame=f"{unit_id} mock",
                    fresh=True,
                )
            )
        return FlowReadbackSnapshot(
            readings=tuple(readings),
            wall_timestamp=wall_timestamp,
            monotonic_ns=readings[-1].monotonic_ns,
            fresh=True,
        )

    @property
    def serial_desynchronized(self) -> bool:
        return False

    def set_flow(self, channel: str | float, value: float | None = None, *, comp: bool = False) -> bool:
        # Backward compatibility: allow set_flow(value) signature.
        if value is None:
            value = channel
            channel = "A"
        channel = str(channel).upper()
        if not hasattr(self, "flow_commands"):
            self.flow_commands = []
        self.flow_commands.append((channel, float(value), bool(comp)))
        if hasattr(self, "fail_on") and channel in self.fail_on:
            return False
        self._flow = float(value)
        self._setpoint_readbacks_sccm[channel] = float(value)
        self._mass_flows_sccm[channel] = float(value)
        self._setpoint_tx_monotonic_ns[channel] = int(self._monotonic_ns_clock())
        return True

    def last_setpoint_readback_sccm(self, channel: str) -> float | None:
        return self._setpoint_readbacks_sccm.get(str(channel).upper())

    def last_setpoint_tx_monotonic_ns(self, channel: str) -> int | None:
        return self._setpoint_tx_monotonic_ns.get(str(channel).upper())

    def set_mass_flow_readback(self, channel: str, value_sccm: float) -> None:
        self._mass_flows_sccm[str(channel).upper()] = float(value_sccm)

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        key = f"{device}/{line}" if device else line
        self._digital_state[key] = bool(state)
        if line.lower() == "p1.0" or "master" in key.lower():
            self.master_events.append((key, bool(state)))
        return True

    def write_digital_ack(
        self,
        *,
        device: str | None,
        line: str,
        state: bool,
        timeout_ms: int,
    ) -> DigitalWriteAck:
        if self._do_owner_thread_id != threading.get_ident():
            return DigitalWriteAck(
                success=False,
                started_ns=None,
                actual_ns=None,
                wall_timestamp=time.time(),
                message="DO task 所有权不属于当前线程，已拒绝跨线程写入。",
            )
        started_ns = int(self._monotonic_ns_clock())
        success = self.write_digital(device=device, line=line, state=state)
        actual_ns = int(self._monotonic_ns_clock())
        return DigitalWriteAck(
            success=success,
            started_ns=started_ns,
            actual_ns=actual_ns if success else None,
            wall_timestamp=time.time(),
            message="ok" if success else "Mock 数字输出失败",
        )

    def prepare_do_output(self) -> bool:
        owner = threading.get_ident()
        if self._do_owner_thread_id is None:
            self._do_owner_thread_id = owner
        return self._do_owner_thread_id == owner

    def release_do_output(self) -> bool:
        if self._do_owner_thread_id not in {None, threading.get_ident()}:
            raise RuntimeError("DO task 所有权不属于当前线程，不能跨线程释放。")
        self._do_owner_thread_id = None
        return True

    @property
    def do_resources_in_use(self) -> bool:
        return self._do_owner_thread_id is not None

    def release_serial_resources(self) -> None:
        return None

    def close_all(self) -> bool:
        for key in list(self._digital_state.keys()):
            self._digital_state[key] = False
        return True

    def stop_heaters(self) -> bool:
        return True

    def flush_logs(self) -> None:
        return None

    def self_check(self) -> tuple[list[SelfCheckResult], bool]:
        now_ts = time.time()
        result = SelfCheckResult(
            name="mock_hal",
            type="simulation",
            status="PASS",
            reason="模拟模式：跳过物理硬件检查",
            suggestion="无需操作",
            checked_at=now_ts,
        )
        return [result], True

    def get_line_state(self, key: str) -> bool | None:
        return self._digital_state.get(key)
