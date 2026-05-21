from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from ..transports import TcpSocketTransport


LOGGER = logging.getLogger(__name__)

_DEFAULT_MIN_SETPOINT_C = -40.0
_DEFAULT_MAX_SETPOINT_C = 150.0
_TEMPERATURE_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
_STATUS_ON_TOKENS = {"1", "ON", "RUN", "RUNNING", "START", "STARTED", "REMOTE"}
_STATUS_OFF_TOKENS = {"0", "OFF", "STOP", "STOPPED", "LOCAL"}


@dataclass(frozen=True)
class CC230CommandResponse:
    command: str
    request_bytes: bytes
    response_text: str | None
    response_bytes: bytes


def _coerce_float(value: Any, *, field_name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise DriverValidationError(f"Field '{field_name}' is required.")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be numeric.") from exc


def _temperature_from_response(text: str | None) -> float:
    raw = str(text or "").strip()
    if not raw:
        raise DriverError("CC230 returned an empty temperature response.")

    matches = _TEMPERATURE_RE.findall(raw)
    if not matches:
        raise DriverError(f"CC230 temperature response contains no numeric value: {raw!r}.")

    token = matches[-1].replace(",", ".")
    try:
        value = float(token)
    except ValueError as exc:
        raise DriverError(f"CC230 temperature response could not be parsed: {raw!r}.") from exc

    if abs(value) > 300:
        value /= 100.0
    return round(value, 4)


def _format_setpoint_celsius(value_celsius: float) -> str:
    return f"{float(value_celsius):+.2f}"


def _format_legacy_set_command(value_celsius: float) -> str:
    scaled = int(round(abs(float(value_celsius)) * 100))
    sign = "-" if float(value_celsius) < 0 else "+"
    return f"SET {sign}{scaled:05d}"


def _status_payload(text: str | None) -> dict[str, Any]:
    raw = str(text or "").strip()
    tokens = {token.strip().upper() for token in re.split(r"[^A-Za-z0-9+-]+", raw) if token.strip()}
    is_on = None
    if tokens & _STATUS_ON_TOKENS:
        is_on = True
    if tokens & _STATUS_OFF_TOKENS:
        is_on = False

    return {
        "raw": raw,
        "temperature_control_active": is_on,
        "circulation_active": is_on,
        "remote_control_active": "REMOTE" in tokens,
    }


def _unknown_status_payload(error: Exception | None = None) -> dict[str, Any]:
    payload = _status_payload(None)
    payload["status_available"] = False
    if error is not None:
        payload["communication_error"] = str(error)
    return payload


class HuberCC230Client:
    """Line-oriented RS-232 client for the older Huber/Polystat CC230."""

    def __init__(self, transport: TcpSocketTransport, *, encoding: str = "ascii", max_response_bytes: int = 4096):
        self.transport = transport
        self.encoding = encoding
        self.max_response_bytes = int(max_response_bytes)
        self.history: list[CC230CommandResponse] = []

    def connect(self) -> None:
        self.transport.connect()

    def disconnect(self, *, safe: bool = False) -> None:
        if safe:
            for command in ("STOP", "LOCAL"):
                try:
                    self.send_command(command, expect_response=False)
                except Exception:
                    LOGGER.warning("CC230 safe disconnect command %s failed.", command, exc_info=True)
        self.transport.close()

    def _clear_input_buffer(self) -> bytes:
        drain = getattr(self.transport, "drain_input", None)
        if not callable(drain):
            return b""
        try:
            drained = drain(max_bytes=self.max_response_bytes, idle_timeout_s=0.02)
            if drained:
                LOGGER.debug("CC230 drained stale input bytes: %s", drained.hex())
            return drained
        except Exception:
            LOGGER.debug("CC230 input drain failed; continuing with command send.", exc_info=True)
            return b""

    def send_command(self, command: str, expect_response: bool = True) -> CC230CommandResponse:
        command_text = str(command or "").strip()
        if not command_text:
            raise DriverValidationError("CC230 command must not be empty.")

        self.connect()
        self._clear_input_buffer()
        request_bytes = command_text.encode(self.encoding) + b"\r\n"
        LOGGER.debug("CC230 send: %r", command_text)
        self.transport.send(request_bytes)

        response_bytes = b""
        response_text: str | None = None
        if expect_response:
            response_bytes = self.transport.receive_until(b"\n", max_bytes=self.max_response_bytes)
            response_text = response_bytes.decode(self.encoding, errors="replace").strip()
            LOGGER.debug("CC230 recv: %r", response_text)

        response = CC230CommandResponse(
            command=command_text,
            request_bytes=request_bytes,
            response_text=response_text,
            response_bytes=response_bytes,
        )
        self.history.append(response)
        return response

    def _read_temperature_with_fallback(self, primary_command: str, fallback_command: str | None = None) -> float:
        primary_error: Exception | None = None
        try:
            response = self.send_command(primary_command)
            return _temperature_from_response(response.response_text)
        except (DriverError, OSError, socket.timeout) as exc:
            primary_error = exc

        if not fallback_command:
            assert primary_error is not None
            raise primary_error

        try:
            response = self.send_command(fallback_command)
            return _temperature_from_response(response.response_text)
        except Exception as fallback_error:
            raise DriverError(
                f"CC230 command {primary_command!r} failed and fallback {fallback_command!r} also failed: "
                f"{fallback_error}"
            ) from fallback_error

    def enable_remote(self) -> bool:
        self.send_command("REMOTE", expect_response=False)
        return True

    def enable_local(self) -> bool:
        self.send_command("LOCAL", expect_response=False)
        return True

    def start(self) -> bool:
        self.enable_remote()
        self.send_command("START", expect_response=False)
        return True

    def stop(self) -> bool:
        self.send_command("STOP", expect_response=False)
        return True

    def read_status(self) -> dict[str, Any]:
        try:
            response = self.send_command("STATUS?")
            payload = _status_payload(response.response_text)
            payload["status_available"] = True
            return payload
        except (DriverError, OSError, socket.timeout) as exc:
            LOGGER.info("CC230 STATUS? did not return a usable response; reporting status as unavailable.")
            return _unknown_status_payload(exc)

    def read_setpoint(self) -> float:
        return self._read_temperature_with_fallback("SETPOINT?", "SP?")

    def write_setpoint(self, value_celsius: float, *, min_setpoint_c: float, max_setpoint_c: float) -> float:
        value = float(value_celsius)
        if not min_setpoint_c <= value <= max_setpoint_c:
            raise DriverValidationError(
                f"Setpoint {value:g} degC is outside configured safety range "
                f"{min_setpoint_c:g}..{max_setpoint_c:g} degC."
            )

        primary_command = f"SETPOINT! {_format_setpoint_celsius(value)}"
        try:
            response = self.send_command(primary_command)
            return _temperature_from_response(response.response_text)
        except (DriverError, OSError, socket.timeout):
            self.send_command(_format_legacy_set_command(value), expect_response=False)
            return round(value, 4)

    def read_process_temperature(self) -> float:
        return self._read_temperature_with_fallback("TEMP?", "TI?")

    def read_bath_temperature(self) -> float:
        return self._read_temperature_with_fallback("BATH?")

    def read_internal_temperature(self) -> float:
        return self._read_temperature_with_fallback("TI?")

    def read_external_temperature(self) -> float:
        return self._read_temperature_with_fallback("TE?")

    def set_internal_sensor(self) -> bool:
        self.enable_remote()
        self.send_command("INTERN!", expect_response=False)
        return True

    def set_external_sensor(self) -> bool:
        self.enable_remote()
        self.send_command("EXTERN!", expect_response=False)
        return True

    def read_error(self) -> str:
        try:
            return str(self.send_command("ERROR?").response_text or "").strip()
        except (OSError, socket.timeout) as exc:
            LOGGER.info("CC230 ERROR? did not return a usable response; ignoring optional error readout.")
            return ""

    def read_warning(self) -> str:
        try:
            return str(self.send_command("WARN?").response_text or "").strip()
        except (OSError, socket.timeout) as exc:
            LOGGER.info("CC230 WARN? did not return a usable response; ignoring optional warning readout.")
            return ""

    def healthcheck(self) -> dict[str, Any]:
        self.enable_remote()
        return {
            "remote": True,
            "status": self.read_status(),
            "setpoint_c": self.read_setpoint(),
        }


class HuberCC230Driver(DeviceDriver):
    protocol_names = ("huber_cc230",)

    def execute(self, *, transport: TcpSocketTransport, request: DeviceCommandRequest) -> DeviceCommandResult:
        command_name = str(request.command_name or "").strip().lower()
        payload = request.payload or {}
        client = HuberCC230Client(
            transport,
            max_response_bytes=int(payload.get("max_response_bytes") or max(transport.config.recv_size, 4096)),
        )

        min_setpoint = _coerce_float(
            payload.get("min_setpoint_c"),
            field_name="payload.min_setpoint_c",
            default=_DEFAULT_MIN_SETPOINT_C,
        )
        max_setpoint = _coerce_float(
            payload.get("max_setpoint_c"),
            field_name="payload.max_setpoint_c",
            default=_DEFAULT_MAX_SETPOINT_C,
        )
        if min_setpoint >= max_setpoint:
            raise DriverValidationError("Field 'payload.min_setpoint_c' must be lower than 'payload.max_setpoint_c'.")

        if command_name == "manual_text":
            text = payload.get("text", payload.get("command_text"))
            expect_response = bool(payload.get("expect_response", str(text or "").strip().endswith("?")))
            response = client.send_command(str(text or ""), expect_response=expect_response)
            return self._result(response.response_text, client)

        if command_name in {"enable_remote", "remote"}:
            value = client.enable_remote()
        elif command_name in {"enable_local", "local"}:
            value = client.enable_local()
        elif command_name in {"start", "start_device", "start_control"}:
            value = client.start()
        elif command_name in {"stop", "stop_device", "stop_control"}:
            value = client.stop()
        elif command_name in {"get_status", "read_status"}:
            value = client.read_status()
        elif command_name in {"get_setpoint", "read_setpoint"}:
            value = client.read_setpoint()
        elif command_name in {"set_setpoint", "set_temperature", "write_setpoint"}:
            temp_c = _coerce_float(payload.get("temp_c", payload.get("temperature_c")), field_name="payload.temp_c")
            value = client.write_setpoint(
                temp_c,
                min_setpoint_c=min_setpoint,
                max_setpoint_c=max_setpoint,
            )
        elif command_name in {"get_process_temp", "read_temperature", "read_process_temperature"}:
            value = client.read_process_temperature()
        elif command_name in {"get_bath_temp", "read_bath_temperature"}:
            value = client.read_bath_temperature()
        elif command_name in {"get_internal_temp", "read_internal_temperature"}:
            value = client.read_internal_temperature()
        elif command_name in {"get_external_temp", "read_external_temperature"}:
            value = client.read_external_temperature()
        elif command_name in {"select_internal_sensor", "set_internal_sensor"}:
            value = client.set_internal_sensor()
        elif command_name in {"select_external_sensor", "set_external_sensor"}:
            value = client.set_external_sensor()
        elif command_name in {"get_error", "read_error"}:
            value = client.read_error()
        elif command_name in {"get_warning", "read_warning"}:
            value = client.read_warning()
        elif command_name == "healthcheck":
            value = client.healthcheck()
        else:
            raise DriverValidationError(f"Unsupported CC230 command '{request.command_name}'.")

        return self._result(value, client)

    def _result(self, value: Any, client: HuberCC230Client) -> DeviceCommandResult:
        last = client.history[-1] if client.history else None
        history = [
            {
                "command": item.command,
                "request_hex": item.request_bytes.hex(),
                "response_text": item.response_text,
                "response_hex": item.response_bytes.hex() if item.response_bytes else None,
            }
            for item in client.history
        ]
        return DeviceCommandResult(
            acknowledged=True,
            response_text=None if last is None else last.response_text,
            response_hex=None if last is None or not last.response_bytes else last.response_bytes.hex(),
            metadata={
                "driver": "huber_cc230",
                "protocol": "cc230_ascii_rs232",
                "value": value,
                "command_history": history,
                "request_hex": None if last is None else last.request_bytes.hex(),
            },
        )
