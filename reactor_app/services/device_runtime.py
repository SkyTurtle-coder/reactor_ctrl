from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from ..extensions import db
from ..models import ControlCommand, ControlCommandEvent, Device, Measurement, MeasurementChannel
from .drivers import DeviceCommandRequest, DeviceCommandResult, DriverError, DriverNotFoundError, DriverValidationError, get_driver
from .transports import TcpSocketConfig, TcpSocketTransport


_MEASUREMENT_PARSERS = {"text", "float", "int", "bool"}
_MEASUREMENT_SOURCES = {"poller", "event", "manual", "import"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_optional_int(value: Any, *, field_name: str, min_value: int = 1) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DeviceCommandError(f"Field '{field_name}' must be an integer.", status_code=400) from exc

    if parsed < min_value:
        raise DeviceCommandError(f"Field '{field_name}' must be >= {min_value}.", status_code=400)
    return parsed


@dataclass
class ExecutedDeviceCommand:
    command: ControlCommand
    result: DeviceCommandResult
    measurement: Measurement | None = None


class DeviceCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        command: ControlCommand | None = None,
        details: Any | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.command = command
        self.details = details


def _add_command_event(command: ControlCommand, event_type: str, event_payload: dict[str, Any] | None = None) -> None:
    db.session.add(
        ControlCommandEvent(
            command=command,
            event_type=event_type,
            event_payload=event_payload,
        )
    )


def _mark_connection_success(connection_id: int, *, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_connection "
            "SET last_seen_at=:ts, last_error=NULL, updated_at=:ts "
            "WHERE connection_id=:cid"
        ),
        {"ts": timestamp, "cid": connection_id},
    )


def _mark_connection_failure(connection_id: int, *, message: str, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_connection "
            "SET last_error=:msg, updated_at=:ts "
            "WHERE connection_id=:cid"
        ),
        {"msg": message, "ts": timestamp, "cid": connection_id},
    )


def _mark_binding_online(device_id: int, *, connection_id: int, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_binding_current "
            "SET last_seen_at=:ts, is_online=1 "
            "WHERE device_id=:did AND connection_id=:cid"
        ),
        {"ts": timestamp, "did": device_id, "cid": connection_id},
    )


def _mark_binding_offline(device_id: int, *, connection_id: int) -> None:
    db.session.execute(
        text(
            "UPDATE device_binding_current "
            "SET is_online=0 "
            "WHERE device_id=:did AND connection_id=:cid"
        ),
        {"did": device_id, "cid": connection_id},
    )


def _build_transport_config(connection, payload: dict[str, Any]) -> TcpSocketConfig:
    read_timeout_ms = _parse_optional_int(payload.get("response_timeout_ms"), field_name="response_timeout_ms")
    write_timeout_ms = _parse_optional_int(payload.get("write_timeout_ms"), field_name="write_timeout_ms")
    connect_timeout_ms = _parse_optional_int(payload.get("connect_timeout_ms"), field_name="connect_timeout_ms")
    recv_size = _parse_optional_int(payload.get("recv_size"), field_name="recv_size")

    return TcpSocketConfig(
        host=connection.tcp_host,
        port=connection.tcp_port,
        connect_timeout_s=(connect_timeout_ms or max(connection.read_timeout_ms, connection.write_timeout_ms, 3000)) / 1000,
        read_timeout_s=(read_timeout_ms or connection.read_timeout_ms) / 1000,
        write_timeout_s=(write_timeout_ms or connection.write_timeout_ms) / 1000,
        recv_size=recv_size or 4096,
    )


def _fail_command(command: ControlCommand, *, status: str, message: str, connection, binding) -> None:
    finished_at = _now_utc()
    command.status = status
    command.error_message = message
    command.finished_at = finished_at

    # These runtime telemetry fields can be touched by the recipe and manual
    # reconcilers at the same time. Use a savepoint so a concurrent 1020 error
    # cannot destroy the command record; last writer wins is acceptable for
    # connection health metadata.
    try:
        with db.session.begin_nested():
            _mark_connection_failure(connection.connection_id, message=message, timestamp=finished_at)
            if binding is not None:
                _mark_binding_offline(binding.device_id, connection_id=binding.connection_id)
    except Exception:
        pass
    db.session.expire(connection, ["last_error", "updated_at"])
    if binding is not None:
        db.session.expire(binding, ["is_online"])

    _add_command_event(command, status, {"message": message, "finished_at": finished_at.isoformat()})


def _parse_measurement_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise DriverValidationError("Field 'payload.measurement.measured_at' must be an ISO datetime string.") from exc
    raise DriverValidationError("Field 'payload.measurement.measured_at' must be an ISO datetime string.")


def _parse_measurement_quality_score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError("Field 'payload.measurement.quality_score' must be numeric.") from exc


