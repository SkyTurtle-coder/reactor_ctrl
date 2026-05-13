"""CLI test tool for the Huber CC230 via MOXA TCP-to-RS232 bridge.

Examples
--------
    python huber_test.py --host 192.168.1.50 --detect
    python huber_test.py --host 192.168.1.50 --get-temp
    python huber_test.py --host 192.168.1.50 --get-external-temp
    python huber_test.py --host 192.168.1.50 --get-setpoint
    python huber_test.py --host 192.168.1.50 --set-setpoint 25.00
    python huber_test.py --host 192.168.1.50 --status
    python huber_test.py --host 192.168.1.50 --start
    python huber_test.py --host 192.168.1.50 --stop
    python huber_test.py --host 192.168.1.50 --command "IN_PV_00"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from reactor_app.services.drivers.huber_cc230 import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MOXA_PORT,
    DEFAULT_TIMEOUT_S,
    HuberCC230Client,
    configure_cc230_logging,
)


load_dotenv(Path(__file__).resolve().parent / ".env")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Test tool for Huber CC230 via MOXA TCP-to-RS232.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Connection
    p.add_argument("--host", default=os.getenv("HUBER_CC230_HOST"),
                   help="MOXA IP address or hostname.")
    p.add_argument("--port", type=int,
                   default=_env_int("HUBER_CC230_PORT", DEFAULT_MOXA_PORT),
                   help=f"MOXA TCP data port (default {DEFAULT_MOXA_PORT}).")
    p.add_argument("--timeout", type=float,
                   default=_env_float("HUBER_CC230_TIMEOUT_S", DEFAULT_TIMEOUT_S),
                   help="Read timeout in seconds (default 2.0).")
    p.add_argument("--line-ending", default=os.getenv("HUBER_CC230_LINE_ENDING", "crlf"),
                   choices=("cr", "crlf", "lf"),
                   help="Command terminator sent to device (default: cr = \\r).")
    p.add_argument("--max-retries", type=int,
                   default=_env_int("HUBER_CC230_MAX_RETRIES", DEFAULT_MAX_RETRIES),
                   help="Retry count on timeout (default 2).")

    # Setpoint limits
    p.add_argument("--min-setpoint", type=float,
                   default=_env_float("HUBER_CC230_MIN_SETPOINT_C", -50.0))
    p.add_argument("--max-setpoint", type=float,
                   default=_env_float("HUBER_CC230_MAX_SETPOINT_C", 200.0))

    # Logging
    p.add_argument("--log-file", default=os.getenv("HUBER_CC230_LOG_FILE"),
                   help="Append TX/RX log to this file.")
    p.add_argument("--console-log", action="store_true",
                   help="Print TX/RX wire log to stderr.")

    # Modes
    p.add_argument("--dry-run", action="store_true",
                   help="Log commands but do not send anything.")
    p.add_argument("--mock", action="store_true",
                   help="Use an in-process mock instead of a real connection.")

    # Actions
    p.add_argument("--detect", action="store_true",
                   help="Detect working protocol / line-ending.")
    p.add_argument("--command", metavar="CMD",
                   help="Send one raw ASCII command and print the reply.")
    p.add_argument("--get-temp", action="store_true",
                   help="Read internal (bath) temperature via IN_PV_00.")
    p.add_argument("--get-external-temp", action="store_true",
                   help="Read external (reactor) temperature via IN_PV_02.")
    p.add_argument("--get-setpoint", action="store_true",
                   help="Read current setpoint via IN_SP_00.")
    p.add_argument("--set-setpoint", type=float, metavar="DEGC",
                   help="Set temperature setpoint (°C) via OUT_SP_00.")
    p.add_argument("--status", action="store_true",
                   help="Read STATUS and display parsed result.")
    p.add_argument("--start", action="store_true",
                   help="Send START (temperature control on).")
    p.add_argument("--stop", action="store_true",
                   help="Send STOP (temperature control off).")

    return p


def _has_action(args: argparse.Namespace) -> bool:
    return any([
        args.detect,
        args.command,
        args.get_temp,
        args.get_external_temp,
        args.get_setpoint,
        args.set_setpoint is not None,
        args.status,
        args.start,
        args.stop,
    ])


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not _has_action(args):
        parser.error("Select at least one action, e.g. --detect or --get-temp.")
    if not args.host and not args.mock and not args.dry_run:
        parser.error("--host is required unless --mock or --dry-run is used.")

    configure_cc230_logging(log_file=args.log_file, console=args.console_log)

    client = HuberCC230Client(
        host=args.host or "dry-run",
        port=args.port,
        timeout=args.timeout,
        line_ending=args.line_ending,
        max_retries=args.max_retries,
        min_setpoint_c=args.min_setpoint,
        max_setpoint_c=args.max_setpoint,
        dry_run=args.dry_run,
        mock=args.mock,
        log_file=args.log_file,
        console_logging=args.console_log,
    )

    try:
        with client:
            if args.detect:
                result = client.detect_protocol()
                print(f"protocol={result['protocol']}")
                print(f"line_ending={result['line_ending']}")
                print(f"command={result['command']!r}")
                print(f"response={result['response']!r}")

            if args.command:
                reply = client.query(args.command)
                print(f"reply={reply!r}")

            if args.get_temp:
                temp = client.get_internal_temperature()
                print(f"internal_temperature_C={temp:.2f}")

            if args.get_external_temp:
                temp = client.get_external_temperature()
                if temp is None:
                    print("external_temperature=None (no sensor or error)")
                else:
                    print(f"external_temperature_C={temp:.2f}")

            if args.get_setpoint:
                sp = client.get_setpoint()
                print(f"setpoint_C={sp:.2f}")

            if args.set_setpoint is not None:
                ok = client.set_setpoint(args.set_setpoint)
                print(f"set_setpoint={'OK' if ok else 'FAILED'}")

            if args.status:
                s = client.get_status()
                print(f"status_code={s['code']}")
                print(f"status_text={s['text']}")
                print(f"temperature_control_active={s['temperature_control_active']}")
                print(f"alarm={s['alarm']}")

            if args.start:
                ok = client.start_temperature_control()
                print(f"start={'OK' if ok else 'FAILED'}")

            if args.stop:
                ok = client.stop_temperature_control()
                print(f"stop={'OK' if ok else 'FAILED'}")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
