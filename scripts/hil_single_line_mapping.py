from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nidaqmx.system import System

from app.main import load_effective_config
from app.services.real_hal import RealHAL

LIVE_CONFIRMATION = "I_AUTHORIZE_SINGLE_LINE_MAPPING_HIL"
EXPECTED_DEVICES = {
    "Dev1": ("USB-6001", 34887710),
    "Dev2": ("USB-6001", 34887797),
}


def require_success(ok: bool, action: str) -> None:
    if not ok:
        raise RuntimeError(f"{action} failed")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicitly authorized, fail-closed DO mapping pulse."
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--flow-sccm", required=True, type=float)
    parser.add_argument("--duration-s", required=True, type=float)
    parser.add_argument("--confirmed-full-scale-sccm", required=True, type=float)
    parser.add_argument("--open-before-flow", action="store_true")
    parser.add_argument("--dry-pulse", action="store_true")
    parser.add_argument("--keep-target-low", action="store_true")
    parser.add_argument("--confirm", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config" / "default_config.json",
    )
    parser.add_argument(
        "--local-config",
        type=Path,
        default=REPO_ROOT / "config" / "local_config.json",
    )
    args = parser.parse_args()

    if args.confirm != LIVE_CONFIRMATION:
        parser.error(f"live hardware requires --confirm {LIVE_CONFIRMATION}")
    if args.target != "Dev2/P1.0":
        parser.error("this approved diagnostic is restricted to Dev2/P1.0")
    parser.error(
        "this legacy direct-HAL selector diagnostic is retired by Story 4.5; "
        "use a separately authorized owner/receipt HIL harness"
    )
    if sum((args.open_before_flow, args.dry_pulse, args.keep_target_low)) > 1:
        parser.error("diagnostic modes are mutually exclusive")
    if args.dry_pulse:
        if args.flow_sccm != 0:
            parser.error("dry pulse requires --flow-sccm 0")
        if not 0 < args.duration_s <= 5:
            parser.error("dry pulse duration must be in the range 0 < duration <= 5 s")
    else:
        if not 0 < args.flow_sccm <= args.confirmed_full_scale_sccm:
            parser.error(
                "flow must be positive and within the confirmed device full scale"
            )
        if not 0 < args.duration_s <= 20:
            parser.error("duration must be in the approved range 0 < duration <= 20 s")

    inventory = {
        device.name: (device.product_type, int(device.serial_num))
        for device in System.local().devices
    }
    if inventory != EXPECTED_DEVICES:
        raise RuntimeError(
            f"NI inventory mismatch: expected={EXPECTED_DEVICES!r}, actual={inventory!r}"
        )
    print(f"PREFLIGHT inventory={inventory!r}", flush=True)

    config = load_effective_config(args.config, args.local_config)
    if str(config.get("hal_mode", "")).lower() != "real":
        raise RuntimeError("effective config is not in real hardware mode")
    hal = RealHAL.from_config(config)
    device, line = args.target.split("/", 1)
    opened = False

    try:
        require_success(hal.prepare_do_output(), "prepare DO output")
        for channel in ("A", "B", "C"):
            require_success(hal.set_flow(channel, 0.0), f"initial {channel}=0")
        require_success(hal.close_all(), "initial odor 1-20 close")
        print("INITIAL_CONVERGED odor_targets_closed=true A/B/C=0", flush=True)

        if args.keep_target_low:
            opened_at = time.monotonic()
            require_success(
                hal.set_flow("A", args.flow_sccm),
                f"set A={args.flow_sccm:g} sccm",
            )
            flow_started_at = time.monotonic()
        elif args.dry_pulse:
            opened_ack = hal.write_digital_ack(
                device=device,
                line=line,
                state=True,
                timeout_ms=100,
            )
            require_success(
                opened_ack.success,
                f"open {args.target}: {opened_ack.message}",
            )
            opened = True
            opened_at = time.monotonic()
            flow_started_at = opened_at
        elif args.open_before_flow:
            opened_ack = hal.write_digital_ack(
                device=device,
                line=line,
                state=True,
                timeout_ms=100,
            )
            require_success(
                opened_ack.success,
                f"open {args.target}: {opened_ack.message}",
            )
            opened = True
            opened_at = time.monotonic()
            require_success(
                hal.set_flow("A", args.flow_sccm),
                f"set A={args.flow_sccm:g} sccm",
            )
            flow_started_at = time.monotonic()
        else:
            require_success(
                hal.set_flow("A", args.flow_sccm),
                f"set A={args.flow_sccm:g} sccm",
            )
            opened_ack = hal.write_digital_ack(
                device=device,
                line=line,
                state=True,
                timeout_ms=100,
            )
            require_success(
                opened_ack.success,
                f"open {args.target}: {opened_ack.message}",
            )
            opened = True
            opened_at = time.monotonic()
            flow_started_at = opened_at
        deadline = flow_started_at + args.duration_s
        print(
            f"{'LOW' if args.keep_target_low else 'OPEN'} target={args.target} "
            f"flow_sccm={args.flow_sccm:g} "
            f"duration_s={args.duration_s:g}",
            flush=True,
        )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if not args.dry_pulse and deadline - time.monotonic() > 0.15:
                print(f"FLOW A_mass_sccm={hal.read_flow():.1f}", flush=True)

        require_success(hal.set_flow("A", 0.0), "normal final A=0")
        if args.keep_target_low:
            require_success(hal.close_all(), "post-flow odor 1-20 low")
        else:
            close_ack = hal.write_digital_ack(
                device=device,
                line=line,
                state=False,
                timeout_ms=100,
            )
            require_success(
                close_ack.success,
                f"close {args.target}: {close_ack.message}",
            )
            opened = False
        print(
            f"{'LOW_COMPLETE' if args.keep_target_low else 'CLOSE'} "
            f"target={args.target} "
            f"actual_state_window_s={time.monotonic() - opened_at:.3f} "
            f"actual_flow_s={time.monotonic() - flow_started_at:.3f}",
            flush=True,
        )
        return 0
    finally:
        errors: list[str] = []
        a_zero = hal.set_flow("A", 0.0)
        if not a_zero:
            errors.append("final A=0 failed")
        if opened and a_zero:
            ack = hal.write_digital_ack(
                device=device,
                line=line,
                state=False,
                timeout_ms=100,
            )
            if not ack.success:
                errors.append(f"target close failed: {ack.message}")
        elif opened:
            errors.append("selector left unchanged because final A=0 was not confirmed")
        if not hal.close_all():
            errors.append("final odor 1-20 close failed")
        for channel in ("B", "C"):
            if not hal.set_flow(channel, 0.0):
                errors.append(f"final {channel}=0 failed")
        if not hal.release_do_output():
            errors.append("release DO output failed")
        hal.release_serial_resources()
        print(
            "FINAL_CONVERGED odor_targets_closed=true A/B/C=0 selector_safe=true"
            if not errors
            else f"FINAL_UNCERTAIN errors={errors!r}",
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
