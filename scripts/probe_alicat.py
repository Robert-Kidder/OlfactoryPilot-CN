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
    parser = argparse.ArgumentParser(description="Probe Alicat units over RS232.")
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--baud", type=int, default=19200)
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--ids", default="a,b,c", help="Comma-separated Alicat unit IDs to poll")
    parser.add_argument("--scale", type=float, default=0.001, help="Scale UI sccm values to Alicat device units")
    parser.add_argument("--set", nargs=2, metavar=("UNIT", "SCCM"), help="Optionally set one unit setpoint in sccm")
    args = parser.parse_args()

    units = [item.strip() for item in args.ids.split(",") if item.strip()]
    with serial.Serial(args.port, args.baud, timeout=args.timeout) as port:
        print(f"Opened {args.port} @ {args.baud}")
        for unit in units:
            response = query(port, f"{unit}\r")
            print(f"poll {unit!r}: {response!r}")

        if args.set:
            unit, value = args.set
            device_value = float(value) * float(args.scale)
            command = f"{unit}s{device_value:.3f}\r"
            print(f"set {unit!r}: {command!r}")
            query(port, command)
            time.sleep(0.1)
            response = query(port, f"{unit}\r")
            print(f"after set poll {unit!r}: {response!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
