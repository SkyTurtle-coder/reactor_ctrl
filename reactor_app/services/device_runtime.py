from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..extensions import db
from ..models import ControlCommand, ControlCommandEvent, Device
from .drivers import DeviceCommandRequest, DeviceCommandResult, DriverError, DriverNotFoundError, DriverValidationError, get_driver
from .transports import TcpSocketConfig, TcpSocketTransport


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

    connection.last_error = message
    if binding is not None:
        binding.is_online = False

    _add_command_event(command, status, {"message": message, "finished_at": finished_at.isoformat()})


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
    transport_config = _build_transport_config(connection, payload)

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
        with TcpSocketTransport(transport_config) as transport:
            command.status = "sent"
            command.sent_at = sent_at
            _add_command_event(command, "sent", {"sent_at": sent_at.isoformat()})
            result = driver.execute(transport=transport, request=request)
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

    connection.last_seen_at = finished_at
    connection.last_error = None
    binding.last_seen_at = finished_at
    binding.is_online = True

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

    return ExecutedDeviceCommand(command=command, result=result)
