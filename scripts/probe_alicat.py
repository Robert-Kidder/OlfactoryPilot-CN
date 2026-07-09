from __future__ import annotations

import argparse
import time

import serial


def query(port: serial.Serial, command: str) -> str:
    payload = command.encode("ascii")
    port.reset_input_buffer()
    port.write(payload)
    port.flush()
    response = port.readline()
    return response.decode("ascii", errors="replace").strip()


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
        print(f"已打开 {args.port} @ {args.baud}")
        for unit in units:
            response = query(port, f"{unit}\r")
            print(f"轮询 {unit!r}: {response!r}")

        if args.set:
            unit, value = args.set
            device_value = float(value) * float(args.scale)
            command = f"{unit}s{device_value:.3f}\r"
            print(f"设置 {unit!r}: {command!r}")
            query(port, command)
            time.sleep(0.1)
            response = query(port, f"{unit}\r")
            print(f"设置后轮询 {unit!r}: {response!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
