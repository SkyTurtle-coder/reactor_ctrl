"""Huber CC230 compatible control – NAMUR/PP/LAI ASCII over RS-232 via MOXA TCP bridge.

Hardware connection
-------------------
    CC230  ←→  RS-232 (9600 8N1, no flow control)  ←→  MOXA NPort (TCP Server)  ←→  TCP socket

Protocol
--------
    NAMUR/PP/LAI ASCII.  Master (PC/MOXA) sends one command; slave (CC230) replies.
    Never send the next command before the previous reply has arrived.
    START and STOP have no reply; wait ≥ 500 ms after sending them.

RS-232 parameters
-----------------
    Baud 9600 · 8 data bits · No parity · 1 stop bit · No flow control
    Line terminator: CR ("\\r") primary, CRLF ("\\r\\n") as fallback.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from ..transports import TcpSocketTransport


LOGGER = logging.getLogger(__name__)

# ── Defaults ───────────────────────────────────────────────────────────────────
DEFAULT_MOXA_PORT = 4001
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_LINE_ENDING = "\r"          # Huber NAMUR spec: CR primary; fallback CRLF
DEFAULT_MAX_RETRIES = 2
DEFAULT_MIN_SETPOINT_C = -50.0
DEFAULT_MAX_SETPOINT_C = 200.0

_PROBE_TIMEOUT_S = 1.0              # shorter timeout used during protocol detection
_POST_ACTION_DELAY_S = 0.5          # mandatory pause after START / STOP (no reply expected)
_MAX_RESPONSE_BYTES = 512

NO_RESPONSE_MESSAGE = (
    "Keine Antwort vom HUBER CC230. Bitte MOXA RS232-Parameter, Kabeltyp, "
    "Nullmodem/1:1-Kabel, Geräteadresse, Remote-Modus und RS232-Menü am "
    "Thermostat prüfen."
)

# ── NAMUR/ASCII command templates ──────────────────────────────────────────────
_NAMUR: dict[str, str] = {
    "get_internal_temp":   "IN_PV_00",
    "get_external_temp":   "IN_PV_02",
    "get_setpoint":        "IN_SP_00",
    "get_analog_setpoint": "IN_SP_05",
    "set_setpoint":        "OUT_SP_00 {value:.2f}",
    "start":               "START",
    "stop":                "STOP",
    "status":              "STATUS",
}

# Actions for which the device sends no reply; a pause is required instead.
_NO_REPLY_ACTIONS: frozenset[str] = frozenset({"start", "stop"})

# ── STATUS code table ──────────────────────────────────────────────────────────
_STATUS_CODES: dict[int, str] = {
    -1: "Alarm",
    0:  "OK / Standby / Manual Stop",
    1:  "OK / Temperature control or bleed active",
    2:  "Remote Stop / Remote control active, temperature control off",
    3:  "Remote Start / Temperature control active with remote control",
}

_NUMERIC_RE = re.compile(r"[-+]?(?:\d+(?:[\.,]\d*)?|[\.,]\d+)")

_LINE_ENDINGS: dict[str, str] = {
    "cr":   "\r",
    "lf":   "\n",
    "crlf": "\r\n",
    "\r":   "\r",
    "\n":   "\n",
    "\r\n": "\r\n",
}

# Detection order: spec says CR first, CRLF fallback; LF kept as empirical last resort.
_DETECT_ENDINGS = ["\r", "\r\n", "\n"]
_DETECT_PROBES = ["get_internal_temp", "get_setpoint", "status"]


# ── Exceptions ─────────────────────────────────────────────────────────────────

class HuberCC230Error(RuntimeError):
    pass


class HuberCC230NoResponseError(HuberCC230Error):
    pass


# ── Logging helpers ────────────────────────────────────────────────────────────

def configure_cc230_logging(
    *,
    log_file: str | None = None,
    console: bool = False,
    level: int = logging.INFO,
) -> None:
    LOGGER.setLevel(level)
    LOGGER.propagate = True
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if console and not any(getattr(h, "_cc230_console", False) for h in LOGGER.handlers):
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        h.setLevel(level)
        h._cc230_console = True  # type: ignore[attr-defined]
        LOGGER.addHandler(h)
    if log_file and not any(getattr(h, "_cc230_log_file", None) == log_file for h in LOGGER.handlers):
        h = logging.FileHandler(log_file, encoding="utf-8")
        h.setFormatter(fmt)
        h.setLevel(level)
        h._cc230_log_file = log_file  # type: ignore[attr-defined]
        LOGGER.addHandler(h)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_wire(direction: str, payload: str, *, detail: str | None = None) -> None:
    suffix = f" {detail}" if detail else ""
    LOGGER.info("%s %s %r%s", _ts(), direction, payload, suffix)


# ── Line-ending helpers ────────────────────────────────────────────────────────

def normalize_line_ending(value: Any, *, field_name: str = "line_ending") -> str:
    if value in (None, ""):
        return DEFAULT_LINE_ENDING
    s = str(value)
    key = s.strip().lower()
    if key in _LINE_ENDINGS:
        return _LINE_ENDINGS[key]
    if s in _LINE_ENDINGS:
        return _LINE_ENDINGS[s]
    raise ValueError(f"Field '{field_name}' must be one of: cr, crlf, lf.")


def line_ending_name(value: str) -> str:
    e = normalize_line_ending(value)
    for name, raw in (("crlf", "\r\n"), ("cr", "\r"), ("lf", "\n")):
        if e == raw:
            return name
    return "custom"


# ── Response parsers ───────────────────────────────────────────────────────────

def parse_numeric_response(response: str | None, *, field_name: str = "response") -> float:
    """Return the measurement value from a NAMUR response string.

    The device may reply with a bare float ("+24.56"), or with an appended
    status nibble ("24.56 0"), or with an echo prefix ("IN_PV_00 24.56").
    In all cases the measurement is the last token that contains a decimal
    point; fall back to the last numeric token for integer-only replies.
    """
    text = str(response or "").strip()
    matches = _NUMERIC_RE.findall(text)
    if not matches:
        raise HuberCC230Error(
            f"Could not parse numeric {field_name} from Huber CC230 response: {text!r}."
        )
    decimal_matches = [m for m in matches if "." in m or "," in m]
    token = (decimal_matches[-1] if decimal_matches else matches[-1]).replace(",", ".")
    return float(token)


def parse_status_response(response: str | None) -> dict[str, Any]:
    """Parse a STATUS reply into a structured dict.

    The device returns a single integer: -1 (Alarm), 0, 1, 2, or 3.
    Some firmware appends extra fields separated by spaces; parse only
    the first integer token.
    """
    text = str(response or "").strip()
    # Match leading optional sign + digits only (integer part of first token).
    m = re.search(r"[-+]?\d+", text)
    if not m:
        raise HuberCC230Error(f"Could not parse STATUS from Huber CC230 response: {text!r}.")
    code = int(m.group())
    status_text = _STATUS_CODES.get(code, f"Unknown status code {code}")
    return {
        "code": code,
        "text": status_text,
        "temperature_control_active": code in (1, 3),
        "alarm": code == -1,
        "remote": code in (2, 3),
        "raw": text,
    }


def _is_plausible_response(response: str | None) -> bool:
    text = str(response or "").strip()
    if not text:
        return False
    upper = text.upper()
    if upper in {"?", "ERR", "ERROR", "NAK"} or upper.startswith(("ERR ", "ERROR ", "NAK ")):
        return False
    return True


def _format_namur_command(action: str, *, value: float | None = None) -> str:
    template = _NAMUR.get(action)
    if not template:
        raise HuberCC230Error(f"Unknown NAMUR action: {action!r}.")
    return template.format(value=0.0 if value is None else float(value))


# ── Env / config helpers ───────────────────────────────────────────────────────

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


def _coerce_float(value: Any, *, field_name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise DriverValidationError(f"Field '{field_name}' is required.")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be numeric.") from exc


def _coerce_int(value: Any, *, field_name: str, default: int, min_value: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be an integer.") from exc
    if parsed < min_value:
        raise DriverValidationError(f"Field '{field_name}' must be >= {min_value}.")
    return parsed


def _validate_setpoint(value: Any, *, min_setpoint_c: float, max_setpoint_c: float) -> float:
    try:
        temp = float(value)
    except (TypeError, ValueError) as exc:
        raise HuberCC230Error("Setpoint must be numeric.") from exc
    if min_setpoint_c >= max_setpoint_c:
        raise HuberCC230Error("min_setpoint_c must be lower than max_setpoint_c.")
    if not min_setpoint_c <= temp <= max_setpoint_c:
        raise HuberCC230Error(
            f"Setpoint {temp:g} °C is outside configured safety range "
            f"{min_setpoint_c:g}..{max_setpoint_c:g} °C."
        )
    return temp


# ── HuberCC230Client ───────────────────────────────────────────────────────────

class HuberCC230Client:
    """TCP client for a Huber CC230 connected through a MOXA NPort (TCP-to-RS232).

    Protocol: NAMUR/PP/LAI ASCII, 9600 8N1, no flow control.
    Always wait for a reply before sending the next command.
    START and STOP have no reply; the mandatory 500 ms pause is inserted
    automatically.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_MOXA_PORT,
        timeout: float = DEFAULT_TIMEOUT_S,
        line_ending: str = DEFAULT_LINE_ENDING,
        max_retries: int = DEFAULT_MAX_RETRIES,
        *,
        min_setpoint_c: float | None = None,
        max_setpoint_c: float | None = None,
        dry_run: bool = False,
        mock: bool = False,
        mock_responses: dict[str, str] | None = None,
        log_file: str | None = None,
        console_logging: bool = False,
    ):
        self.host = str(host or "").strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self.line_ending = normalize_line_ending(line_ending)
        self.max_retries = max(0, int(max_retries))
        self.min_setpoint_c = float(DEFAULT_MIN_SETPOINT_C if min_setpoint_c is None else min_setpoint_c)
        self.max_setpoint_c = float(DEFAULT_MAX_SETPOINT_C if max_setpoint_c is None else max_setpoint_c)
        self.dry_run = bool(dry_run)
        self.mock = bool(mock)
        self.mock_responses = dict(mock_responses or {})
        self.sock: socket.socket | None = None
        # Mock device state
        self._mock_setpoint = 20.0
        self._mock_temp = 20.0
        self._mock_external_temp: float | None = 20.0
        self._mock_running = False
        configure_cc230_logging(log_file=log_file, console=console_logging)

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if self.dry_run or self.mock:
            return
        if self.sock is not None:
            return
        if not self.host:
            raise HuberCC230Error("host must not be empty.")
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def disconnect(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()
        finally:
            self.sock = None

    def is_connected(self) -> bool:
        return self.dry_run or self.mock or self.sock is not None

    def close(self) -> None:
        self.disconnect()

    def __enter__(self) -> "HuberCC230Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ── Raw communication ──────────────────────────────────────────────────────

    def send_raw(self, command: str) -> str:
        return self.query(command)

    def query(self, command: str) -> str:
        return self._query_with_retries(command, retries=self.max_retries)

    # ── Protocol detection ─────────────────────────────────────────────────────

    def detect_protocol(self) -> dict[str, Any]:
        """Probe the device to find the working line terminator.

        Tries CR first (NAMUR spec), then CRLF, then LF.
        Sends only harmless read commands (IN_PV_00, IN_SP_00, STATUS).
        Raises HuberCC230NoResponseError if nothing responds.
        """
        if self.mock:
            return {"protocol": "namur", "line_ending": "cr", "command": "IN_PV_00", "response": "+23.45"}

        original_ending = self.line_ending
        original_timeout = self.timeout
        self.timeout = _PROBE_TIMEOUT_S
        if self.sock:
            self.disconnect()

        try:
            for ending in _DETECT_ENDINGS:
                self.line_ending = ending
                for action in _DETECT_PROBES:
                    command = _format_namur_command(action)
                    try:
                        self.connect()
                        response = self._query_with_retries(command, retries=0)
                    except Exception as exc:
                        _log_wire("DETECT MISS", f"{action}/{line_ending_name(ending)}", detail=str(exc))
                        self.disconnect()
                        continue
                    if _is_plausible_response(response):
                        result = {
                            "protocol": "namur",
                            "command": command,
                            "response": response,
                            "line_ending": line_ending_name(ending),
                        }
                        LOGGER.info("Huber CC230 protocol detected: %s", result)
                        # Keep detected ending for subsequent calls.
                        self.timeout = original_timeout
                        return result
                    self.disconnect()
        except Exception:
            self.line_ending = original_ending
            self.timeout = original_timeout
            raise

        self.line_ending = original_ending
        self.timeout = original_timeout
        raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)

    # ── Read methods ───────────────────────────────────────────────────────────

    def get_internal_temperature(self) -> float:
        """Read internal (bath / jacket) temperature via IN_PV_00."""
        response = self.query(_NAMUR["get_internal_temp"])
        return parse_numeric_response(response, field_name="internal temperature")

    def get_external_temperature(self) -> float | None:
        """Read external (process / reactor) temperature via IN_PV_02.

        Returns None when no external sensor is connected or the device
        signals an error for this channel.
        """
        if self.mock:
            return self._mock_external_temp
        try:
            response = self.query(_NAMUR["get_external_temp"])
            if not _is_plausible_response(response):
                return None
            return parse_numeric_response(response, field_name="external temperature")
        except (HuberCC230Error, OSError):
            return None

    def get_setpoint(self) -> float:
        """Read current temperature setpoint via IN_SP_00."""
        response = self.query(_NAMUR["get_setpoint"])
        return parse_numeric_response(response, field_name="setpoint")

    def get_analog_setpoint(self) -> float:
        """Read current analog setpoint via IN_SP_05."""
        response = self.query(_NAMUR["get_analog_setpoint"])
        return parse_numeric_response(response, field_name="analog setpoint")

    # ── Write / control methods ────────────────────────────────────────────────

    def set_setpoint(self, value: float) -> bool:
        """Set temperature setpoint via OUT_SP_00.

        Validates against min_setpoint_c / max_setpoint_c before sending.
        Every change is logged at WARNING level.
        """
        temp = _validate_setpoint(
            value,
            min_setpoint_c=self.min_setpoint_c,
            max_setpoint_c=self.max_setpoint_c,
        )
        LOGGER.warning("Huber CC230 setpoint change: %.2f °C", temp)
        if self.mock:
            self._mock_setpoint = temp
            return True
        command = _format_namur_command("set_setpoint", value=temp)
        if self.dry_run:
            _log_wire("DRY-RUN TX", self._wire(command))
            return True
        response = self.query(command)
        if not response:
            return False
        text = response.strip().upper()
        if text in {"OK", "ACK"}:
            return True
        # Some firmware echoes the new setpoint value as confirmation.
        try:
            confirmed = parse_numeric_response(response, field_name="setpoint echo")
            return abs(confirmed - temp) <= 0.2
        except HuberCC230Error:
            return True  # non-error reply counts as acknowledged

    def start_temperature_control(self) -> bool:
        """Send START.  No reply is expected; waits 500 ms after sending."""
        LOGGER.warning("Huber CC230 START requested.")
        if self.mock:
            self._mock_running = True
            return True
        return self._send_no_reply(_NAMUR["start"])

    def stop_temperature_control(self) -> bool:
        """Send STOP.  No reply is expected; waits 500 ms after sending."""
        LOGGER.warning("Huber CC230 STOP requested.")
        if self.mock:
            self._mock_running = False
            return True
        return self._send_no_reply(_NAMUR["stop"])

    def get_status(self) -> dict[str, Any]:
        """Read device status via STATUS and return a structured dict."""
        if self.mock:
            code = 1 if self._mock_running else 0
            return {
                "code": code,
                "text": _STATUS_CODES[code],
                "temperature_control_active": self._mock_running,
                "alarm": False,
                "remote": False,
                "raw": str(code),
            }
        response = self.query(_NAMUR["status"])
        return parse_status_response(response)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _wire(self, command: str) -> str:
        text = str(command or "")
        if text.endswith(("\r", "\n")):
            return text
        return f"{text}{self.line_ending}"

    def _send_no_reply(self, command: str) -> bool:
        if self.dry_run:
            wire = self._wire(command)
            _log_wire("DRY-RUN TX (no-reply)", wire)
            time.sleep(_POST_ACTION_DELAY_S)
            return True
        self.connect()
        assert self.sock is not None
        wire = self._wire(command)
        _log_wire("TX (no-reply)", wire)
        self.sock.sendall(wire.encode("ascii"))
        time.sleep(_POST_ACTION_DELAY_S)
        return True

    def _query_with_retries(self, command: str, *, retries: int) -> str:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self.mock:
                    return self._mock_query(command)
                if self.dry_run:
                    wire = self._wire(command)
                    _log_wire("DRY-RUN TX", wire)
                    return "DRY_RUN"
                self.connect()
                assert self.sock is not None
                wire = self._wire(command)
                _log_wire("TX", wire)
                self.sock.sendall(wire.encode("ascii"))
                response = self._read_response()
                _log_wire("RX", response)
                return response
            except (OSError, HuberCC230Error) as exc:
                last_error = exc
                _log_wire("ERROR", str(command), detail=f"attempt={attempt + 1} error={exc}")
                self.disconnect()
                if attempt >= retries:
                    break
        if isinstance(last_error, socket.timeout):
            raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE) from last_error
        if last_error is not None:
            raise last_error
        raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)

    def _read_response(self) -> str:
        if self.sock is None:
            raise HuberCC230Error("Socket is not connected.")
        data = bytearray()
        while len(data) < _MAX_RESPONSE_BYTES:
            try:
                chunk = self.sock.recv(1)
            except socket.timeout as exc:
                if data:
                    raise HuberCC230Error(
                        f"Incomplete response before timeout: {bytes(data)!r}."
                    ) from exc
                raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE) from exc
            if not chunk:
                if data:
                    raise HuberCC230Error(
                        f"Incomplete response before connection closed: {bytes(data)!r}."
                    )
                raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)
            if chunk == b"\r":
                # Consume optional trailing \n (CRLF terminator) so it does
                # not pollute the buffer of the next query.
                try:
                    nxt = self.sock.recv(1)
                    if nxt and nxt != b"\n":
                        data.extend(nxt)
                except socket.timeout:
                    pass
                break
            if chunk == b"\n":
                break
            data.extend(chunk)
        else:
            raise HuberCC230Error("Response exceeded maximum length.")
        response = bytes(data).decode("ascii", errors="replace")
        if not response:
            raise HuberCC230Error("Device returned an empty response.")
        return response

    def _mock_query(self, command: str) -> str:
        normalized = str(command or "").strip()
        _log_wire("MOCK TX", normalized)
        if normalized in self.mock_responses:
            response = self.mock_responses[normalized]
        else:
            response = self._default_mock_response(normalized)
        _log_wire("MOCK RX", response)
        return response

    def _default_mock_response(self, command: str) -> str:
        upper = command.upper()
        if upper == "IN_PV_00":
            return f"+{self._mock_temp:.2f}"
        if upper == "IN_PV_02":
            if self._mock_external_temp is None:
                raise HuberCC230Error("Mock: no external sensor.")
            return f"+{self._mock_external_temp:.2f}"
        if upper == "IN_SP_00" or upper == "IN_SP_05":
            return f"+{self._mock_setpoint:.2f}"
        if upper.startswith("OUT_SP_00"):
            self._mock_setpoint = parse_numeric_response(command, field_name="mock setpoint")
            return "OK"
        if upper == "STATUS":
            return "1" if self._mock_running else "0"
        if upper == "VERSION":
            return "HUBER CC230 MOCK"
        raise HuberCC230Error(f"Mock has no response for {command!r}.")


