from __future__ import annotations

import logging
import re
import socket
from typing import Any

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverValidationError
from ..transports import TcpSocketTransport


LOGGER = logging.getLogger(__name__)

_HEX_2_RE = re.compile(r"^[0-9A-Fa-f]{2}$")
_HEX_4_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
_NOT_AVAILABLE_VALUE = "7FFF"
_DEFAULT_MIN_SETPOINT_C = -40.0
_DEFAULT_MAX_SETPOINT_C = 150.0
_MAX_STALE_PB_RESPONSES = 3
_PB_NAMES = {
    "setpoint": "00",
    "internal_temp": "01",
    "return_temp": "02",
    "pump_pressure": "03",
    "error": "05",
    "warning": "06",
    "process_temp": "07",
    "status": "0A",
    "temperature_control_active": "14",
    "circulation_active": "16",
}
_READ_COMMANDS = {
    "get_setpoint": ("00", "temperature_c"),
    "get_internal_temp": ("01", "temperature_c"),
    "get_return_temp": ("02", "temperature_c"),
    "get_pump_pressure": ("03", "raw_u16"),
    "get_process_temp": ("07", "temperature_c"),
    "get_error": ("05", "i16"),
    "get_warning": ("06", "i16"),
}


def _normalize_addr(addr: Any) -> str:
    if isinstance(addr, int):
        if not 0 <= addr <= 0xFF:
            raise DriverValidationError("PB address must be between 0x00 and 0xFF.")
        return f"{addr:02X}"

    normalized = str(addr or "").strip()
    if normalized.lower().startswith("0x"):
        normalized = normalized[2:]
    if normalized.lower() in _PB_NAMES:
        normalized = _PB_NAMES[normalized.lower()]
    normalized = normalized.upper()
    if not _HEX_2_RE.fullmatch(normalized):
        raise DriverValidationError("PB address must be a two-digit hex value.")
    return normalized


def _normalize_value16(value: Any) -> str:
    if isinstance(value, int):
        try:
            return HuberUnistatTCP.encode_i16(value)
        except ValueError as exc:
            raise DriverValidationError("PB value must fit into 16 bits.") from exc
    normalized = str(value or "").strip()
    if normalized.lower().startswith("0x"):
        normalized = normalized[2:]
    normalized = normalized.upper()
    if not _HEX_4_RE.fullmatch(normalized):
        raise DriverValidationError("PB value must be a four-digit hex value.")
    return normalized


def _pb_response_address(response_text: str) -> str | None:
    response = str(response_text or "").strip()
    if not response.startswith("{S") or len(response) < 4:
        return None
    addr = response[2:4].upper()
    return addr if _HEX_2_RE.fullmatch(addr) else None


def _is_address_mismatch_response(response_text: str, addr_hex: str) -> bool:
    response_addr = _pb_response_address(response_text)
    return response_addr is not None and response_addr != _normalize_addr(addr_hex)


def _split_response_lines(response_bytes: bytes) -> list[bytes]:
    lines = [line for line in response_bytes.splitlines(keepends=True) if line.strip()]
    return lines or [response_bytes]


