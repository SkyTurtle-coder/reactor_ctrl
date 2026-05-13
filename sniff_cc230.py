"""Raw RS232 sniffer for Huber CC230 via MOXA TCP bridge.

Sends probe commands and logs every byte (hex + ASCII) in both directions,
so the exact protocol can be reconstructed from the wire traffic.

Usage examples:
  # Auto-probe all known line endings and commands:
  python sniff_cc230.py --host 192.168.1.50 --probe

  # Send one specific command (hex-escaped if needed):
  python sniff_cc230.py --host 192.168.1.50 --send "IN_PV_00" --ending cr

  # Interactive REPL – type ASCII commands, see raw hex back:
  python sniff_cc230.py --host 192.168.1.50 --interactive

  # Just listen for 5 s (device may broadcast on its own):
  python sniff_cc230.py --host 192.168.1.50 --listen 5
"""
from __future__ import annotations

import argparse
import select
import socket
import sys
import time
from typing import Sequence

DEFAULT_PORT = 4001
DEFAULT_TIMEOUT = 3.0
DEFAULT_RECV = 512

LINE_ENDINGS: dict[str, bytes] = {
    "cr":   b"\r",
    "lf":   b"\n",
    "crlf": b"\r\n",
    "none": b"",
}


def _hex_dump(data: bytes, prefix: str = "") -> str:
    """Return a hex dump with printable ASCII annotation."""
    if not data:
        return f"{prefix}(empty)"
    hex_part = " ".join(f"{b:02X}" for b in data)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{prefix}[{len(data):3d} B]  {hex_part:<48}  |{ascii_part}|"


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def _log(direction: str, data: bytes) -> None:
    tag = {"TX": ">>", "RX": "<<", "INFO": "--"}.get(direction, direction)
    print(f"{_ts()} {tag} {_hex_dump(data)}")
    sys.stdout.flush()


def _connect(host: str, port: int, timeout: float) -> socket.socket:
    print(f"-- Connecting to {host}:{port} (timeout={timeout}s) …")
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    print("-- Connected.")
    return sock


