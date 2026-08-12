from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from .capabilities import DeviceCapability
from ..transports.interface import ITransport


LOGGER = logging.getLogger(__name__)

_DEFAULT_MIN_SETPOINT_C = -40.0
_DEFAULT_MAX_SETPOINT_C = 150.0
_SETPOINT_READBACK_TOLERANCE_C = 0.05
_MISSING_EXTERNAL_SENSOR_TEMP_C = -151.0
_MISSING_EXTERNAL_SENSOR_TOLERANCE_C = 0.005
_DRAIN_IDLE_TIMEOUT_S = 0.08
_MAX_STALE_RESPONSES = 3
_NUMBER_RE = re.compile(r"([+-])?\s*(\d+(?:[.,]\d+)?)")
_STATUS_ACTIVE_VALUES = {1}


@dataclass(frozen=True)
class MinistatCCCommandResponse:
    command: str
    request_bytes: bytes
    response_text: str | None
    response_bytes: bytes


@dataclass
class MinistatCCSetpointResult:
    requested_value: float
    verified_setpoint: float
    setpoint_sync_status: str = "verified"
    attempts: list[dict[str, Any]] = field(default_factory=list)


def _coerce_float(value: Any, *, field_name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise DriverValidationError(f"Field '{field_name}' is required.")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be numeric.") from exc


def _normalize_response_text(text: str | None) -> str:
    return str(text or "").strip()


def _last_numeric_token(text: str | None) -> str:
    raw = _normalize_response_text(text)
    matches = list(_NUMBER_RE.finditer(raw))
    if not matches:
        raise DriverError(f"Ministat CC response contains no numeric value: {raw!r}.")
    match = matches[-1]
    sign = match.group(1) or ""
    number = match.group(2).replace(",", ".")
    return f"{sign}{number}"


def _temperature_from_pp_response(text: str | None) -> float:
    token = _last_numeric_token(text)
    try:
        value = float(token)
    except ValueError as exc:
        raise DriverError(f"Ministat CC temperature response could not be parsed: {text!r}.") from exc

    normalized = token.lstrip("+-")
    if "." not in normalized:
        # Huber PP represents temperatures as signed integer hundredths:
        # +02500 -> 25.00 degC, -01234 -> -12.34 degC.
        value /= 100.0
    return round(value, 4)


def _integer_from_pp_response(text: str | None) -> int:
    token = _last_numeric_token(text)
    try:
        return int(round(float(token.replace(",", "."))))
    except ValueError as exc:
        raise DriverError(f"Ministat CC integer response could not be parsed: {text!r}.") from exc


def _format_pp_temperature(value_celsius: float) -> str:
    hundredths = int(round(float(value_celsius) * 100))
    sign = "+" if hundredths >= 0 else "-"
    return f"{sign}{abs(hundredths):05d}"


def _is_missing_external_sensor_temp(value: float | None) -> bool:
    if value is None:
        return False
    return abs(float(value) - _MISSING_EXTERNAL_SENSOR_TEMP_C) <= _MISSING_EXTERNAL_SENSOR_TOLERANCE_C


def _is_echo_response(response_text: str | None, sent_command: str) -> bool:
    raw = _normalize_response_text(response_text).upper()
    command = str(sent_command or "").strip().upper()
    if not raw or not command:
        return False
    return raw == command or raw == command.rstrip("?")


def _status_payload(raw_status: int) -> dict[str, Any]:
    active = raw_status in _STATUS_ACTIVE_VALUES
    return {
        "raw": raw_status,
        "temperature_control_active": active,
        "circulation_active": active,
        "remote_control_active": None,
        "status_available": True,
    }


class HuberMinistatCCClient:
    """Line-oriented PP client for Huber Ministat/Compatible-Control thermostats."""

    def __init__(self, transport: ITransport, *, encoding: str = "ascii", max_response_bytes: int = 4096):
        self.transport = transport
        self.encoding = encoding
        self.max_response_bytes = int(max_response_bytes)
        self.history: list[MinistatCCCommandResponse] = []

    def connect(self) -> None:
        self.transport.connect()

    def _clear_input_buffer(self) -> bytes:
        drain = getattr(self.transport, "drain_input", None)
        if not callable(drain):
            return b""
        try:
            drained = drain(max_bytes=self.max_response_bytes, idle_timeout_s=_DRAIN_IDLE_TIMEOUT_S)
            if drained:
                LOGGER.debug("Ministat CC drained %d stale input byte(s): %s", len(drained), drained.hex())
            return drained
        except Exception:
            LOGGER.debug("Ministat CC input drain failed; continuing.", exc_info=True)
            return b""

    def send_command(self, command: str, expect_response: bool = True) -> MinistatCCCommandResponse:
        command_text = str(command or "").strip()
        if not command_text:
            raise DriverValidationError("Ministat CC command must not be empty.")

        self.connect()
        self._clear_input_buffer()
        request_bytes = command_text.encode(self.encoding) + b"\r\n"
        LOGGER.debug("Ministat CC send: %r", command_text)
        self.transport.send(request_bytes)

        response_bytes = b""
        response_text: str | None = None
        if expect_response:
            last_response: MinistatCCCommandResponse | None = None
            for _ in range(_MAX_STALE_RESPONSES + 1):
                response_bytes = self.transport.receive_until(b"\n", max_bytes=self.max_response_bytes)
                response_text = response_bytes.decode(self.encoding, errors="replace").strip()
                LOGGER.debug("Ministat CC recv: %r", response_text)
                response = MinistatCCCommandResponse(
                    command=command_text,
                    request_bytes=request_bytes,
                    response_text=response_text,
                    response_bytes=response_bytes,
                )
                self.history.append(response)
                last_response = response
                if not _is_echo_response(response_text, command_text):
                    return response
                LOGGER.debug("Ministat CC skipped echo response for %r.", command_text)
            assert last_response is not None
            return last_response

        response = MinistatCCCommandResponse(
            command=command_text,
            request_bytes=request_bytes,
            response_text=response_text,
            response_bytes=response_bytes,
        )
        self.history.append(response)
        return response

    def read_setpoint(self) -> float:
        return _temperature_from_pp_response(self.send_command("SP?").response_text)

    def read_internal_temperature(self) -> float:
        return _temperature_from_pp_response(self.send_command("TI?").response_text)

    def read_external_temperature(self) -> float:
        return _temperature_from_pp_response(self.send_command("TE?").response_text)

    def read_process_temperature(self) -> float:
        try:
            external = self.read_external_temperature()
            if not _is_missing_external_sensor_temp(external):
                return external
        except (DriverError, OSError, socket.timeout):
            LOGGER.debug("Ministat CC external/process read failed; falling back to internal temperature.", exc_info=True)
        return self.read_internal_temperature()

    def read_status(self) -> dict[str, Any]:
        raw_status = _integer_from_pp_response(self.send_command("CA?").response_text)
        return _status_payload(raw_status)

    def read_error(self) -> str:
        return _normalize_response_text(self.send_command("FSW?").response_text)

    def enable_remote(self) -> bool:
        # PP commands are accepted as master/slave serial commands. The public PP
        # table does not define a separate REMOTE command for Compatible Control.
        return True

    def enable_local(self) -> bool:
        return True

    def set_internal_sensor(self) -> bool:
        response = self.send_command("INTERN@")
        raw = _normalize_response_text(response.response_text).upper()
        if raw and "INTERN" not in raw:
            raise DriverError(f"Ministat CC did not confirm internal sensor selection: {raw!r}.")
        return True

    def set_external_sensor(self) -> bool:
        response = self.send_command("EXTERN@")
        raw = _normalize_response_text(response.response_text).upper()
        if raw and "EXTERN" not in raw:
            raise DriverError(f"Ministat CC did not confirm external sensor selection: {raw!r}.")
        return True

    def write_setpoint(
        self,
        value_celsius: float,
        *,
        min_setpoint_c: float,
        max_setpoint_c: float,
    ) -> MinistatCCSetpointResult:
        requested = round(float(value_celsius), 4)
        if not min_setpoint_c <= requested <= max_setpoint_c:
            raise DriverValidationError(
                f"Setpoint {requested:g} degC is outside configured safety range "
                f"{min_setpoint_c:g}..{max_setpoint_c:g} degC."
            )

        command = f"SP@ {_format_pp_temperature(requested)}"
        response = self.send_command(command)
        verified = _temperature_from_pp_response(response.response_text)
        deviation = round(abs(verified - requested), 4)
        attempt = {
            "command": command,
            "readback_c": verified,
            "deviation_c": deviation,
        }
        if deviation > _SETPOINT_READBACK_TOLERANCE_C:
            raise DriverError(
                f"Ministat CC setpoint readback mismatch: requested {requested:g} degC, "
                f"read back {verified:g} degC (deviation {deviation:g} degC)."
            )
        return MinistatCCSetpointResult(
            requested_value=requested,
            verified_setpoint=verified,
            attempts=[attempt],
        )

    def start(self) -> bool:
        response = self.send_command("CA@ 00001")
        return _integer_from_pp_response(response.response_text) in _STATUS_ACTIVE_VALUES

    def stop(self) -> bool:
        response = self.send_command("CA@ 00000")
        return _integer_from_pp_response(response.response_text) == 0

    def healthcheck(self) -> dict[str, Any]:
        return {
            "error_status": self.read_error(),
            "status": self.read_status(),
            "setpoint_c": self.read_setpoint(),
        }

    def read_live_telemetry(self) -> dict[str, Any]:
        def _safe_read(channel_name: str, reader) -> Any:
            try:
                return reader()
            except (DriverError, OSError, socket.timeout) as exc:
                LOGGER.debug("Ministat CC live telemetry: %s read failed (%s).", channel_name, exc)
                return None

        setpoint = _safe_read("setpoint_C", self.read_setpoint)
        internal_temp = _safe_read("actual_temp_C", self.read_internal_temperature)
        external_temp = _safe_read("external_temp_C", self.read_external_temperature)
        if _is_missing_external_sensor_temp(external_temp):
            external_temp = None
        status = _safe_read("status", self.read_status)

        telemetry = {
            "setpoint_C": None if setpoint is None else float(setpoint),
            "actual_temp_C": None if internal_temp is None else float(internal_temp),
            "external_temp_C": None if external_temp is None else float(external_temp),
        }
        if isinstance(status, dict):
            telemetry["temperature_control_active"] = status.get("temperature_control_active")
            telemetry["circulation_active"] = status.get("circulation_active")
            telemetry["status_raw"] = status.get("raw")

        if not any(telemetry.get(key) is not None for key in ("setpoint_C", "actual_temp_C", "external_temp_C")):
            raise DriverError("Ministat CC returned no valid numeric temperature data.")
        return telemetry


class HuberMinistatCCDriver(DeviceDriver):
    protocol_names = ("huber_ministat_cc",)

    def get_capabilities(self) -> frozenset[str]:
        return frozenset({
            DeviceCapability.CAN_HEAT,
            DeviceCapability.CAN_COOL,
            DeviceCapability.CAN_SET_TEMPERATURE,
            DeviceCapability.CAN_MEASURE_TEMPERATURE,
            DeviceCapability.HAS_FEEDBACK,
            DeviceCapability.SUPPORTS_MANUAL_MODE,
            DeviceCapability.SUPPORTS_RECIPE_MODE,
        })

    def execute(self, *, transport: ITransport, request: DeviceCommandRequest) -> DeviceCommandResult:
        request.throw_if_interrupted(location="driver.huber_ministat_cc.start")
        command_name = str(request.command_name or "").strip().lower()
        payload = request.payload or {}
        client = HuberMinistatCCClient(
            transport,
            max_response_bytes=int(payload.get("max_response_bytes") or max(transport.recv_size, 4096)),
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
            value = client.write_setpoint(temp_c, min_setpoint_c=min_setpoint, max_setpoint_c=max_setpoint)
        elif command_name in {"get_process_temp", "read_temperature", "read_process_temperature"}:
            value = client.read_process_temperature()
        elif command_name in {"get_bath_temp", "read_bath_temperature", "get_internal_temp", "read_internal_temperature"}:
            value = client.read_internal_temperature()
        elif command_name in {"get_external_temp", "read_external_temperature"}:
            value = client.read_external_temperature()
        elif command_name in {"select_internal_sensor", "set_internal_sensor"}:
            value = client.set_internal_sensor()
            return self._result(value, client, active_control_sensor="internal")
        elif command_name in {"select_external_sensor", "set_external_sensor"}:
            value = client.set_external_sensor()
            return self._result(value, client, active_control_sensor="external")
        elif command_name in {"get_error", "read_error", "get_fault_status", "read_fault_status"}:
            value = client.read_error()
        elif command_name in {"read_live_telemetry", "get_live_telemetry"}:
            value = client.read_live_telemetry()
        elif command_name == "healthcheck":
            value = client.healthcheck()
        else:
            raise DriverValidationError(f"Unsupported Ministat CC command '{request.command_name}'.")

        return self._result(value, client)

    def _result(
        self,
        value: Any,
        client: HuberMinistatCCClient,
        *,
        active_control_sensor: str | None = None,
    ) -> DeviceCommandResult:
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

        extra: dict[str, Any] = {}
        if isinstance(value, MinistatCCSetpointResult):
            extra = {
                "verified_setpoint": value.verified_setpoint,
                "setpoint_sync_status": value.setpoint_sync_status,
                "setpoint_attempts": value.attempts,
            }
            value = value.requested_value
        if active_control_sensor is not None:
            extra["active_control_sensor"] = active_control_sensor

        return DeviceCommandResult(
            acknowledged=True,
            response_text="" if last is None else last.response_text,
            response_hex="" if last is None or not last.response_bytes else last.response_bytes.hex(),
            metadata={
                "driver": "huber_ministat_cc",
                "protocol": "compatible_control_pp_rs232",
                "value": value,
                **extra,
                "command_history": history,
                "request_hex": None if last is None else last.request_bytes.hex(),
            },
        )
