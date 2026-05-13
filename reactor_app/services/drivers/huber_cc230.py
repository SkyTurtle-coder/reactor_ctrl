from __future__ import annotations

import logging
import os
import re
import socket
from datetime import datetime, timezone
from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from .huber_unistat import HuberUnistatTCP
from ..transports import TcpSocketTransport


LOGGER = logging.getLogger(__name__)

NO_RESPONSE_MESSAGE = (
    "Keine Antwort vom Huber CC230. Bitte RS232, Baudrate, Nullmodem-Kabel, "
    "Remote-Modus und MOXA-Port prüfen."
)
DEFAULT_MOXA_PORT = 4001
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_LINE_ENDING = "\r"
DEFAULT_MAX_RETRIES = 2
DEFAULT_MIN_SETPOINT_C = -50.0
DEFAULT_MAX_SETPOINT_C = 200.0
_MAX_RESPONSE_BYTES = 512
_NUMERIC_RE = re.compile(r"[-+]?(?:\d+(?:[\.,]\d*)?|[\.,]\d+)")
_LINE_ENDINGS = {
    "cr": "\r",
    "lf": "\n",
    "crlf": "\r\n",
    "\r": "\r",
    "\n": "\n",
    "\r\n": "\r\n",
}


# NAMUR/ASCII is implemented first because it is the safest shared subset for
# older CC230 devices. PB is mapped through the existing Huber PB helpers.
# LAI differs by device generation; keep the structure explicit instead of
# inventing unsafe commands.
COMMAND_MAP: dict[str, dict[str, str | None]] = {
    "namur": {
        "get_temp": "IN_PV_00",
        "get_setpoint": "IN_SP_00",
        "set_setpoint": "OUT_SP_00 {value:.2f}",
        "start": "START",
        "stop": "STOP",
        "status": "STATUS",
        "version": "VERSION",
    },
    "lai": {
        # TODO: Fill with verified CC230 LAI commands after checking the
        # specific device manual/firmware. Do not guess write commands.
        "get_temp": None,
        "get_setpoint": None,
        "set_setpoint": None,
        "start": None,
        "stop": None,
        "status": None,
        "version": None,
    },
    "pb": {
        "get_temp": "{{M01****\r\n",
        "get_setpoint": "{{M00****\r\n",
        "set_setpoint": "{{M00{value_pb}\r\n",
        "start": "{{M140001\r\n",
        "stop": "{{M140000\r\n",
        "status": "{{M0A****\r\n",
        "version": None,
    },
}

_PB_ADDR_BY_ACTION = {
    "get_temp": "01",
    "get_setpoint": "00",
    "set_setpoint": "00",
    "start": "14",
    "stop": "14",
    "status": "0A",
}


class HuberCC230Error(RuntimeError):
    pass


class HuberCC230NoResponseError(HuberCC230Error):
    pass


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


def normalize_line_ending(value: Any, *, field_name: str = "line_ending") -> str:
    if value in (None, ""):
        return DEFAULT_LINE_ENDING
    normalized = str(value)
    lookup_key = normalized.strip().lower()
    if lookup_key in _LINE_ENDINGS:
        return _LINE_ENDINGS[lookup_key]
    if normalized in _LINE_ENDINGS:
        return _LINE_ENDINGS[normalized]
    allowed = "cr, crlf, lf"
    raise ValueError(f"Field '{field_name}' must be one of: {allowed}.")


def line_ending_name(value: str) -> str:
    ending = normalize_line_ending(value)
    for name, raw in (("cr", "\r"), ("crlf", "\r\n"), ("lf", "\n")):
        if ending == raw:
            return name
    return "custom"


def configure_cc230_logging(
    *,
    log_file: str | None = None,
    console: bool = False,
    level: int = logging.INFO,
) -> None:
    LOGGER.setLevel(level)
    LOGGER.propagate = True

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if console and not any(getattr(handler, "_huber_cc230_console", False) for handler in LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.setLevel(level)
        handler._huber_cc230_console = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)
    if log_file and not any(getattr(handler, "_huber_cc230_log_file", None) == log_file for handler in LOGGER.handlers):
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(level)
        handler._huber_cc230_log_file = log_file  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_wire(direction: str, payload: str, *, detail: str | None = None) -> None:
    suffix = f" {detail}" if detail else ""
    LOGGER.info("%s %s %r%s", _timestamp(), direction, payload, suffix)


