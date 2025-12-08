from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Iterable

from app.models import SelfCheckResult

LOG = logging.getLogger(__name__)


class HardwareCheckService:
    """Run startup self-checks for NI DAQ and RS232 serial connectivity."""

    def __init__(
        self,
        *,
        expected_ni_devices: Iterable[str] | None = None,
        serial_port: str | None = None,
        baud_rate: int | None = None,
        time_func: Callable[[], float] = time.time,
        nidaqmx_loader: Callable[[], object] | None = None,
        serial_provider: Callable[[], tuple[object, object]] | None = None,
    ) -> None:
        self.expected_ni_devices = [d for d in (expected_ni_devices or [])]
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self._now = time_func
        self._nidaqmx_loader = nidaqmx_loader
        self._serial_provider = serial_provider

    @classmethod
    def from_config(cls, config: dict) -> HardwareCheckService:
        return cls(
            expected_ni_devices=config.get("ni_devices", []),
            serial_port=config.get("serial_port"),
            baud_rate=config.get("baud_rate"),
        )

    def run_checks(self) -> tuple[list[SelfCheckResult], bool]:
        results: list[SelfCheckResult] = []
        results.extend(self._check_ni_devices())
        results.append(self._check_serial_port())
        hardware_ready = all(item.status == "PASS" for item in results)
        for item in results:
            LOG.info(
                "自检结果 | 设备=%s | 类型=%s | 状态=%s | 原因=%s | 建议=%s | 时间戳=%.0f",
                item.name,
                item.type,
                item.status,
                item.reason,
                item.suggestion,
                item.checked_at,
            )
        return results, hardware_ready

    # Internal helpers
    def _load_nidaqmx_system(self):
        if self._nidaqmx_loader:
            return self._nidaqmx_loader()
        return importlib.import_module("nidaqmx.system")

    def _load_serial_modules(self) -> tuple[object, object]:
        if self._serial_provider:
            return self._serial_provider()
        serial_module = importlib.import_module("serial")
        list_ports_module = importlib.import_module("serial.tools.list_ports")
        return serial_module, list_ports_module

    def _check_ni_devices(self) -> list[SelfCheckResult]:
        timestamp = self._now()
        results: list[SelfCheckResult] = []
        expected = self.expected_ni_devices or ["USB-6001", "USB-6501"]
        try:
            system = self._load_nidaqmx_system()
            devices = list(getattr(system.System.local(), "devices", []))
        except ModuleNotFoundError:
            for name in expected:
                results.append(
                    SelfCheckResult(
                        name=name,
                        type="ni",
                        status="FAIL",
                        reason="NI-DAQmx 未安装或模块不可用",
                        suggestion="安装 NI-DAQmx 驱动后重新运行自检",
                        checked_at=timestamp,
                    )
                )
            return results
        except Exception as exc:  # pragma: no cover - defensive
            results.append(
                SelfCheckResult(
                    name="NI-DAQmx",
                    type="ni",
                    status="FAIL",
                    reason=f"NI 自检失败: {exc}",
                    suggestion="检查 NI 驱动/权限后重试",
                    checked_at=timestamp,
                )
            )
            return results

        if not devices:
            for name in expected:
                results.append(
                    SelfCheckResult(
                        name=name,
                        type="ni",
                        status="FAIL",
                        reason=f"未检测到 {name}",
                        suggestion="检查 USB 连接并确认设备通电",
                        checked_at=timestamp,
                    )
                )
            return results

        def _matches(device, target: str) -> bool:
            target_lower = target.lower()
            product = str(getattr(device, "product_type", "")).lower()
            device_name = str(getattr(device, "name", "")).lower()
            return target_lower in product or target_lower in device_name

        for name in expected:
            match = next((d for d in devices if _matches(d, name)), None)
            if not match:
                results.append(
                    SelfCheckResult(
                        name=name,
                        type="ni",
                        status="FAIL",
                        reason=f"未检测到 {name}",
                        suggestion="确认设备连接/驱动正常，或检查设备 ID 设置",
                        checked_at=timestamp,
                    )
                )
                continue
            product = getattr(match, "product_type", getattr(match, "name", name))
            results.append(
                SelfCheckResult(
                    name=name,
                    type="ni",
                    status="PASS",
                    reason=f"{product} 连接正常",
                    suggestion="无需操作",
                    checked_at=timestamp,
                )
            )
        return results

    def _check_serial_port(self) -> SelfCheckResult:
        timestamp = self._now()
        if not self.serial_port or not self.baud_rate:
            return SelfCheckResult(
                name="RS232",
                type="serial",
                status="FAIL",
                reason="未配置串口/波特率",
                suggestion="请在配置文件设置 serial_port 与 baud_rate 后重试",
                checked_at=timestamp,
            )

        try:
            serial_module, list_ports_module = self._load_serial_modules()
        except ModuleNotFoundError:
            return SelfCheckResult(
                name="RS232",
                type="serial",
                status="FAIL",
                reason="未安装 pyserial",
                suggestion="安装 pyserial 后重试",
                checked_at=timestamp,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return SelfCheckResult(
                name="RS232",
                type="serial",
                status="FAIL",
                reason=f"串口自检加载失败: {exc}",
                suggestion="检查 Python 环境后重试",
                checked_at=timestamp,
            )

        available_ports = [p.device for p in list_ports_module.comports()]
        if self.serial_port not in available_ports:
            return SelfCheckResult(
                name=self.serial_port,
                type="serial",
                status="FAIL",
                reason=f"未找到配置的串口 {self.serial_port}",
                suggestion="确认物理连接、尝试插拔并更新配置中的 serial_port",
                checked_at=timestamp,
            )

        try:
            connection = serial_module.Serial(self.serial_port, self.baud_rate, timeout=1)
            if hasattr(connection, "close"):
                connection.close()
            return SelfCheckResult(
                name=self.serial_port,
                type="serial",
                status="PASS",
                reason="串口连接正常",
                suggestion="无需操作",
                checked_at=timestamp,
            )
        except serial_module.SerialException as exc:  # type: ignore[attr-defined]
            message = str(exc).lower()
            if "denied" in message or "permission" in message or "access" in message:
                hint = "关闭可能占用串口的程序后重试"
            elif "baud" in message:
                hint = "检查波特率配置是否正确"
            else:
                hint = "检查串口连线与配置"
            return SelfCheckResult(
                name=self.serial_port,
                type="serial",
                status="FAIL",
                reason=f"串口打开失败: {exc}",
                suggestion=hint,
                checked_at=timestamp,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return SelfCheckResult(
                name=self.serial_port,
                type="serial",
                status="FAIL",
                reason=f"串口自检异常: {exc}",
                suggestion="检查串口连接与权限",
                checked_at=timestamp,
            )
