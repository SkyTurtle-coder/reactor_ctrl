from __future__ import annotations

import argparse
import socket
import sys

from reactor_app.services.drivers import HuberUnistatTCP


def _read_line(sock: socket.socket, *, max_bytes: int = 64) -> bytes:
    data = bytearray()
    while len(data) < max_bytes:
        chunk = sock.recv(1)
        if not chunk:
            raise TimeoutError("Connection closed before a Huber PB response was received.")
        data.extend(chunk)
        if data.endswith(b"\n"):
            return bytes(data)
    raise TimeoutError("Huber PB response exceeded the maximum expected length.")


def _query(host: str, port: int, *, command: str, timeout: float) -> tuple[str, float | int | dict[str, bool]]:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        request = HuberUnistatTCP.build_request(_command_addr(command), "****")
        sock.sendall(request.encode("ascii"))
        response = _read_line(sock).decode("ascii", errors="replace")

    value_hex = HuberUnistatTCP.validate_response(response, _command_addr(command))
    if command in {"get_internal_temp", "get_process_temp", "get_return_temp", "get_setpoint"}:
        return value_hex, HuberUnistatTCP.decode_temp(value_hex)
    if command == "get_status":
        raw = int(value_hex, 16)
        return value_hex, HuberUnistatTCP.status_bits(raw)
    return value_hex, HuberUnistatTCP.decode_i16(value_hex)


def _command_addr(command: str) -> str:
    mapping = {
        "get_setpoint": "00",
        "get_internal_temp": "01",
        "get_return_temp": "02",
        "get_error": "05",
        "get_warning": "06",
        "get_process_temp": "07",
        "get_status": "0A",
    }
    return mapping[command]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read one Huber PB variable through a Moxa TCP serial port.")
    parser.add_argument("--host", required=True, help="Moxa NPort IP address or host name.")
    parser.add_argument("--port", type=int, default=4001, help="Moxa TCP data port, usually 4000 + serial port number.")
    parser.add_argument("--timeout-s", type=float, default=1.5)
    parser.add_argument(
        "--command",
        default="get_internal_temp",
        choices=(
            "get_setpoint",
            "get_internal_temp",
            "get_return_temp",
            "get_error",
            "get_warning",
            "get_process_temp",
            "get_status",
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        value_hex, value = _query(args.host, args.port, command=args.command, timeout=args.timeout_s)
    except Exception as exc:
        print(f"Huber smoke test failed on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"{args.command} {args.host}:{args.port} -> value_hex={value_hex} value={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
