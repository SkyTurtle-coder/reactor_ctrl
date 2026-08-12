from __future__ import annotations

import argparse
import socket
import sys

from reactor_app.services.drivers import HuberUnistatTCP
from reactor_app.services.drivers.huber_ministat_cc import _integer_from_pp_response, _temperature_from_pp_response


_PB_COMMAND_ADDR = {
    "get_setpoint": "00",
    "get_internal_temp": "01",
    "get_return_temp": "02",
    "get_error": "05",
    "get_warning": "06",
    "get_process_temp": "07",
    "get_external_temp": "07",
    "get_status": "0A",
}
_PP_COMMAND_TEXT = {
    "get_setpoint": "SP?",
    "get_internal_temp": "TI?",
    "get_external_temp": "TE?",
    "get_process_temp": "TE?",
    "get_error": "FSW?",
    "get_status": "TEMP?",
    "get_control_status": "CA?",
}
_TEMPERATURE_COMMANDS = {"get_setpoint", "get_internal_temp", "get_external_temp", "get_process_temp", "get_return_temp"}


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


def _query_pb(host: str, port: int, *, command: str, timeout: float) -> tuple[str, float | int | dict[str, bool]]:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        request = HuberUnistatTCP.build_request(_command_addr(command), "****")
        sock.sendall(request.encode("ascii"))
        response = _read_line(sock).decode("ascii", errors="replace")

    value_hex = HuberUnistatTCP.validate_response(response, _command_addr(command))
    if command in _TEMPERATURE_COMMANDS:
        return value_hex, HuberUnistatTCP.decode_temp(value_hex)
    if command == "get_status":
        raw = int(value_hex, 16)
        return value_hex, HuberUnistatTCP.status_bits(raw)
    return value_hex, HuberUnistatTCP.decode_i16(value_hex)


def _query_pp(
    host: str,
    port: int,
    *,
    command: str,
    timeout: float,
) -> tuple[str, float | int | dict[str, bool | str] | str]:
    if command not in _PP_COMMAND_TEXT:
        raise ValueError(f"PP smoke test does not support command '{command}'.")
    command_text = _PP_COMMAND_TEXT[command]
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((command_text + "\r\n").encode("ascii"))
        response = _read_line(sock, max_bytes=128).decode("ascii", errors="replace")

    if command in _TEMPERATURE_COMMANDS:
        return response.strip(), _temperature_from_pp_response(response)
    if command == "get_status":
        return response.strip(), {
            "active_control_sensor": response.strip().lower(),
        }
    if command == "get_control_status":
        raw = _integer_from_pp_response(response)
        active = raw == 1
        return response.strip(), {
            "temperature_control_active": active,
            "circulation_active": active,
        }
    return response.strip(), response.strip()


def _query(
    host: str,
    port: int,
    *,
    command: str,
    timeout: float,
    protocol: str,
) -> tuple[str, float | int | dict[str, bool | str] | str]:
    if protocol == "pp":
        return _query_pp(host, port, command=command, timeout=timeout)
    return _query_pb(host, port, command=command, timeout=timeout)


def _command_addr(command: str) -> str:
    return _PB_COMMAND_ADDR[command]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read one Huber PB or PP value through a Moxa TCP serial port.")
    parser.add_argument("--host", required=True, help="Moxa NPort IP address or host name.")
    parser.add_argument("--port", type=int, default=4001, help="Moxa TCP data port, usually 4000 + serial port number.")
    parser.add_argument("--protocol", choices=("pb", "pp"), default="pb", help="Huber wire protocol to test.")
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
            "get_external_temp",
            "get_process_temp",
            "get_status",
            "get_control_status",
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        raw_value, value = _query(
            args.host,
            args.port,
            command=args.command,
            timeout=args.timeout_s,
            protocol=args.protocol,
        )
    except Exception as exc:
        print(f"Huber {args.protocol.upper()} smoke test failed on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"{args.protocol.upper()} {args.command} {args.host}:{args.port} -> raw={raw_value} value={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