def _parse_measurement_source(value: Any) -> str:
    source = str(value or "poller").strip().lower()
    if source not in _MEASUREMENT_SOURCES:
        allowed = ", ".join(sorted(_MEASUREMENT_SOURCES))
        raise DriverValidationError(f"Field 'payload.measurement.source' must be one of: {allowed}.")
    return source


def _parse_measurement_parser(value: Any) -> str:
    parser_name = str(value or "text").strip().lower()
    if parser_name not in _MEASUREMENT_PARSERS:
        allowed = ", ".join(sorted(_MEASUREMENT_PARSERS))
        raise DriverValidationError(f"Field 'payload.measurement.parser' must be one of: {allowed}.")
    return parser_name


def _parse_bool_value(raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    truthy = {"true", "1", "yes", "y", "on", "running"}
    falsy = {"false", "0", "no", "n", "off", "stopped"}
    if normalized in truthy:
        return True
    if normalized in falsy:
        return False
    raise DriverValidationError(
        "Field 'payload.measurement.parser=bool' requires a response value like true/false, 1/0, running/stopped."
    )


def _extract_response_value(response_text: str, *, key: str | None) -> str:
    cleaned_response = response_text.strip()
    if not key:
        return cleaned_response

    target_key = key.strip().upper()
    for fragment in cleaned_response.split(";"):
        if "=" not in fragment:
            continue
        current_key, current_value = fragment.split("=", 1)
        if current_key.strip().upper() == target_key:
            return current_value.strip()

    raise DriverValidationError(
        f"Response does not contain key '{key}' required by 'payload.measurement.key'."
    )


def _measurement_value_type(parser_name: str) -> str:
    if parser_name == "text":
        return "text"
    if parser_name == "int":
        return "int"
    if parser_name == "bool":
        return "bool"
    return "float"


def _persist_measurement(
    *,
    device: Device,
    command: ControlCommand,
    payload: dict[str, Any],
    result: DeviceCommandResult,
    finished_at: datetime,
) -> Measurement | None:
    try:
        measurement_config = payload.get("measurement")
        if measurement_config is None:
            return None
        if not isinstance(measurement_config, dict):
            raise DeviceCommandError(
                "Field 'payload.measurement' must be a JSON object.",
                status_code=400,
                command=command,
            )

        channel_code = str(measurement_config.get("channel_code", "")).strip()
        if not channel_code:
            raise DeviceCommandError(
                "Field 'payload.measurement.channel_code' is required.",
                status_code=400,
                command=command,
            )

        parser_name = _parse_measurement_parser(measurement_config.get("parser"))
        source = _parse_measurement_source(measurement_config.get("source"))
        response_text = result.response_text
        if response_text is None:
            raise DeviceCommandError(
                "Measurement persistence requires a text response from the device.",
                status_code=422,
                command=command,
            )

        key = measurement_config.get("key")
        key_text = str(key).strip() if key is not None else None
        raw_value = _extract_response_value(response_text, key=key_text or None)

        numeric_value: float | None = None
        text_value: str | None = None
        if parser_name == "text":
            text_value = raw_value
        elif parser_name == "float":
            try:
                numeric_value = float(raw_value)
            except ValueError as exc:
                raise DeviceCommandError(
                    f"Could not parse measurement value '{raw_value}' as float.",
                    status_code=422,
                    command=command,
                ) from exc
        elif parser_name == "int":
            try:
                numeric_value = float(int(raw_value))
            except ValueError as exc:
                raise DeviceCommandError(
                    f"Could not parse measurement value '{raw_value}' as int.",
                    status_code=422,
                    command=command,
                ) from exc
        else:
            numeric_value = 1.0 if _parse_bool_value(raw_value) else 0.0

        value_type = _measurement_value_type(parser_name)
        display_name = str(measurement_config.get("display_name") or channel_code).strip() or channel_code
        unit = str(measurement_config.get("unit") or "").strip()
        measured_at = _parse_measurement_datetime(measurement_config.get("measured_at")) or finished_at
        quality_score = _parse_measurement_quality_score(measurement_config.get("quality_score"))

        channel = MeasurementChannel.query.filter_by(device_id=device.device_id, channel_code=channel_code).one_or_none()
        if channel is None:
            channel = MeasurementChannel(
                device_id=device.device_id,
                channel_code=channel_code,
                display_name=display_name,
                unit=unit,
                value_type=value_type,
                is_active=True,
            )
            db.session.add(channel)
            db.session.flush()
        else:
            channel.display_name = display_name
            channel.unit = unit
            channel.value_type = value_type
            channel.is_active = True

        measurement = Measurement(
            device_id=device.device_id,
            channel_id=channel.channel_id,
            channel_code=channel.channel_code,
            measured_at=measured_at,
            numeric_value=numeric_value,
            text_value=text_value,
            unit=unit or None,
            quality_score=quality_score,
            raw_payload={
                "command_id": command.command_id,
                "command_name": command.command_name,
                "request_payload": payload,
                "response_text": result.response_text,
                "response_hex": result.response_hex,
                "driver_metadata": result.metadata,
                "measurement": {
                    "parser": parser_name,
                    "key": key_text,
                    "raw_value": raw_value,
                },
            },
            source=source,
        )
        db.session.add(measurement)
        db.session.flush()

        _add_command_event(
            command,
            "measurement_saved",
            {
                "measurement_id": measurement.measurement_id,
                "channel_code": measurement.channel_code,
                "value_type": value_type,
                "numeric_value": measurement.numeric_value,
                "text_value": measurement.text_value,
                "unit": measurement.unit,
                "measured_at": measured_at.isoformat(),
            },
        )
        return measurement
    except DriverValidationError as exc:
        message = str(exc)
        command.error_message = message
        _add_command_event(command, "measurement_failed", {"message": message})
        raise DeviceCommandError(
            "Measurement persistence failed.",
            status_code=422,
            command=command,
            details={"measurement_error": message},
        ) from exc
    except DeviceCommandError as exc:
        command.error_message = str(exc)
        _add_command_event(command, "measurement_failed", {"message": str(exc)})
        raise


def execute_device_command(
    device: Device,
    *,
    command_name: str,
    payload: dict[str, Any],
    requested_by: str,
) -> ExecutedDeviceCommand:
    binding = device.current_binding
    if binding is None:
        raise DeviceCommandError(f"Device {device.device_id} has no current binding.", status_code=409)

    connection = binding.connection
    if connection is None:
        raise DeviceCommandError(f"Device {device.device_id} is bound to an invalid connection.", status_code=409)
    if not connection.is_enabled:
        raise DeviceCommandError(f"Connection {connection.connection_id} is disabled.", status_code=409)
    if connection.transport_type != "tcp_socket":
        raise DeviceCommandError(
            f"Transport type '{connection.transport_type}' is not supported for command execution.",
            status_code=400,
        )

    try:
        driver = get_driver(device.protocol)
    except DriverNotFoundError as exc:
        raise DeviceCommandError(str(exc), status_code=400) from exc
    transport_config = _build_transport_config(connection, payload) if driver.uses_transport else None

    command = ControlCommand(
        device_id=device.device_id,
        request_uuid=str(uuid4()),
        requested_by=requested_by,
        command_name=command_name,
        command_payload=payload,
        status="queued",
    )
    db.session.add(command)
    db.session.flush()
    _add_command_event(command, "queued", {"requested_by": requested_by})

    request = DeviceCommandRequest(command_name=command_name, payload=payload)
    sent_at = _now_utc()

    try:
        if driver.uses_transport:
            assert transport_config is not None
            with TcpSocketTransport(transport_config) as transport:
                command.status = "sent"
                command.sent_at = sent_at
                _add_command_event(command, "sent", {"sent_at": sent_at.isoformat()})
                result = driver.execute(transport=transport, request=request)
        else:
            command.status = "sent"
            command.sent_at = sent_at
            _add_command_event(command, "sent", {"sent_at": sent_at.isoformat()})
            result = driver.execute(transport=None, request=request)
    except socket.timeout as exc:
        _fail_command(command, status="timeout", message=str(exc), connection=connection, binding=binding)
        raise DeviceCommandError("Timed out while waiting for a device response.", status_code=504, command=command) from exc
    except DriverValidationError as exc:
        _fail_command(command, status="failed", message=str(exc), connection=connection, binding=binding)
        raise DeviceCommandError(str(exc), status_code=400, command=command) from exc
    except (OSError, DriverError) as exc:
        _fail_command(command, status="failed", message=str(exc), connection=connection, binding=binding)
        raise DeviceCommandError("Device command execution failed.", status_code=502, command=command) from exc

    finished_at = _now_utc()
    command.status = "acked" if result.acknowledged else "sent"
    command.ack_at = finished_at if result.acknowledged else None
    command.finished_at = finished_at
    command.error_message = None

    # See _fail_command for why these telemetry fields are written outside ORM
    # dirty tracking. The binding update is guarded by connection_id so a stale
    # in-flight command cannot mark a newly rebound device online.
    try:
        with db.session.begin_nested():
            _mark_connection_success(connection.connection_id, timestamp=finished_at)
            _mark_binding_online(binding.device_id, connection_id=binding.connection_id, timestamp=finished_at)
    except Exception:
        pass
    db.session.expire(connection, ["last_seen_at", "last_error", "updated_at"])
    db.session.expire(binding, ["last_seen_at", "is_online"])

    _add_command_event(
        command,
        "response",
        {
            "finished_at": finished_at.isoformat(),
            "response_text": result.response_text,
            "response_hex": result.response_hex,
            "metadata": result.metadata,
        },
    )
    measurement = _persist_measurement(
        device=device,
        command=command,
        payload=payload,
        result=result,
        finished_at=finished_at,
    )

    return ExecutedDeviceCommand(command=command, result=result, measurement=measurement)
