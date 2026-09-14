from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import serial

from app.services.alicat_serial import AlicatSerialSession, quantize_setpoint_for_wire


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 RS232 探测 Alicat 质量流量控制器。")
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--baud", type=int, default=19200)
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--ids", default="a,b,c", help="要轮询的 Alicat 设备 ID，用逗号分隔")
    parser.add_argument("--scale", type=float, default=0.001, help="把界面 sccm 数值缩放为 Alicat 设备单位")
    parser.add_argument("--set", nargs=2, metavar=("设备ID", "SCCM"), help="可选：设置一个设备的 sccm 目标值")
    args = parser.parse_args()

    units = [item.strip() for item in args.ids.split(",") if item.strip()]
    with serial.Serial(args.port, args.baud, timeout=args.timeout) as port:
        session = AlicatSerialSession(
            port,
            baud_rate=args.baud,
            frame_timeout_s=args.timeout,
        )
        print(f"已打开 {args.port} @ {args.baud}")
        for unit in units:
            frame = session.poll(unit)
            print(f"轮询 {unit!r}: {frame.raw!r}")

        if args.set:
            unit, value = args.set
            device_value = float(value) * float(args.scale)
            wire_value = quantize_setpoint_for_wire(device_value)
            command = f"{unit}s{wire_value:.3f}\r"
            print(f"设置 {unit!r}: {command!r}")
            session.set_setpoint(unit, wire_value, tolerance=0.00005)
            time.sleep(0.1)
            frame = session.poll(
                unit,
                expected_setpoint=wire_value,
                setpoint_tolerance=0.00005,
            )
            print(f"设置后轮询 {unit!r}: {frame.raw!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