def _recv_all(sock: socket.socket, timeout: float) -> bytes:
    """Read until the socket is silent for `timeout` seconds."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            ready, _, _ = select.select([sock], [], [], max(0.05, remaining))
        except Exception:
            break
        if not ready:
            break
        try:
            chunk = sock.recv(DEFAULT_RECV)
        except socket.timeout:
            break
        if not chunk:
            break
        data.extend(chunk)
        # Reset deadline after each received byte burst so partial packets
        # have time to arrive completely.
        deadline = time.monotonic() + min(timeout, 0.5)
    return bytes(data)


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------

_PROBE_COMMANDS: list[tuple[str, bytes]] = [
    # NAMUR / ASCII commands (CC230 manual section 5)
    ("IN_PV_00",              b"IN_PV_00"),
    ("IN_SP_00",              b"IN_SP_00"),
    ("STATUS",                b"STATUS"),
    ("VERSION",               b"VERSION"),
    ("IN_PV_00 (uppercase)",  b"IN_PV_00"),
    # Some devices want a leading space or different casing
    ("in_pv_00 (lower)",      b"in_pv_00"),
    # PB / Pilot-ONE binary-ish protocol
    ("PB get_temp",           b"{M01****"),
    ("PB get_sp",             b"{M00****"),
    ("PB status",             b"{M0A****"),
    # LAI-style (older firmware)
    ("$T (LAI temp)",         b"$T"),
    ("$S (LAI status)",       b"$S"),
    # Bare CR / LF probe (wake-up)
    ("bare CR",               b""),
]


def cmd_probe(host: str, port: int, timeout: float) -> None:
    """Try every known command with every common line ending and log raw bytes."""
    print(f"\n{'='*70}")
    print("PROBE MODE – sending all known commands x all line endings")
    print(f"{'='*70}\n")

    for ending_name, ending_bytes in LINE_ENDINGS.items():
        for label, payload in _PROBE_COMMANDS:
            wire = payload + ending_bytes
            print(f"\n--- [{ending_name}] {label} ---")
            try:
                sock = _connect(host, port, timeout)
                try:
                    _log("TX", wire)
                    sock.sendall(wire)
                    rx = _recv_all(sock, timeout)
                    _log("RX", rx)
                    if rx:
                        # Also show the decoded string for convenience
                        try:
                            decoded = rx.decode("ascii", errors="replace").strip()
                            print(f"   decoded: {decoded!r}")
                        except Exception:
                            pass
                finally:
                    sock.close()
            except Exception as exc:
                print(f"   ERROR: {exc}")
            # Brief pause so the device has time to reset its serial buffer.
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Single-send mode
# ---------------------------------------------------------------------------

def cmd_send(host: str, port: int, timeout: float, text: str, ending: str) -> None:
    ending_bytes = LINE_ENDINGS.get(ending, b"\r")
    wire = text.encode("ascii") + ending_bytes
    print(f"\n--- Sending with ending={ending!r} ---")
    sock = _connect(host, port, timeout)
    try:
        _log("TX", wire)
        sock.sendall(wire)
        rx = _recv_all(sock, timeout)
        _log("RX", rx)
        if rx:
            try:
                print(f"decoded: {rx.decode('ascii', errors='replace').strip()!r}")
            except Exception:
                pass
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Hex-send mode (specify exact bytes as hex string, e.g. "494E5F50563030300D")
# ---------------------------------------------------------------------------

def cmd_send_hex(host: str, port: int, timeout: float, hexstr: str) -> None:
    wire = bytes.fromhex(hexstr.replace(" ", "").replace(":", ""))
    print(f"\n--- Sending raw hex ---")
    sock = _connect(host, port, timeout)
    try:
        _log("TX", wire)
        sock.sendall(wire)
        rx = _recv_all(sock, timeout)
        _log("RX", rx)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Listen mode
# ---------------------------------------------------------------------------

def cmd_listen(host: str, port: int, duration: float) -> None:
    print(f"\n--- Listening for {duration}s (device may broadcast spontaneously) ---")
    sock = _connect(host, port, duration + 1)
    try:
        rx = _recv_all(sock, duration)
        _log("RX", rx)
        if not rx:
            print("(nothing received)")
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def cmd_interactive(host: str, port: int, timeout: float, ending: str) -> None:
    ending_bytes = LINE_ENDINGS.get(ending, b"\r")
    print(f"\n--- Interactive mode (ending={ending!r}, timeout={timeout}s) ---")
    print("Type ASCII commands. Prefix with 0x to send raw hex bytes.")
    print("Empty line = send bare line-ending. Ctrl-C to exit.\n")
    sock = _connect(host, port, timeout)
    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break
            if line.lower().startswith("0x"):
                wire = bytes.fromhex(line[2:].replace(" ", ""))
            else:
                wire = line.encode("ascii") + ending_bytes
            _log("TX", wire)
            sock.sendall(wire)
            rx = _recv_all(sock, timeout)
            _log("RX", rx)
            if rx:
                try:
                    print(f"  decoded: {rx.decode('ascii', errors='replace').strip()!r}")
                except Exception:
                    pass
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Raw RS232 sniffer for Huber CC230 via MOXA TCP bridge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="MOXA IP address or hostname.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"MOXA TCP port (default {DEFAULT_PORT}).")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"Socket read timeout in seconds (default {DEFAULT_TIMEOUT}).")
    p.add_argument("--ending", choices=list(LINE_ENDINGS), default="cr", help="Line ending to append (default: cr = \\r).")

    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true", help="Auto-probe all commands × all line endings.")
    group.add_argument("--send", metavar="CMD", help="Send one ASCII command.")
    group.add_argument("--send-hex", metavar="HEX", help="Send raw hex bytes, e.g. 494E5F50563030300D")
    group.add_argument("--listen", type=float, metavar="SECS", help="Listen for N seconds without sending.")
    group.add_argument("--interactive", action="store_true", help="Interactive REPL.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.probe:
            cmd_probe(args.host, args.port, args.timeout)
        elif args.send is not None:
            cmd_send(args.host, args.port, args.timeout, args.send, args.ending)
        elif args.send_hex is not None:
            cmd_send_hex(args.host, args.port, args.timeout, args.send_hex)
        elif args.listen is not None:
            cmd_listen(args.host, args.port, args.listen)
        elif args.interactive:
            cmd_interactive(args.host, args.port, args.timeout, args.ending)
    except KeyboardInterrupt:
        print("\nAborted.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