def _is_error_response(response: str | None) -> bool:
    normalized = str(response or "").strip().upper()
    return normalized in {"?", "ERR", "ERROR", "NAK"} or normalized.startswith(("ERR ", "ERROR ", "NAK "))


def _is_plausible_response(response: str | None) -> bool:
    return bool(str(response or "").strip()) and not _is_error_response(response)


def parse_numeric_response(response: str | None, *, field_name: str = "response") -> float:
    text = str(response or "").strip()
    matches = _NUMERIC_RE.findall(text)
    if not matches:
        raise HuberCC230Error(f"Could not parse numeric {field_name} from Huber CC230 response: {text!r}.")
    # Huber/LAI/NAMUR replies often echo the command first, e.g.
    # "IN_PV_00 25.12". Use the last numeric token so the channel suffix "00"
    # is not mistaken for the measurement.
    token = matches[-1].replace(",", ".")
    return float(token)


def _parse_bool_ack(response: str | None) -> bool:
    text = str(response or "").strip()
    normalized = text.upper()
    if not text or _is_error_response(text):
        return False
    if normalized in {"OK", "ACK", "1", "ON", "START", "STARTED", "RUN", "RUNNING", "STOP", "STOPPED", "OFF"}:
        return True
    return True


def _status_from_text(response: str) -> dict[str, Any]:
    normalized = str(response or "").strip().upper()
    running = any(token in normalized for token in ("RUN", "ON", "START"))
    stopped = any(token in normalized for token in ("STOP", "OFF", "IDLE"))
    return {
        "raw": response,
        "temperature_control_active": running and not stopped,
        "stopped": stopped,
        "error": _is_error_response(response) or "ERROR" in normalized or "ERR" in normalized,
        "warning": "WARN" in normalized,
    }


def _normalize_protocol(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "auto"}:
        return "auto"
    if normalized not in COMMAND_MAP:
        allowed = ", ".join(sorted(["auto", *COMMAND_MAP.keys()]))
        raise HuberCC230Error(f"Huber CC230 protocol must be one of: {allowed}.")
    return normalized


def _format_command(protocol: str, action: str, *, value: float | None = None) -> str:
    mapping = COMMAND_MAP.get(protocol) or {}
    template = mapping.get(action)
    if not template:
        raise HuberCC230Error(f"Huber CC230 protocol '{protocol}' does not define command '{action}'.")
    format_payload = {
        "value": 0.0 if value is None else float(value),
        "value_pb": HuberUnistatTCP.encode_temp(0.0 if value is None else float(value)),
    }
    return template.format(**format_payload)


def _parse_protocol_value(protocol: str, action: str, response: str) -> Any:
    if protocol == "pb":
        addr = _PB_ADDR_BY_ACTION[action]
        value_hex = HuberUnistatTCP.validate_response(response, addr)
        if action in {"get_temp", "get_setpoint", "set_setpoint"}:
            return HuberUnistatTCP.decode_temp(value_hex)
        if action == "status":
            raw = int(value_hex, 16)
            return {"raw": raw, **HuberUnistatTCP.status_bits(raw)}
        if action == "start":
            return value_hex == "0001"
        if action == "stop":
            return value_hex == "0000"
        return value_hex

    if action in {"get_temp", "get_setpoint"}:
        return parse_numeric_response(response, field_name=action)
    if action == "set_setpoint":
        return _parse_bool_ack(response)
    if action in {"start", "stop"}:
        return _parse_bool_ack(response)
    if action == "status":
        return _status_from_text(response)
    return response


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
        raise HuberCC230Error("Huber CC230 setpoint must be numeric.") from exc
    if min_setpoint_c >= max_setpoint_c:
        raise HuberCC230Error("Minimum setpoint must be lower than maximum setpoint.")
    if not min_setpoint_c <= temp <= max_setpoint_c:
        raise HuberCC230Error(
            f"Setpoint {temp:g} degC is outside configured safety range "
            f"{min_setpoint_c:g}..{max_setpoint_c:g} degC."
        )
    return temp


