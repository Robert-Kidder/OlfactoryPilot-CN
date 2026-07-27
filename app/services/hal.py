from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models import SelfCheckResult


@dataclass(frozen=True)
class AnalogInputFrame:
    timestamp: float
    ai0: float
    ai6: float | None = None
    monotonic_ns: int = 0
    ai_epoch: int = 0
    sample_sequence: int = 0
    origin_uncertainty_ns: int = 0


@dataclass(frozen=True)
class BreathSample:
    timestamp: float
    monotonic_ns: int
    value: float
    ai_epoch: int
    sample_sequence: int


@dataclass(frozen=True)
class BreathSampleBatch:
    samples: tuple[BreathSample, ...]

    @classmethod
    def from_frames(cls, frames: tuple[AnalogInputFrame, ...]) -> BreathSampleBatch:
        return cls(
            samples=tuple(
                BreathSample(
                    timestamp=frame.timestamp,
                    monotonic_ns=frame.monotonic_ns,
                    value=frame.ai0,
                    ai_epoch=frame.ai_epoch,
                    sample_sequence=frame.sample_sequence,
                )
                for frame in frames
            )
        )

    def map_values(self, transform) -> BreathSampleBatch:
        return BreathSampleBatch(
            tuple(
                BreathSample(
                    timestamp=sample.timestamp,
                    monotonic_ns=sample.monotonic_ns,
                    value=float(transform(sample.value)),
                    ai_epoch=sample.ai_epoch,
                    sample_sequence=sample.sample_sequence,
                )
                for sample in self.samples
            )
        )


@dataclass(frozen=True)
class DigitalWriteAck:
    success: bool
    started_ns: int | None
    actual_ns: int | None
    wall_timestamp: float
    message: str = ""
    uncertain: bool = False
    measurement_point: str = "daqmx_write_ack"


@runtime_checkable
class HalInterface(Protocol):
    """HAL 接口抽象：统一模拟与真实硬件的读写能力。"""

    @property
    def ttl_input_ready(self) -> bool:
        """共享 AI 采样链路是否包含可用 AI6。"""

    def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:
        """从单一 AI task 读取 AI0/AI6 共享采样帧。"""

    def read_ai_frames(self, timestamp: float | None = None) -> list[AnalogInputFrame]:
        """读取共享 AI task 当前可用的全部采样帧。"""

    def reset_ai_input(self) -> bool:
        """释放失效的共享 AI task，并返回资源是否已确定释放。"""

    def read_ai0(self, timestamp: float | None = None) -> float:
        """读取模拟输入（呼吸波形）。"""

    def read_flow(self) -> float:
        """读取气流传感器值（sccm 单位）。"""

    def set_flow(self, channel: str, value: float, *, comp: bool = False) -> bool:
        """设置指定通道的目标流量。

        channel: "A"/"B"/"C" MFC。comp=True 表示补偿/合成流（Rest 下的 A_comp）。
        """

    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:
        """写入数字输出（阀/继电器）。"""

    def write_digital_ack(
        self,
        *,
        device: str | None,
        line: str,
        state: bool,
        timeout_ms: int,
    ) -> DigitalWriteAck:
        """在 HAL 边界返回 write 前/后的单调测量。"""

    def prepare_do_output(self) -> bool:
        """由 ActuationWorker 线程预建并取得 DO session 所有权。"""

    def release_do_output(self) -> bool:
        """由当前 DO owner 确定性释放 session，并确认 ownership handoff。"""

    def release_serial_resources(self) -> None:
        """由 serial owner 最后释放 MFC 串口。"""

    def close_all(self) -> bool:
        """关闭全部通道/阀门。"""

    def stop_heaters(self) -> bool:
        """关闭加热/泵等执行件。"""

    def flush_logs(self) -> None:
        """刷新日志/文件句柄。"""

    def self_check(self) -> tuple[list[SelfCheckResult], bool]:
        """执行自检，返回结果列表与是否 ready。"""


class HalBase(abc.ABC):
    """方便未来真实 HAL 继承的基类。"""

    @abc.abstractmethod
    def read_ai0(self, timestamp: float | None = None) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def read_flow(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def set_flow(self, channel: str, value: float, *, comp: bool = False) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def write_digital(self, *, device: str | None, line: str, state: bool) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def close_all(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def stop_heaters(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def flush_logs(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def self_check(self) -> tuple[list[SelfCheckResult], bool]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def ttl_input_ready(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @abc.abstractmethod
    def read_ai_frame(self, timestamp: float | None = None) -> AnalogInputFrame:  # pragma: no cover
        raise NotImplementedError

    @abc.abstractmethod
    def read_ai_frames(self, timestamp: float | None = None) -> list[AnalogInputFrame]:  # pragma: no cover
        raise NotImplementedError

    @abc.abstractmethod
    def reset_ai_input(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError
