from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from .capabilities import DeviceCapability
from ..transports.interface import ITransport


LOGGER = logging.getLogger(__name__)

_CRLF = b"\r\n"
_DEFAULT_MAX_RESPONSE_BYTES = 8192
_DEFAULT_RETRY_DELAY_MS = 250
_GENERAL_ERRORS = {
    "ES": "Syntax error or command not recognized.",
    "ET": "Transmission error.",
    "EL": "Logical error; command cannot be executed.",
}
_DEVICE_ERROR_CODES = {
    "1": "boot_error",
    "2": "brand_error",
    "3": "checksum_error",
    "9": "option_fail",
    "10": "eeprom_error",
    "11": "device_mismatch",
    "12": "hot_plug_out",
    "14": "weight_module_electronic_mismatch",
    "15": "adjustment_needed",
}
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


class MTSicsError(DriverError):
    """Base class for MT-SICS protocol/device errors."""


class MTSicsProtocolError(MTSicsError):
    """Raised when a response cannot be decoded or parsed as MT-SICS."""


class MTSicsCommandError(MTSicsError):
    """Raised when the scale answers with a command-specific error status."""


class MTSicsNotReadyError(MTSicsCommandError):
    """Raised for MT-SICS I responses: understood, but not executable now."""


class MTSicsUnsupportedCommandError(MTSicsCommandError):
    """Raised when the device reports an unsupported or invalid command."""


class MTSicsOverloadError(MTSicsCommandError):
    """Raised when the weighing range is overloaded."""


class MTSicsUnderloadError(MTSicsCommandError):
    """Raised when the weighing range is underloaded."""


@dataclass(frozen=True)
class MTSicsWeightReading:
    value: Decimal
    unit: str
    stable: bool
    status: str
    measured_at: datetime
    raw_response: str
    quality_status: str
    device_serial: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "value": float(self.value),
            "value_decimal": str(self.value),
            "unit": self.unit,
            "stable": self.stable,
            "status": self.status,
            "quality_status": self.quality_status,
            "measured_at": self.measured_at.isoformat(),
            "raw_response": self.raw_response,
            "device_serial": self.device_serial,
        }


@dataclass(frozen=True)
class MTSicsTareResult:
    value: Decimal
    unit: str
    raw_response: str

    def to_metadata(self) -> dict[str, Any]:
        return {
            "value": float(self.value),
            "value_decimal": str(self.value),
            "unit": self.unit,
            "raw_response": self.raw_response,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_bool(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    raise DriverValidationError(f"Field '{field_name}' must be a boolean.")


def _coerce_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    min_value: int = 0,
    max_value: int | None = None,
) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be an integer.") from exc
    if parsed < min_value:
        raise DriverValidationError(f"Field '{field_name}' must be >= {min_value}.")
    if max_value is not None and parsed > max_value:
        raise DriverValidationError(f"Field '{field_name}' must be <= {max_value}.")
    return parsed


def _coerce_command(value: Any, *, field_name: str = "command") -> str:
    command = str(value or "").strip()
    if not command:
        raise DriverValidationError(f"Field '{field_name}' must not be empty.")
    if "\r" in command or "\n" in command:
        raise DriverValidationError(f"Field '{field_name}' must not contain line breaks.")
    try:
        command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DriverValidationError(f"Field '{field_name}' must be ASCII.") from exc
    return command


def _response_tokens(line: str) -> list[str]:
    return [token for token in str(line or "").strip().split() if token]


def _unquote(text: str) -> str:
    return text.replace(r"\"", '"').replace(r"\\", "\\")


def _quoted_values(line: str) -> list[str]:
    return [_unquote(match.group(1)) for match in _QUOTED_RE.finditer(line)]


def _parse_decimal(token: str, *, line: str) -> Decimal:
    normalized = str(token or "").strip().replace(",", ".")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise MTSicsProtocolError(f"MT-SICS response contains an invalid numeric value: {line!r}.") from exc


def _raise_command_status(command: str, status: str, line: str) -> None:
    normalized_status = str(status or "").strip()
    if normalized_status == "I":
        raise MTSicsNotReadyError(
            f"MT-SICS command {command} is understood but not executable at present: {line!r}."
        )
    if normalized_status == "L":
        raise MTSicsUnsupportedCommandError(
            f"MT-SICS command {command} is not executable or has invalid parameters: {line!r}."
        )
    if normalized_status == "+":
        raise MTSicsOverloadError(f"Scale reports overload for MT-SICS command {command}: {line!r}.")
    if normalized_status in {"-", "–"}:
        raise MTSicsUnderloadError(f"Scale reports underload for MT-SICS command {command}: {line!r}.")
    raise MTSicsCommandError(f"MT-SICS command {command} failed with status {normalized_status!r}: {line!r}.")


def parse_weight_response(line: str, *, measured_at: datetime | None = None) -> MTSicsWeightReading:
    tokens = _response_tokens(line)
    if len(tokens) < 2 or tokens[0] != "S":
        raise MTSicsProtocolError(f"MT-SICS weight response must start with 'S': {line!r}.")

    status = tokens[1]
    if status in {"S", "D"}:
        if len(tokens) < 4:
            raise MTSicsProtocolError(f"MT-SICS weight response is incomplete: {line!r}.")
        value = _parse_decimal(tokens[2], line=line)
        unit = tokens[3]
        return MTSicsWeightReading(
            value=value,
            unit=unit,
            stable=status == "S",
            status=status,
            measured_at=measured_at or _utc_now(),
            raw_response=line,
            quality_status="stable" if status == "S" else "dynamic",
        )

    if status == "Error":
        error_number = tokens[2] if len(tokens) >= 3 else ""
        trigger = tokens[3] if len(tokens) >= 4 else ""
        error_name = _DEVICE_ERROR_CODES.get(error_number, "device_error")
        raise MTSicsCommandError(
            f"Scale reports {error_name} ({error_number}) from trigger {trigger or 'unknown'}: {line!r}."
        )

    _raise_command_status("S", status, line)
    raise AssertionError("unreachable")


def parse_tare_response(line: str) -> MTSicsTareResult:
    tokens = _response_tokens(line)
    if len(tokens) < 2 or tokens[0] != "T":
        raise MTSicsProtocolError(f"MT-SICS tare response must start with 'T': {line!r}.")
    status = tokens[1]
    if status == "S":
        if len(tokens) < 4:
            raise MTSicsProtocolError(f"MT-SICS tare response is incomplete: {line!r}.")
        return MTSicsTareResult(
            value=_parse_decimal(tokens[2], line=line),
            unit=tokens[3],
            raw_response=line,
        )
    _raise_command_status("T", status, line)
    raise AssertionError("unreachable")


def parse_ack_response(command: str, line: str) -> dict[str, Any]:
    tokens = _response_tokens(line)
    expected = str(command or "").strip().upper()
    if len(tokens) < 2 or tokens[0] != expected:
        raise MTSicsProtocolError(f"MT-SICS response for {expected} is malformed: {line!r}.")
    status = tokens[1]
    if status == "A":
        return {"acknowledged": True, "raw_response": line}
    _raise_command_status(expected, status, line)
    raise AssertionError("unreachable")


def parse_i0_response(lines: list[str]) -> dict[str, Any]:
    supported: list[str] = []
    entries: list[dict[str, Any]] = []
    for line in lines:
        tokens = _response_tokens(line)
        if len(tokens) < 2 or tokens[0] != "I0":
            raise MTSicsProtocolError(f"Unexpected line in I0 response: {line!r}.")
        status = tokens[1]
        if status in {"I", "L", "+", "-", "–"}:
            _raise_command_status("I0", status, line)
        quoted = _quoted_values(line)
        command_name = quoted[0] if quoted else None
        level = None
        if len(tokens) >= 3:
            try:
                level = int(tokens[2])
            except ValueError:
                level = None
        if command_name:
            supported.append(command_name)
            entries.append({"command": command_name, "level": level, "raw_response": line})
    return {"supported_commands": supported, "entries": entries, "raw_responses": lines}


def parse_identification_response(command: str, line: str) -> dict[str, Any]:
    expected = str(command or "").strip().upper()
    tokens = _response_tokens(line)
    if len(tokens) < 2 or tokens[0] != expected:
        raise MTSicsProtocolError(f"MT-SICS response for {expected} is malformed: {line!r}.")
    status = tokens[1]
    if status != "A":
        _raise_command_status(expected, status, line)
    quoted = _quoted_values(line)
    value = quoted[0] if quoted else " ".join(tokens[2:])
    return {
        "command": expected,
        "status": status,
        "value": value,
        "quoted_values": quoted,
        "raw_response": line,
    }


class _MTSicsSession:
    def __init__(
        self,
        transport: ITransport,
        *,
        log_raw_telegrams: bool = False,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.transport = transport
        self.log_raw_telegrams = bool(log_raw_telegrams)
        self.max_response_bytes = int(max_response_bytes)
        self._rx_buffer = bytearray()
        self.skipped_lines: list[str] = []

    def _drain_stale_input(self) -> None:
        # A fresh session's rx buffer starts empty, so bytes left on the wire
        # from a prior command's over-read (e.g. an unsolicited I4 boot
        # telegram sent right after a weight response) would otherwise sit
        # unread until they collide with the next command's real response.
        # Best-effort only: harmless no-op for transports without this method
        # (e.g. the fake transport used in unit tests).
        drain = getattr(self.transport, "drain_input", None)
        if not callable(drain):
            return
        try:
            drained = drain(max_bytes=self.max_response_bytes, idle_timeout_s=0.02)
            if drained and self.log_raw_telegrams:
                LOGGER.debug("MT-SICS drained %d stale input byte(s) before command: %r", len(drained), drained)
        except Exception:
            LOGGER.debug("MT-SICS input drain failed; continuing with command send.", exc_info=True)

    def send_command(self, command: str) -> bytes:
        command_text = _coerce_command(command)
        self._drain_stale_input()
        payload = command_text.encode("ascii") + _CRLF
        if self.log_raw_telegrams:
            LOGGER.debug("MT-SICS send: %r", payload)
        self.transport.send(payload)
        return payload

    def read_line(self) -> str:
        while True:
            newline_index = self._rx_buffer.find(b"\n")
            if newline_index >= 0:
                raw_line = bytes(self._rx_buffer[: newline_index + 1])
                del self._rx_buffer[: newline_index + 1]
                if self.log_raw_telegrams:
                    LOGGER.debug("MT-SICS recv: %r", raw_line)
                return self._decode_line(raw_line)

            chunk = self.transport.receive_until(b"\n", max_bytes=self.max_response_bytes)
            if not chunk:
                raise socket.timeout("No MT-SICS response bytes were received.")
            self._rx_buffer.extend(chunk)

    def read_response_for(
        self,
        command: str,
        *,
        multiline_until_status_a: bool = False,
        max_lines: int = 64,
    ) -> list[str]:
        expected = str(command or "").strip().upper()
        lines: list[str] = []
        while len(lines) < max_lines:
            line = self.read_line()
            if not line:
                continue
            self._raise_if_general_error(line)
            if not self._matches_expected_response(expected, line):
                self.skipped_lines.append(line)
                LOGGER.info("Skipping unsolicited MT-SICS response while waiting for %s: %r", expected, line)
                continue
            lines.append(line)
            if not multiline_until_status_a:
                return lines
            tokens = _response_tokens(line)
            if len(tokens) >= 2 and tokens[1] == "A":
                return lines
            if len(tokens) >= 2 and tokens[1] not in {"B", "A"}:
                return lines
        raise socket.timeout(f"MT-SICS response for {expected} exceeded {max_lines} line(s).")

    def _decode_line(self, raw_line: bytes) -> str:
        try:
            text = raw_line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise MTSicsProtocolError(f"MT-SICS response is not valid ASCII: {raw_line.hex()}.") from exc
        return text.rstrip("\r\n")

    def _raise_if_general_error(self, line: str) -> None:
        tokens = _response_tokens(line)
        if not tokens:
            return
        error = _GENERAL_ERRORS.get(tokens[0])
        if error is not None:
            raise MTSicsCommandError(f"MT-SICS general error {tokens[0]}: {error} Raw response: {line!r}.")

    def _matches_expected_response(self, expected: str, line: str) -> bool:
        tokens = _response_tokens(line)
        if not tokens:
            return False
        first = tokens[0]
        if expected in {"S", "SI", "SIR", "SIU", "SIRU", "SNR", "SR"}:
            return first == "S"
        return first == expected


class MettlerToledoICS435Driver(DeviceDriver):
    protocol_names = ("mettler_toledo_ics435", "ics435_mtsics")
    persistent_transport = True

    def get_capabilities(self) -> frozenset[str]:
        return frozenset({
            DeviceCapability.CAN_WEIGH,
            DeviceCapability.HAS_FEEDBACK,
            DeviceCapability.SUPPORTS_MANUAL_MODE,
        })

    def execute(self, *, transport: ITransport, request: DeviceCommandRequest) -> DeviceCommandResult:
        if transport is None:
            raise DriverValidationError("MT-SICS driver requires a transport.")

        command_name = str(request.command_name or "").strip().lower()
        payload = request.payload or {}
        max_response_bytes = _coerce_int(
            payload.get("max_response_bytes"),
            field_name="payload.max_response_bytes",
            default=max(getattr(transport, "recv_size", 0), _DEFAULT_MAX_RESPONSE_BYTES),
            min_value=1,
            max_value=65536,
        )
        log_raw = _coerce_bool(payload.get("log_raw_telegrams"), field_name="payload.log_raw_telegrams", default=False)
        max_retries = _coerce_int(payload.get("max_retries"), field_name="payload.max_retries", default=0, min_value=0, max_value=5)
        retry_delay_ms = _coerce_int(
            payload.get("retry_delay_ms"),
            field_name="payload.retry_delay_ms",
            default=_DEFAULT_RETRY_DELAY_MS,
            min_value=0,
            max_value=60000,
        )
        reconnect = _coerce_bool(payload.get("reconnect"), field_name="payload.reconnect", default=True)

        def operation() -> DeviceCommandResult:
            session = _MTSicsSession(
                transport,
                log_raw_telegrams=log_raw,
                max_response_bytes=max_response_bytes,
            )
            return self._execute_once(session=session, request=request)

        return self._execute_with_retries(
            operation,
            request=request,
            transport=transport,
            max_retries=max_retries,
            retry_delay_ms=retry_delay_ms,
            reconnect=reconnect,
            is_state_changing=command_name in {"tare", "t", "clear_tare", "tac", "zero", "z"},
        )

    def _execute_with_retries(
        self,
        operation: Callable[[], DeviceCommandResult],
        *,
        request: DeviceCommandRequest,
        transport: ITransport,
        max_retries: int,
        retry_delay_ms: int,
        reconnect: bool,
        is_state_changing: bool,
    ) -> DeviceCommandResult:
        attempts = max_retries + 1
        if is_state_changing and max_retries > 0:
            LOGGER.warning(
                "MT-SICS state-changing command %s is configured with max_retries=%s. "
                "Retries may repeat the operation if the device processed the first request but the response was lost.",
                request.command_name,
                max_retries,
            )
        last_exc: BaseException | None = None
        for attempt_index in range(attempts):
            request.throw_if_interrupted(location="driver.ics435.retry_preflight")
            try:
                return operation()
            except (socket.timeout, OSError, ConnectionError) as exc:
                last_exc = exc
                if attempt_index >= max_retries or not reconnect:
                    raise
                try:
                    transport.close()
                finally:
                    if retry_delay_ms > 0:
                        time.sleep(retry_delay_ms / 1000.0)
        assert last_exc is not None
        raise last_exc

    def _execute_once(self, *, session: _MTSicsSession, request: DeviceCommandRequest) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.ics435.start")
        command_name = str(request.command_name or "").strip().lower()
        payload = request.payload or {}

        if command_name in {"read_weight", "get_weight", "weight", "read_live_telemetry"}:
            weight_command = str(payload.get("weight_command") or "SI").strip().upper()
            if weight_command not in {"S", "SI"}:
                raise DriverValidationError("Field 'payload.weight_command' must be either 'SI' or 'S'.")
            return self._read_weight(session, request=request, command=weight_command)

        if command_name in {"read_stable_weight", "get_stable_weight"}:
            return self._read_weight(session, request=request, command="S")

        if command_name in {"tare", "t"}:
            return self._tare(session, request=request)

        if command_name in {"clear_tare", "tac"}:
            return self._ack_command(session, request=request, command="TAC", metadata_command="clear_tare")

        if command_name in {"zero", "z"}:
            return self._ack_command(session, request=request, command="Z", metadata_command="zero")

        if command_name in {"initialize", "identify", "read_device_info"}:
            return self._read_device_info(session, request=request)

        if command_name in {"list_commands", "supported_commands", "i0"}:
            return self._read_supported_commands(session, request=request)

        if command_name in {"get_serial_number", "i4"}:
            return self._single_identification(session, request=request, command="I4", key="serial_number")

        if command_name in {"get_software_version", "i3"}:
            return self._single_identification(session, request=request, command="I3", key="software")

        if command_name in {"get_device_identification", "i2"}:
            return self._single_identification(session, request=request, command="I2", key="device_identification")

        if command_name in {"get_sics_level", "i1"}:
            return self._single_identification(session, request=request, command="I1", key="sics_level")

        if command_name in {"raw", "send_raw", "manual_text"}:
            return self._raw_command(session, request=request)

        raise DriverValidationError(f"Unsupported ICS435 command '{request.command_name}'.")

    def _read_weight(self, session: _MTSicsSession, *, request: DeviceCommandRequest, command: str) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.ics435.weight_pre_send")
        request_bytes = session.send_command(command)
        request.throw_if_interrupted(location="driver.ics435.weight_pre_receive")
        line = session.read_response_for(command)[0]
        reading = parse_weight_response(line)
        request.throw_if_interrupted(location="driver.ics435.weight_post_receive")
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": command,
            "request_hex": request_bytes.hex(),
            "weight": reading.to_metadata(),
            "value": reading.to_metadata(),
            "skipped_responses": list(session.skipped_lines),
        }
        return DeviceCommandResult(
            acknowledged=True,
            response_text=line,
            response_hex=(line + "\r\n").encode("ascii").hex(),
            metadata=metadata,
        )

    def _tare(self, session: _MTSicsSession, *, request: DeviceCommandRequest) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.ics435.tare_pre_send")
        request_bytes = session.send_command("T")
        line = session.read_response_for("T")[0]
        tare_result = parse_tare_response(line)
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": "T",
            "request_hex": request_bytes.hex(),
            "tare": tare_result.to_metadata(),
            "value": tare_result.to_metadata(),
            "skipped_responses": list(session.skipped_lines),
        }
        return DeviceCommandResult(True, line, (line + "\r\n").encode("ascii").hex(), metadata)

    def _ack_command(
        self,
        session: _MTSicsSession,
        *,
        request: DeviceCommandRequest,
        command: str,
        metadata_command: str,
    ) -> DeviceCommandResult:
        request.throw_if_interrupted(location=f"driver.ics435.{metadata_command}_pre_send")
        request_bytes = session.send_command(command)
        line = session.read_response_for(command)[0]
        ack = parse_ack_response(command, line)
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": command,
            "request_hex": request_bytes.hex(),
            "value": True,
            "ack": ack,
            "skipped_responses": list(session.skipped_lines),
        }
        return DeviceCommandResult(True, line, (line + "\r\n").encode("ascii").hex(), metadata)

    def _read_supported_commands(self, session: _MTSicsSession, *, request: DeviceCommandRequest) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.ics435.i0_pre_send")
        request_bytes = session.send_command("I0")
        lines = session.read_response_for("I0", multiline_until_status_a=True)
        payload = parse_i0_response(lines)
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": "I0",
            "request_hex": request_bytes.hex(),
            "value": payload["supported_commands"],
            **payload,
            "skipped_responses": list(session.skipped_lines),
        }
        response_text = "\n".join(lines)
        return DeviceCommandResult(True, response_text, response_text.encode("ascii").hex(), metadata)

    def _single_identification(
        self,
        session: _MTSicsSession,
        *,
        request: DeviceCommandRequest,
        command: str,
        key: str,
    ) -> DeviceCommandResult:
        request.throw_if_interrupted(location=f"driver.ics435.{command.lower()}_pre_send")
        request_bytes = session.send_command(command)
        line = session.read_response_for(command)[0]
        payload = parse_identification_response(command, line)
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": command,
            "request_hex": request_bytes.hex(),
            key: payload["value"],
            "value": payload["value"],
            "identification": payload,
            "skipped_responses": list(session.skipped_lines),
        }
        return DeviceCommandResult(True, line, (line + "\r\n").encode("ascii").hex(), metadata)

    def _read_device_info(self, session: _MTSicsSession, *, request: DeviceCommandRequest) -> DeviceCommandResult:
        info: dict[str, Any] = {
            "serial_number": None,
            "software": None,
            "device_identification": None,
            "sics_level": None,
            "supported_commands": [],
            "responses": {},
            "errors": {},
        }
        response_lines: list[str] = []
        request_hex: dict[str, str] = {}
        for command, key in (
            ("I4", "serial_number"),
            ("I3", "software"),
            ("I2", "device_identification"),
            ("I1", "sics_level"),
        ):
            request.throw_if_interrupted(location=f"driver.ics435.initialize_{command.lower()}")
            try:
                request_bytes = session.send_command(command)
                request_hex[command] = request_bytes.hex()
                line = session.read_response_for(command)[0]
                parsed = parse_identification_response(command, line)
                response_lines.append(line)
                info[key] = parsed["value"]
                info["responses"][command] = parsed
            except MTSicsError as exc:
                info["errors"][command] = str(exc)
                LOGGER.warning("MT-SICS initialization command %s failed: %s", command, exc)

        request.throw_if_interrupted(location="driver.ics435.initialize_i0")
        try:
            request_bytes = session.send_command("I0")
            request_hex["I0"] = request_bytes.hex()
            lines = session.read_response_for("I0", multiline_until_status_a=True)
            response_lines.extend(lines)
            parsed_i0 = parse_i0_response(lines)
            info["supported_commands"] = parsed_i0["supported_commands"]
            info["responses"]["I0"] = parsed_i0
        except MTSicsError as exc:
            info["errors"]["I0"] = str(exc)
            LOGGER.warning("MT-SICS initialization command I0 failed: %s", exc)

        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": "initialize",
            "request_hex": request_hex,
            "value": info,
            "device_info": info,
            "skipped_responses": list(session.skipped_lines),
        }
        response_text = "\n".join(response_lines)
        return DeviceCommandResult(True, response_text, response_text.encode("ascii").hex(), metadata)

    def _raw_command(self, session: _MTSicsSession, *, request: DeviceCommandRequest) -> DeviceCommandResult:
        payload = request.payload or {}
        command = _coerce_command(payload.get("command", payload.get("text", payload.get("command_text"))), field_name="payload.command")
        max_lines = _coerce_int(payload.get("max_lines"), field_name="payload.max_lines", default=1, min_value=1, max_value=128)
        multiline_until_status_a = _coerce_bool(
            payload.get("multiline_until_status_a"),
            field_name="payload.multiline_until_status_a",
            default=command.strip().upper() == "I0",
        )
        request.throw_if_interrupted(location="driver.ics435.raw_pre_send")
        request_bytes = session.send_command(command)
        lines = session.read_response_for(
            command.split()[0],
            multiline_until_status_a=multiline_until_status_a,
            max_lines=max_lines,
        )
        response_text = "\n".join(lines)
        metadata = {
            "driver": "mettler_toledo_ics435",
            "protocol": "mt_sics",
            "command": command,
            "request_hex": request_bytes.hex(),
            "value": response_text,
            "line_count": len(lines),
            "skipped_responses": list(session.skipped_lines),
        }
        return DeviceCommandResult(True, response_text, response_text.encode("ascii").hex(), metadata)