# ── _TransportHuberCC230Session ────────────────────────────────────────────────

class _TransportHuberCC230Session:
    """Thin wrapper around TcpSocketTransport for use inside HuberCC230Driver."""

    def __init__(
        self,
        transport: TcpSocketTransport,
        *,
        line_ending: str,
        max_retries: int,
    ):
        self.transport = transport
        self.line_ending = normalize_line_ending(line_ending)
        self.max_retries = max(0, int(max_retries))

    def query(self, command: str) -> str:
        return self._query_with_retries(command, retries=self.max_retries)

    def send_no_wait(self, command: str) -> None:
        wire = self._wire(command)
        _log_wire("TX (no-reply)", wire)
        self.transport.send(wire.encode("ascii"))
        time.sleep(_POST_ACTION_DELAY_S)

    def _query_with_retries(self, command: str, *, retries: int) -> str:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                wire = self._wire(command)
                _log_wire("TX", wire)
                self.transport.send(wire.encode("ascii"))
                raw = self.transport.receive_until(
                    self.line_ending.encode("ascii"),
                    max_bytes=max(self.transport.config.recv_size, _MAX_RESPONSE_BYTES),
                )
                response = raw.decode("ascii", errors="replace").rstrip("\r\n")
                if not response:
                    raise HuberCC230Error("Device returned an empty response.")
                _log_wire("RX", response)
                return response
            except (OSError, HuberCC230Error) as exc:
                last_error = exc
                _log_wire("ERROR", str(command), detail=f"attempt={attempt + 1} error={exc}")
                self.transport.close()
                if attempt >= retries:
                    break
        if isinstance(last_error, socket.timeout):
            raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE) from last_error
        if last_error is not None:
            raise last_error
        raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)

    def _wire(self, command: str) -> str:
        text = str(command or "")
        if text.endswith(("\r", "\n")):
            return text
        return f"{text}{self.line_ending}"