class HuberCC230Client:
    """TCP client for a Huber CC230 connected through a MOXA TCP-RS232 port."""

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
        protocol: str = "auto",
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
        self.min_setpoint_c = float(
            DEFAULT_MIN_SETPOINT_C if min_setpoint_c is None else min_setpoint_c
        )
        self.max_setpoint_c = float(
            DEFAULT_MAX_SETPOINT_C if max_setpoint_c is None else max_setpoint_c
        )
        self.protocol = _normalize_protocol(protocol)
        self.dry_run = bool(dry_run)
        self.mock = bool(mock)
        self.mock_responses = dict(mock_responses or {})
        self.sock: socket.socket | None = None
        self.last_detection: dict[str, Any] | None = None
        self._mock_setpoint = 20.0
        self._mock_temp = 20.0
        self._mock_running = False
        configure_cc230_logging(log_file=log_file, console=console_logging)

    def connect(self) -> None:
        if self.dry_run or self.mock:
            return
        if self.sock is not None:
            return
        if not self.host:
            raise HuberCC230Error("Huber CC230 host must not be empty.")
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

    def send_raw(self, command: str) -> str:
        return self.query(command)

    def query(self, command: str) -> str:
        return self._query_with_retries(command, retries=self.max_retries)

    def detect_protocol(self) -> dict[str, Any]:
        result = _detect_protocol_with_session(self)
        self.protocol = str(result["protocol"])
        self.line_ending = normalize_line_ending(result["line_ending"])
        self.last_detection = result
        return result

    def get_internal_temperature(self) -> float:
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "get_temp"))
        return float(_parse_protocol_value(protocol, "get_temp", response))

    def get_setpoint(self) -> float:
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "get_setpoint"))
        return float(_parse_protocol_value(protocol, "get_setpoint", response))

    def set_setpoint(self, value: float) -> bool:
        temp = _validate_setpoint(
            value,
            min_setpoint_c=self.min_setpoint_c,
            max_setpoint_c=self.max_setpoint_c,
        )
        LOGGER.warning("Huber CC230 setpoint change requested: %.2f degC", temp)
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "set_setpoint", value=temp))
        parsed = _parse_protocol_value(protocol, "set_setpoint", response)
        if isinstance(parsed, bool):
            return parsed
        if isinstance(parsed, (int, float)):
            return abs(float(parsed) - temp) <= 0.2
        return bool(parsed)

    def start_temperature_control(self) -> bool:
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "start"))
        return bool(_parse_protocol_value(protocol, "start", response))

    def stop_temperature_control(self) -> bool:
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "stop"))
        return bool(_parse_protocol_value(protocol, "stop", response))

    def get_status(self) -> dict[str, Any]:
        protocol = self._ensure_protocol()
        response = self.query(_format_command(protocol, "status"))
        parsed = _parse_protocol_value(protocol, "status", response)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def close(self) -> None:
        self.disconnect()

    def __enter__(self) -> "HuberCC230Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    def _ensure_protocol(self) -> str:
        if self.protocol == "auto":
            return str(self.detect_protocol()["protocol"])
        return self.protocol

    def _wire_command(self, command: str) -> str:
        command_text = str(command or "")
        if command_text.endswith(("\r", "\n")):
            return command_text
        return f"{command_text}{self.line_ending}"

    def _query_with_retries(self, command: str, *, retries: int) -> str:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self.mock:
                    return self._mock_query(command)
                if self.dry_run:
                    wire_command = self._wire_command(command)
                    _log_wire("DRY-RUN TX", wire_command)
                    return "DRY_RUN"
                self.connect()
                assert self.sock is not None
                wire_command = self._wire_command(command)
                _log_wire("TX", wire_command)
                self.sock.sendall(wire_command.encode("ascii"))
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
            raise HuberCC230Error("Huber CC230 socket is not connected.")

        data = bytearray()
        while len(data) < _MAX_RESPONSE_BYTES:
            try:
                chunk = self.sock.recv(1)
            except socket.timeout as exc:
                if data:
                    raise HuberCC230Error(
                        f"Incomplete Huber CC230 response before timeout: {bytes(data)!r}."
                    ) from exc
                raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE) from exc
            if not chunk:
                if data:
                    raise HuberCC230Error(f"Incomplete Huber CC230 response before connection closed: {bytes(data)!r}.")
                raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)
            data.extend(chunk)
            if chunk in (b"\r", b"\n"):
                break
        else:
            raise HuberCC230Error("Huber CC230 response exceeded maximum response length.")

        response = bytes(data).decode("ascii", errors="replace").rstrip("\r\n")
        if not response:
            raise HuberCC230Error("Huber CC230 returned an empty response.")
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
        normalized = command.upper()
        if normalized == "IN_PV_00":
            return f"{self._mock_temp:.2f}"
        if normalized == "IN_SP_00":
            return f"{self._mock_setpoint:.2f}"
        if normalized.startswith("OUT_SP_00"):
            self._mock_setpoint = parse_numeric_response(normalized, field_name="mock setpoint command")
            return "OK"
        if normalized == "START":
            self._mock_running = True
            return "OK"
        if normalized == "STOP":
            self._mock_running = False
            return "OK"
        if normalized == "STATUS":
            return "RUNNING" if self._mock_running else "STOPPED"
        if normalized == "VERSION":
            return "HUBER CC230 MOCK"
        if normalized == "{M01****":
            return "{S0107D0"
        if normalized == "{M00****":
            return HuberUnistatTCP.build_request("00", HuberUnistatTCP.encode_temp(self._mock_setpoint)).replace("{M", "{S").rstrip()
        raise HuberCC230Error(f"Mock Huber CC230 has no response configured for {command!r}.")