def _coerce_float(value: Any, *, field_name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise DriverValidationError(f"Field '{field_name}' is required.")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be numeric.") from exc


def _coerce_int(value: Any, *, field_name: str, default: int | None = None) -> int:
    if value in (None, ""):
        if default is None:
            raise DriverValidationError(f"Field '{field_name}' is required.")
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError(f"Field '{field_name}' must be an integer.") from exc


class HuberUnistatTCP:
    """Small TCP/PB client for Huber Unistat/Pilot ONE thermostats."""

    def __init__(
        self,
        host: str,
        port: int = 8101,
        timeout: float = 1.5,
        *,
        min_setpoint_c: float = _DEFAULT_MIN_SETPOINT_C,
        max_setpoint_c: float = _DEFAULT_MAX_SETPOINT_C,
    ):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.min_setpoint_c = float(min_setpoint_c)
        self.max_setpoint_c = float(max_setpoint_c)
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @staticmethod
    def encode_i16(value: int) -> str:
        value = int(value)
        if value < -0x8000 or value > 0xFFFF:
            raise ValueError("16-bit value outside encodable range.")
        if value < 0:
            value = (1 << 16) + value
        return f"{value & 0xFFFF:04X}"

    @staticmethod
    def decode_i16(hexstr: str) -> int:
        normalized = _normalize_value16(hexstr)
        value = int(normalized, 16)
        if value & 0x8000:
            value -= 0x10000
        return value

    @classmethod
    def encode_temp(cls, temp_c: float) -> str:
        return cls.encode_i16(round(float(temp_c) * 100))

    @classmethod
    def decode_temp(cls, hexstr: str) -> float:
        return cls.decode_i16(hexstr) / 100.0

    @staticmethod
    def status_bits(raw_status: int) -> dict[str, bool]:
        return {
            "temperature_control_active": bool(raw_status & (1 << 0)),
            "circulation_active": bool(raw_status & (1 << 1)),
            "pump_on": bool(raw_status & (1 << 4)),
            "error": bool(raw_status & (1 << 8)),
            "warning": bool(raw_status & (1 << 9)),
        }

    @staticmethod
    def build_request(addr_hex: str, value_hex: str) -> str:
        return f"{{M{_normalize_addr(addr_hex)}{_normalize_value16(value_hex) if value_hex != '****' else value_hex}\r\n"

    @staticmethod
    def validate_response(response_text: str, addr_hex: str) -> str:
        response = str(response_text or "").strip()
        if not response.startswith("{S"):
            raise DriverError(f"Invalid Huber PB response: {response!r}.")
        if len(response) < 8:
            raise DriverError(f"Incomplete Huber PB response: {response!r}.")

        expected_addr = _normalize_addr(addr_hex)
        response_addr = response[2:4].upper()
        value_hex = response[4:8].upper()
        if response_addr != expected_addr:
            raise DriverError(f"Huber PB address mismatch: sent {expected_addr}, got {response_addr}.")
        if not _HEX_4_RE.fullmatch(value_hex):
            raise DriverError(f"Huber PB value is not a 16-bit hex value: {value_hex!r}.")
        if value_hex == _NOT_AVAILABLE_VALUE:
            raise DriverError(f"Huber PB address {expected_addr} is not available or not unlocked.")
        return value_hex

    def _send_raw(self, cmd: str) -> str:
        if self.sock is None:
            raise ConnectionError("Huber thermostat is not connected.")
        LOGGER.debug("Huber PB send: %r", cmd)
        self.sock.sendall(cmd.encode("ascii"))

        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("Connection closed by Huber thermostat.")
            data.extend(chunk)

        response = bytes(data).decode("ascii", errors="replace")
        LOGGER.debug("Huber PB recv: %r", response)
        return response

    def _request(self, addr_hex: str, value_hex: str) -> str:
        addr = _normalize_addr(addr_hex)
        value = value_hex if value_hex == "****" else _normalize_value16(value_hex)
        response = self._send_raw(self.build_request(addr, value))
        return self.validate_response(response, addr)

    def read_var(self, addr_hex: str | int) -> str:
        return self._request(_normalize_addr(addr_hex), "****")

    def write_var(self, addr_hex: str | int, value16: str | int) -> str:
        return self._request(_normalize_addr(addr_hex), _normalize_value16(value16))

    def get_setpoint(self) -> float:
        return self.decode_temp(self.read_var("00"))

    def set_setpoint(self, temp_c: float) -> float:
        temp = float(temp_c)
        if not self.min_setpoint_c <= temp <= self.max_setpoint_c:
            raise ValueError("Setpoint outside configured safety range.")
        return self.decode_temp(self.write_var("00", self.encode_temp(temp)))

    def get_internal_temp(self) -> float:
        return self.decode_temp(self.read_var("01"))

    def get_process_temp(self) -> float:
        return self.decode_temp(self.read_var("07"))

    def start(self) -> bool:
        status = self.get_status()
        error = self.get_error()
        warning = self.get_warning()
        if error != 0:
            raise RuntimeError(f"Huber thermostat reports error {error}; start is blocked.")
        LOGGER.info("Huber start preflight status=%s warning=%s", status, warning)
        return self.write_var("14", "0001") == "0001"

    def stop(self) -> bool:
        return self.write_var("14", "0000") == "0000"

    def get_status_raw(self) -> int:
        return int(self.read_var("0A"), 16)

    def get_status(self) -> dict[str, bool]:
        return self.status_bits(self.get_status_raw())

    def get_error(self) -> int:
        return self.decode_i16(self.read_var("05"))

    def clear_error(self) -> int:
        return self.decode_i16(self.write_var("05", "0001"))

    def get_warning(self) -> int:
        return self.decode_i16(self.read_var("06"))

    def clear_warning(self) -> int:
        return self.decode_i16(self.write_var("06", "0001"))

    def __enter__(self) -> "HuberUnistatTCP":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class _TransportHuberClient:
    def __init__(self, transport: TcpSocketTransport):
        self.transport = transport

    def request(self, addr_hex: str, value_hex: str) -> tuple[str, bytes, bytes]:
        addr = _normalize_addr(addr_hex)
        value = value_hex if value_hex == "****" else _normalize_value16(value_hex)
        request_text = HuberUnistatTCP.build_request(addr, value)
        request_bytes = request_text.encode("ascii")
        LOGGER.debug("Huber PB send: %r", request_text)
        self.transport.send(request_bytes)
        stale_responses: list[str] = []
        last_mismatch: DriverError | None = None

        for _ in range(_MAX_STALE_PB_RESPONSES + 1):
            response_bytes = self.transport.receive_until(b"\n", max_bytes=max(self.transport.config.recv_size, 64))
            for response_line in _split_response_lines(response_bytes):
                response_text = response_line.decode("ascii", errors="replace")
                LOGGER.debug("Huber PB recv: %r", response_text)
                try:
                    value_response = HuberUnistatTCP.validate_response(response_text, addr)
                except DriverError as exc:
                    if _is_address_mismatch_response(response_text, addr):
                        stale_responses.append(response_text.strip())
                        last_mismatch = exc
                        LOGGER.warning(
                            "Skipping stale Huber PB response while waiting for address %s: %r",
                            addr,
                            response_text,
                        )
                        continue
                    raise
                return value_response, request_bytes, response_line

        if last_mismatch is not None:
            skipped = "; ".join(stale_responses)
            raise DriverError(f"{last_mismatch} Skipped stale response(s): {skipped}.") from last_mismatch
        raise DriverError(f"Huber PB did not return a response for address {addr}.")

    def read_var(self, addr_hex: str) -> tuple[str, bytes, bytes]:
        return self.request(addr_hex, "****")

    def write_var(self, addr_hex: str, value_hex: str) -> tuple[str, bytes, bytes]:
        return self.request(addr_hex, value_hex)


class HuberUnistatDriver(DeviceDriver):
    protocol_names = ("huber_unistat_430", "huber_pilot_one")

    def execute(self, *, transport: TcpSocketTransport, request: DeviceCommandRequest) -> DeviceCommandResult:
        command_name = str(request.command_name or "").strip().lower()
        payload = request.payload or {}
        client = _TransportHuberClient(transport)

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

        metadata: dict[str, Any] = {"driver": "huber_unistat_430", "protocol": "pilot_one_pb"}

        if command_name in {"enable_remote", "remote"}:
            return self._metadata_only_result(
                True,
                {
                    **metadata,
                    "command": "enable_remote",
                    "note": "Pilot ONE/PB TCP control is already remote-capable; no separate REMOTE command is required.",
                },
            )

        if command_name in {"enable_local", "local"}:
            return self._metadata_only_result(
                True,
                {
                    **metadata,
                    "command": "enable_local",
                    "note": "Pilot ONE/PB TCP local-mode handover is not exposed by this protocol mapping.",
                },
            )

        if command_name in {"select_internal_sensor", "set_internal_sensor"}:
            return self._metadata_only_result(
                "internal",
                {
                    **metadata,
                    "command": "select_internal_sensor",
                    "active_control_sensor": "internal",
                    "note": "No dedicated Pilot ONE/PB sensor-select command is configured; internal is treated as the safe default.",
                },
            )

        if command_name in {"select_external_sensor", "set_external_sensor", "read_active_sensor"}:
            raise DriverValidationError(
                "Huber Unistat 430 sensor selection is not mapped for this protocol. "
                "Use internal control or add the device-specific PB address before selecting external control."
            )

        if command_name in {"read_var", "read_pb"}:
            addr = _normalize_addr(payload.get("addr", payload.get("address")))
            value_hex, request_bytes, response_bytes = client.read_var(addr)
            value = self._decode_payload_value(payload, value_hex)
            metadata.update({"addr": addr, "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(value, value_hex, response_bytes, metadata)

        if command_name in {"write_var", "write_pb"}:
            addr = _normalize_addr(payload.get("addr", payload.get("address")))
            value_hex = _normalize_value16(payload.get("value16", payload.get("value_hex", payload.get("value"))))
            value_response, request_bytes, response_bytes = client.write_var(addr, value_hex)
            metadata.update({"addr": addr, "value_hex": value_response, "request_hex": request_bytes.hex()})
            return self._result(value_response, value_response, response_bytes, metadata)

        if command_name in _READ_COMMANDS:
            addr, decoder = _READ_COMMANDS[command_name]
            value_hex, request_bytes, response_bytes = client.read_var(addr)
            value = self._decode_named_value(decoder, value_hex)
            metadata.update({"addr": addr, "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(value, value_hex, response_bytes, metadata)

        if command_name == "set_setpoint":
            temp_c = _coerce_float(payload.get("temp_c", payload.get("temperature_c")), field_name="payload.temp_c")
            if not min_setpoint <= temp_c <= max_setpoint:
                raise DriverValidationError(
                    f"Setpoint {temp_c:g} °C is outside configured safety range "
                    f"{min_setpoint:g}..{max_setpoint:g} °C."
                )
            request_value = HuberUnistatTCP.encode_temp(temp_c)
            value_hex, request_bytes, response_bytes = client.write_var("00", request_value)
            confirmed = HuberUnistatTCP.decode_temp(value_hex)
            metadata.update({"addr": "00", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(confirmed, value_hex, response_bytes, metadata)

        if command_name == "start":
            preflight = self._read_start_preflight(client)
            if preflight["error"] != 0:
                raise DriverError(f"Huber thermostat reports error {preflight['error']}; start is blocked.")
            value_hex, request_bytes, response_bytes = client.write_var("14", "0001")
            metadata.update({"addr": "14", "value_hex": value_hex, "request_hex": request_bytes.hex(), "preflight": preflight})
            return self._result(value_hex == "0001", value_hex, response_bytes, metadata)

        if command_name == "stop":
            value_hex, request_bytes, response_bytes = client.write_var("14", "0000")
            metadata.update({"addr": "14", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(value_hex == "0000", value_hex, response_bytes, metadata)

        if command_name == "set_circulation":
            active = _coerce_int(payload.get("active"), field_name="payload.active")
            if active not in (0, 1):
                raise DriverValidationError("Field 'payload.active' must be 0 or 1.")
            value_hex, request_bytes, response_bytes = client.write_var("16", f"{active:04X}")
            metadata.update({"addr": "16", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(value_hex == f"{active:04X}", value_hex, response_bytes, metadata)

        if command_name == "get_status":
            value_hex, request_bytes, response_bytes = client.read_var("0A")
            raw_status = int(value_hex, 16)
            value = {"raw": raw_status, **HuberUnistatTCP.status_bits(raw_status)}
            metadata.update({"addr": "0A", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(value, value_hex, response_bytes, metadata)

        if command_name == "clear_error":
            value_hex, request_bytes, response_bytes = client.write_var("05", "0001")
            metadata.update({"addr": "05", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(HuberUnistatTCP.decode_i16(value_hex), value_hex, response_bytes, metadata)

        if command_name == "clear_warning":
            value_hex, request_bytes, response_bytes = client.write_var("06", "0001")
            metadata.update({"addr": "06", "value_hex": value_hex, "request_hex": request_bytes.hex()})
            return self._result(HuberUnistatTCP.decode_i16(value_hex), value_hex, response_bytes, metadata)

        raise DriverValidationError(f"Unsupported Huber command '{request.command_name}'.")

    def _metadata_only_result(self, value: Any, metadata: dict[str, Any]) -> DeviceCommandResult:
        metadata = {**metadata, "value": value}
        return DeviceCommandResult(
            acknowledged=True,
            response_text="",
            response_hex="",
            metadata=metadata,
        )

    def _read_start_preflight(self, client: _TransportHuberClient) -> dict[str, Any]:
        status_hex, _, _ = client.read_var("0A")
        error_hex, _, _ = client.read_var("05")
        warning_hex, _, _ = client.read_var("06")
        status_raw = int(status_hex, 16)
        return {
            "status_raw": status_raw,
            "status": HuberUnistatTCP.status_bits(status_raw),
            "error": HuberUnistatTCP.decode_i16(error_hex),
            "warning": HuberUnistatTCP.decode_i16(warning_hex),
        }

    def _decode_payload_value(self, payload: dict[str, Any], value_hex: str) -> Any:
        decoder = str(payload.get("decode_as") or "hex").strip().lower()
        return self._decode_named_value(decoder, value_hex)

    def _decode_named_value(self, decoder: str, value_hex: str) -> Any:
        if decoder in {"hex", "raw_hex"}:
            return value_hex
        if decoder in {"i16", "signed", "signed_i16"}:
            return HuberUnistatTCP.decode_i16(value_hex)
        if decoder in {"u16", "raw_u16", "unsigned"}:
            return int(value_hex, 16)
        if decoder in {"temp", "temperature", "temperature_c"}:
            return HuberUnistatTCP.decode_temp(value_hex)
        raise DriverValidationError("Field 'payload.decode_as' must be one of: hex, i16, u16, temperature_c.")

    def _result(
        self,
        value: Any,
        value_hex: str,
        response_bytes: bytes,
        metadata: dict[str, Any],
    ) -> DeviceCommandResult:
        metadata = {**metadata, "value": value}
        return DeviceCommandResult(
            acknowledged=True,
            response_text=response_bytes.decode("ascii", errors="replace").rstrip("\r\n"),
            response_hex=response_bytes.hex(),
            metadata=metadata,
        )
