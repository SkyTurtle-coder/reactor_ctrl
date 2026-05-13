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
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test a Huber CC230 through a MOXA TCP-to-RS232 port.")
    parser.add_argument("--host", default=os.getenv("HUBER_CC230_HOST"), help="MOXA IP address or hostname.")
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("HUBER_CC230_DEFAULT_PORT", DEFAULT_MOXA_PORT),
        help="MOXA TCP data port, usually 4001.",
    )
    parser.add_argument("--timeout", type=float, default=_env_float("HUBER_CC230_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    parser.add_argument(
        "--line-ending",
        default=os.getenv("HUBER_CC230_LINE_ENDING", "cr"),
        help="Command terminator: cr, crlf, lf, or a raw string.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=_env_int("HUBER_CC230_MAX_RETRIES", DEFAULT_MAX_RETRIES),
    )
    parser.add_argument(
        "--protocol",
        default=os.getenv("HUBER_CC230_PROTOCOL", "auto"),
        choices=("auto", "namur", "lai", "pb"),
        help="Protocol variant. Use auto unless the device is known.",
    )
    parser.add_argument("--min-setpoint", type=float, default=_env_float("HUBER_CC230_MIN_SETPOINT_C", -50.0))
    parser.add_argument("--max-setpoint", type=float, default=_env_float("HUBER_CC230_MAX_SETPOINT_C", 200.0))
    parser.add_argument("--log-file", default=os.getenv("HUBER_CC230_LOG_FILE"))
    parser.add_argument("--console-log", action="store_true", help="Log TX/RX traffic to stderr.")
    parser.add_argument("--dry-run", action="store_true", help="Print/log commands but do not send anything.")
    parser.add_argument("--mock", action="store_true", help="Use an in-process mock instead of the MOXA connection.")

    parser.add_argument("--command", help="Send one raw command and print the raw response.")
    parser.add_argument("--detect", action="store_true", help="Detect the Huber protocol with harmless read commands.")
    parser.add_argument("--get-temp", action="store_true", help="Read internal temperature.")
    parser.add_argument("--get-setpoint", action="store_true", help="Read setpoint.")
    parser.add_argument("--set-setpoint", type=float, help="Set setpoint in degC after range validation.")
    parser.add_argument("--start", action="store_true", help="Start temperature control.")
    parser.add_argument("--stop", action="store_true", help="Stop temperature control.")
    parser.add_argument("--status", action="store_true", help="Read status.")
    return parser


def _has_action(args: argparse.Namespace) -> bool:
    return any(
        (
            args.command,
            args.detect,
            args.get_temp,
            args.get_setpoint,
            args.set_setpoint is not None,
            args.start,
            args.stop,
            args.status,
        )
    )


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
        protocol=args.protocol,
        dry_run=args.dry_run,
        mock=args.mock,
        log_file=args.log_file,
        console_logging=args.console_log,
    )

    try:
        with client:
            if args.detect:
                print(client.detect_protocol())
            if args.command:
                print(client.query(args.command))
            if args.get_temp:
                print(f"internal_temperature_C={client.get_internal_temperature():.2f}")
            if args.get_setpoint:
                print(f"setpoint_C={client.get_setpoint():.2f}")
            if args.set_setpoint is not None:
                print(f"set_setpoint={client.set_setpoint(args.set_setpoint)}")
            if args.start:
                print(f"start={client.start_temperature_control()}")
            if args.stop:
                print(f"stop={client.stop_temperature_control()}")
            if args.status:
                print(client.get_status())
    except Exception as exc:
        print(f"Huber CC230 test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