class _TransportHuberCC230Session:
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

    def _query_with_retries(self, command: str, *, retries: int) -> str:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                wire_command = self._wire_command(command)
                request_bytes = wire_command.encode("ascii")
                _log_wire("TX", wire_command)
                self.transport.send(request_bytes)
                response_bytes = self.transport.receive_until(
                    self.line_ending.encode("ascii"),
                    max_bytes=max(self.transport.config.recv_size, _MAX_RESPONSE_BYTES),
                )
                response = response_bytes.decode("ascii", errors="replace").rstrip("\r\n")
                if not response:
                    raise HuberCC230Error("Huber CC230 returned an empty response.")
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

    def _wire_command(self, command: str) -> str:
        command_text = str(command or "")
        if command_text.endswith(("\r", "\n")):
            return command_text
        return f"{command_text}{self.line_ending}"


def _detect_protocol_with_session(session: Any) -> dict[str, Any]:
    original_line_ending = normalize_line_ending(getattr(session, "line_ending", DEFAULT_LINE_ENDING))
    candidate_endings = [original_line_ending]
    if original_line_ending != "\r\n":
        candidate_endings.append("\r\n")

    probes = [
        ("namur", "get_temp"),
        ("namur", "get_setpoint"),
        ("namur", "status"),
        ("namur", "version"),
        ("pb", "get_temp"),
        ("pb", "get_setpoint"),
    ]
    for ending in candidate_endings:
        session.line_ending = ending
        for protocol, action in probes:
            try:
                command = _format_command(protocol, action)
                response = session._query_with_retries(command, retries=0)
            except Exception as exc:
                _log_wire("DETECT MISS", f"{protocol}:{action}", detail=str(exc))
                continue
            if _is_plausible_response(response):
                result = {
                    "protocol": protocol,
                    "command": command.rstrip("\r\n"),
                    "response": response,
                    "line_ending": line_ending_name(ending),
                }
                LOGGER.info("Huber CC230 protocol detected: %s", result)
                return result

    session.line_ending = original_line_ending
    raise HuberCC230NoResponseError(NO_RESPONSE_MESSAGE)