def _detect_with_session(session: _TransportHuberCC230Session) -> dict[str, Any]:
    """Run protocol detection probes through a transport session."""
    original_ending = session.line_ending
    for ending in _DETECT_ENDINGS:
        session.line_ending = ending
        for action in _DETECT_PROBES:
            command = _format_namur_command(action)
            try:
                response = session._query_with_retries(command, retries=0)
            except Exception as exc:
                _log_wire("DETECT MISS", f"{action}/{line_ending_name(ending)}", detail=str(exc))
                continue
            if _is_plausible_response(response):
                result = {
                    "protocol": "namur",
                    "command": command,
                    "response": response,
                    "line_ending": line_ending_name(ending),
                }
                LOGGER.info("Huber CC230 protocol detected: %s", result)
                return result
    session.line_ending = original_ending
    raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)


# ── HuberCC230Driver ───────────────────────────────────────────────────────────

class HuberCC230Driver(DeviceDriver):
    protocol_names = ("huber_cc230",)

    def execute(
        self,
        *,
        transport: TcpSocketTransport | None,
        request: DeviceCommandRequest,
    ) -> DeviceCommandResult:
        if transport is None:
            raise DriverError("Huber CC230 requires a TCP transport.")
        session = _TransportHuberCC230Session(
            transport,
            line_ending=self._line_ending(request.payload),
            max_retries=self._max_retries(request.payload),
        )
        return self._execute_with_session(session, request=request, driver_name="huber_cc230")

    def _execute_with_session(
        self,
        session: Any,
        *,
        request: DeviceCommandRequest,
        driver_name: str,
    ) -> DeviceCommandResult:
        payload = request.payload or {}
        command_name = str(request.command_name or "").strip().lower()
        min_sp = _coerce_float(
            payload.get("min_setpoint_c"),
            field_name="payload.min_setpoint_c",
            default=_env_float("HUBER_CC230_MIN_SETPOINT_C", DEFAULT_MIN_SETPOINT_C),
        )
        max_sp = _coerce_float(
            payload.get("max_setpoint_c"),
            field_name="payload.max_setpoint_c",
            default=_env_float("HUBER_CC230_MAX_SETPOINT_C", DEFAULT_MAX_SETPOINT_C),
        )
        if min_sp >= max_sp:
            raise DriverValidationError(
                "Field 'payload.min_setpoint_c' must be lower than 'payload.max_setpoint_c'."
            )

        try:
            # ── detect ────────────────────────────────────────────────────────
            if command_name in {"detect_protocol", "detect"}:
                detection = _detect_with_session(session)
                return self._result(
                    acknowledged=True,
                    response_text=str(detection.get("response") or ""),
                    response_hex=None,
                    metadata={"driver": driver_name, "value": detection, **detection},
                )

            # ── raw pass-through ──────────────────────────────────────────────
            if command_name in {"manual_text", "raw", "send_raw"}:
                text = str(payload.get("text", payload.get("command", ""))).strip()
                if not text:
                    raise DriverValidationError("Field 'payload.text' is required.")
                response = session.query(text)
                return self._result(
                    acknowledged=True,
                    response_text=response,
                    response_hex=response.encode("ascii", errors="replace").hex(),
                    metadata={"driver": driver_name, "value": response, "protocol": "raw"},
                )

            # ── named commands ────────────────────────────────────────────────
            action = self._action_for_command(command_name)

            if action == "set_setpoint":
                temp = _coerce_float(
                    payload.get("temp_c", payload.get("temperature_c")),
                    field_name="payload.temp_c",
                )
                try:
                    temp = _validate_setpoint(temp, min_setpoint_c=min_sp, max_setpoint_c=max_sp)
                except HuberCC230Error as exc:
                    raise DriverValidationError(str(exc)) from exc
                LOGGER.warning("Huber CC230 setpoint change via driver: %.2f °C", temp)
                command = _format_namur_command(action, value=temp)
            else:
                command = _format_namur_command(action)

            line_ending = line_ending_name(getattr(session, "line_ending", DEFAULT_LINE_ENDING))

            if action in _NO_REPLY_ACTIONS:
                session.send_no_wait(command)
                value: Any = True
                response = ""
            else:
                response = session.query(command)
                value = self._parse_value(action, response)

        except DriverValidationError:
            raise
        except HuberCC230NoResponseError as exc:
            raise DriverError(str(exc)) from exc
        except HuberCC230Error as exc:
            raise DriverError(str(exc)) from exc
        except ValueError as exc:
            raise DriverValidationError(str(exc)) from exc

        return self._result(
            acknowledged=True,
            response_text=response or None,
            response_hex=response.encode("ascii", errors="replace").hex() if response else None,
            metadata={
                "driver": driver_name,
                "protocol": "namur",
                "line_ending": line_ending,
                "command": command.rstrip("\r\n"),
                "value": value,
            },
        )

    @staticmethod
    def _parse_value(action: str, response: str) -> Any:
        if action in {"get_internal_temp", "get_external_temp", "get_setpoint", "get_analog_setpoint"}:
            return parse_numeric_response(response, field_name=action)
        if action == "set_setpoint":
            text = response.strip().upper()
            if text in {"OK", "ACK"}:
                return True
            try:
                return parse_numeric_response(response, field_name="setpoint echo")
            except HuberCC230Error:
                return True
        if action == "status":
            return parse_status_response(response)
        return response

    def _line_ending(self, payload: dict[str, Any]) -> str:
        value = payload.get("line_ending") or os.getenv("HUBER_CC230_LINE_ENDING", "crlf")
        try:
            return normalize_line_ending(value, field_name="payload.line_ending")
        except ValueError as exc:
            raise DriverValidationError(str(exc)) from exc

    def _max_retries(self, payload: dict[str, Any]) -> int:
        default = _env_int("HUBER_CC230_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        return _coerce_int(
            payload.get("max_retries"),
            field_name="payload.max_retries",
            default=default,
            min_value=0,
        )

    def _action_for_command(self, command_name: str) -> str:
        mapping = {
            "get_internal_temp":        "get_internal_temp",
            "get_internal_temperature": "get_internal_temp",
            "get_process_temp":         "get_internal_temp",
            "get_temp":                 "get_internal_temp",
            "get_external_temp":        "get_external_temp",
            "get_external_temperature": "get_external_temp",
            "get_setpoint":             "get_setpoint",
            "get_analog_setpoint":      "get_analog_setpoint",
            "set_setpoint":             "set_setpoint",
            "start":                    "start",
            "start_temperature_control": "start",
            "stop":                     "stop",
            "stop_temperature_control": "stop",
            "get_status":               "status",
            "status":                   "status",
        }
        if command_name not in mapping:
            raise DriverValidationError(f"Unsupported Huber CC230 command '{command_name}'.")
        return mapping[command_name]

    def _result(
        self,
        *,
        acknowledged: bool,
        response_text: str | None,
        response_hex: str | None,
        metadata: dict[str, Any],
    ) -> DeviceCommandResult:
        return DeviceCommandResult(
            acknowledged=acknowledged,
            response_text=response_text,
            response_hex=response_hex,
            metadata=metadata,
        )


# ── HuberCC230MockDriver ───────────────────────────────────────────────────────

class HuberCC230MockDriver(HuberCC230Driver):
    protocol_names = ("huber_cc230_mock",)
    uses_transport = False

    def execute(
        self,
        *,
        transport: TcpSocketTransport | None,
        request: DeviceCommandRequest,
    ) -> DeviceCommandResult:
        client = HuberCC230Client(
            host="mock",
            mock=True,
            line_ending=self._line_ending(request.payload or {}),
            max_retries=self._max_retries(request.payload or {}),
        )
        # Wrap the mock client as a session-like object for _execute_with_session.
        return self._execute_with_session(
            _MockClientSession(client),
            request=request,
            driver_name="huber_cc230_mock",
        )


class _MockClientSession:
    """Adapts HuberCC230Client (mock) to the session interface used by HuberCC230Driver."""

    def __init__(self, client: HuberCC230Client):
        self._client = client
        self.line_ending = client.line_ending

    def query(self, command: str) -> str:
        return self._client.query(command)

    def send_no_wait(self, command: str) -> None:
        upper = command.strip().upper()
        if upper == "START":
            self._client._mock_running = True
        elif upper == "STOP":
            self._client._mock_running = False