class HuberCC230Driver(DeviceDriver):
    protocol_names = ("huber_cc230",)

    def execute(self, *, transport: TcpSocketTransport | None, request: DeviceCommandRequest) -> DeviceCommandResult:
        if transport is None:
            raise DriverError("Huber CC230 requires a TCP transport.")
        return self._execute_with_session(
            _TransportHuberCC230Session(
                transport,
                line_ending=self._line_ending(request.payload),
                max_retries=self._max_retries(request.payload),
            ),
            request=request,
            driver_name="huber_cc230",
        )

    def _execute_with_session(
        self,
        session: Any,
        *,
        request: DeviceCommandRequest,
        driver_name: str,
    ) -> DeviceCommandResult:
        payload = request.payload or {}
        command_name = str(request.command_name or "").strip().lower()
        min_setpoint = _coerce_float(
            payload.get("min_setpoint_c"),
            field_name="payload.min_setpoint_c",
            default=_env_float("HUBER_CC230_MIN_SETPOINT_C", DEFAULT_MIN_SETPOINT_C),
        )
        max_setpoint = _coerce_float(
            payload.get("max_setpoint_c"),
            field_name="payload.max_setpoint_c",
            default=_env_float("HUBER_CC230_MAX_SETPOINT_C", DEFAULT_MAX_SETPOINT_C),
        )
        if min_setpoint >= max_setpoint:
            raise DriverValidationError("Field 'payload.min_setpoint_c' must be lower than 'payload.max_setpoint_c'.")

        try:
            protocol = _normalize_protocol(payload.get("protocol_variant", payload.get("cc230_protocol", "auto")))
            if command_name in {"detect_protocol", "detect"}:
                detection = _detect_protocol_with_session(session)
                return self._result(
                    acknowledged=True,
                    response_text=str(detection.get("response") or ""),
                    response_hex=None,
                    metadata={"driver": driver_name, "value": detection, **detection},
                )

            if command_name in {"manual_text", "raw", "send_raw"}:
                command_text = str(payload.get("text", payload.get("command", ""))).strip()
                if not command_text:
                    raise DriverValidationError("Field 'payload.text' is required.")
                response = session.query(command_text)
                return self._result(
                    acknowledged=True,
                    response_text=response,
                    response_hex=response.encode("ascii", errors="replace").hex(),
                    metadata={"driver": driver_name, "value": response, "protocol": "raw"},
                )

            action = self._action_for_command(command_name)
            if protocol == "auto":
                detection = _detect_protocol_with_session(session)
                protocol = str(detection["protocol"])
            if action == "set_setpoint":
                temp = _coerce_float(payload.get("temp_c", payload.get("temperature_c")), field_name="payload.temp_c")
                try:
                    temp = _validate_setpoint(temp, min_setpoint_c=min_setpoint, max_setpoint_c=max_setpoint)
                except HuberCC230Error as exc:
                    raise DriverValidationError(str(exc)) from exc
                LOGGER.warning("Huber CC230 setpoint change requested through driver: %.2f degC", temp)
                command = _format_command(protocol, action, value=temp)
            else:
                command = _format_command(protocol, action)

            response = session.query(command)
            value = _parse_protocol_value(protocol, action, response)
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
            response_text=response,
            response_hex=response.encode("ascii", errors="replace").hex(),
            metadata={
                "driver": driver_name,
                "protocol": protocol,
                "line_ending": line_ending_name(getattr(session, "line_ending", DEFAULT_LINE_ENDING)),
                "command": command.rstrip("\r\n"),
                "value": value,
            },
        )

    def _line_ending(self, payload: dict[str, Any]) -> str:
        value = payload.get("line_ending") or os.getenv("HUBER_CC230_LINE_ENDING", "cr")
        try:
            return normalize_line_ending(value, field_name="payload.line_ending")
        except ValueError as exc:
            raise DriverValidationError(str(exc)) from exc

    def _max_retries(self, payload: dict[str, Any]) -> int:
        default = _env_int("HUBER_CC230_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        return _coerce_int(payload.get("max_retries"), field_name="payload.max_retries", default=default, min_value=0)

    def _action_for_command(self, command_name: str) -> str:
        mapping = {
            "get_internal_temp": "get_temp",
            "get_internal_temperature": "get_temp",
            "get_process_temp": "get_temp",
            "get_temp": "get_temp",
            "get_setpoint": "get_setpoint",
            "set_setpoint": "set_setpoint",
            "start": "start",
            "start_temperature_control": "start",
            "stop": "stop",
            "stop_temperature_control": "stop",
            "get_status": "status",
            "status": "status",
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


class HuberCC230MockDriver(HuberCC230Driver):
    protocol_names = ("huber_cc230_mock",)
    uses_transport = False

    def execute(self, *, transport: TcpSocketTransport | None, request: DeviceCommandRequest) -> DeviceCommandResult:
        client = HuberCC230Client(
            host="mock",
            mock=True,
            protocol="namur",
            line_ending=self._line_ending(request.payload or {}),
            max_retries=self._max_retries(request.payload or {}),
        )
        return self._execute_with_session(client, request=request, driver_name="huber_cc230_mock")
