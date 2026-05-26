from __future__ import annotations

import socket
import threading
import logging
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect as sa_inspect, text

from ..extensions import db
from ..models import ControlCommand, ControlCommandEvent, Device, DeviceManualState, Measurement, MeasurementChannel
from .drivers import DeviceCommandRequest, DeviceCommandResult, DriverError, DriverNotFoundError, DriverValidationError, get_driver
from .transports import build_transport, TransportTypeNotSupportedError


_MEASUREMENT_PARSERS = {"text", "float", "int", "bool"}
_MEASUREMENT_SOURCES = {"poller", "event", "manual", "import"}
_DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS = 5.0
_DEVICE_COMMAND_LOCKS: dict[int, threading.RLock] = {}
_DEVICE_COMMAND_LOCKS_GUARD = threading.Lock()


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


def describe_device_command_error(exc: DeviceCommandError) -> str:
    command = getattr(exc, "command", None)
    command_name = str(getattr(command, "command_name", "") or "").strip()
    command_id = getattr(command, "command_id", None)
    device_id = getattr(command, "device_id", None)
    detail = str(getattr(command, "error_message", "") or "").strip()
    base_message = str(exc).strip() or "Device command failed."

    parts: list[str] = []
    if command_name:
        parts.append(f"command '{command_name}'")
    if command_id:
        parts.append(f"command_id={command_id}")
    if device_id:
        parts.append(f"device_id={device_id}")

    prefix = "Device command failed"
    if parts:
        prefix = f"{prefix} ({', '.join(parts)})"
    if detail and detail != base_message:
        return f"{prefix}: {detail}"
    return f"{prefix}: {base_message}"


def _db_dialect_name() -> str:
    try:
        bind = db.session.get_bind()
    except Exception:
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


def _local_device_command_lock(device_id: int) -> threading.RLock:
    normalized_device_id = int(device_id)
    with _DEVICE_COMMAND_LOCKS_GUARD:
        lock = _DEVICE_COMMAND_LOCKS.get(normalized_device_id)
        if lock is None:
            lock = threading.RLock()
            _DEVICE_COMMAND_LOCKS[normalized_device_id] = lock
        return lock


@contextmanager
def _device_command_lock(device_id: int, *, timeout_s: float = _DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS):
    normalized_device_id = int(device_id)
    timeout_seconds = max(1, int(round(float(timeout_s))))
    dialect_name = _db_dialect_name()

    if dialect_name in {"mysql", "mariadb"}:
        lock_name = f"reactor_ctrl:device_command:{normalized_device_id}"
        with db.engine.connect() as connection:
            result = connection.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout_s)"),
                {"lock_name": lock_name, "timeout_s": timeout_seconds},
            ).scalar()
            if result != 1:
                raise DeviceCommandError(
                    f"Device {normalized_device_id} is busy executing another command.",
                    status_code=409,
                )
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Failed to release device command lock for device %s.",
                        normalized_device_id,
                        exc_info=True,
                    )
        return

    lock = _local_device_command_lock(normalized_device_id)
    acquired = lock.acquire(timeout=timeout_seconds)
    if not acquired:
        raise DeviceCommandError(
            f"Device {normalized_device_id} is busy executing another command.",
            status_code=409,
        )
    try:
        yield
    finally:
        lock.release()


@contextmanager
def device_command_sequence_lock(device_id: int, *, timeout_s: float = _DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS):
    with _device_command_lock(device_id, timeout_s=timeout_s):
        yield


def _add_command_event(command: ControlCommand, event_type: str, event_payload: dict[str, Any] | None = None) -> None:
    logger = logging.getLogger(__name__)
    try:
        command_state = sa_inspect(command)
        if command_state.detached:
            raise RuntimeError("Cannot add command event: ControlCommand is detached from the active session.")
        if command_state.transient:
            db.session.add(command)

        db.session.flush([command])
        command_id = command.command_id
        if command_id is None:
            raise RuntimeError("Cannot add command event: ControlCommand has no command_id after flush.")

        event = ControlCommandEvent(
            command=command,
            command_id=command_id,
            event_type=event_type,
            event_payload=event_payload,
        )
        db.session.add(event)
        db.session.flush([event])
    except Exception as exc:
        request_uuid = getattr(command, "request_uuid", None)
        logger.exception(
            "Failed to add ControlCommandEvent(command_id=%s, request_uuid=%s): %s",
            getattr(command, "command_id", None),
            request_uuid,
            exc,
        )
        raise


def _commit_command_phase(command: ControlCommand, phase: str) -> None:
    try:
        db.session.commit()
    except Exception as exc:
        command_id = getattr(command, "command_id", None)
        request_uuid = getattr(command, "request_uuid", None)
        try:
            db.session.rollback()
        except Exception:
            pass
        logging.getLogger(__name__).exception(
            "Failed to commit device command phase %s (command_id=%s, request_uuid=%s).",
            phase,
            command_id,
            request_uuid,
        )
        raise DeviceCommandError(
            f"Device command log persistence failed during {phase}.",
            status_code=500,
            command=command,
        ) from exc


def _safe_expire(item: Any, attribute_names: list[str]) -> None:
    try:
        db.session.expire(item, attribute_names)
    except Exception:
        pass


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


def _sensor_value_from_command(command_name: str, result: DeviceCommandResult) -> str | None:
    normalized = str(command_name or "").strip().lower()
    if normalized in {"select_internal_sensor", "set_internal_sensor"}:
        return "internal"
    if normalized in {"select_external_sensor", "set_external_sensor"}:
        return "external"
    value = result.metadata.get("active_control_sensor") if isinstance(result.metadata, dict) else None
    normalized_value = str(value or "").strip().lower()
    return normalized_value if normalized_value in {"internal", "external"} else None


def _record_active_control_sensor(device_id: int, sensor: str) -> None:
    normalized_sensor = str(sensor or "").strip().lower()
    if normalized_sensor not in {"internal", "external"}:
        return
    try:
        if _db_dialect_name() in {"mysql", "mariadb"}:
            db.session.execute(
                text(
                    "INSERT INTO device_manual_state "
                    "(device_id, desired_version, applied_version, queue_status, active_control_sensor) "
                    "VALUES (:did, 0, 0, 'idle', :sensor) "
                    "ON DUPLICATE KEY UPDATE active_control_sensor=VALUES(active_control_sensor)"
                ),
                {"did": int(device_id), "sensor": normalized_sensor},
            )
        else:
            state = db.session.get(DeviceManualState, int(device_id))
            if state is None:
                state = DeviceManualState(
                    device_id=int(device_id),
                    desired_version=0,
                    applied_version=0,
                    queue_status="idle",
                )
                db.session.add(state)
            state.active_control_sensor = normalized_sensor
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        logging.getLogger(__name__).warning(
            "Failed to store active control sensor '%s' for device %s.",
            normalized_sensor,
            device_id,
            exc_info=True,
        )



def _fail_command(
    command: ControlCommand,
    *,
    status: str,
    message: str,
    connection_id: int,
    binding_device_id: int | None,
    binding_connection_id: int | None,
) -> None:
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
            with db.session.no_autoflush:
                _mark_connection_failure(connection_id, message=message, timestamp=finished_at)
                if binding_device_id is not None and binding_connection_id is not None:
                    _mark_binding_offline(binding_device_id, connection_id=binding_connection_id)
    except Exception:
        pass

    _add_command_event(command, status, {"message": message, "finished_at": finished_at.isoformat()})
    _commit_command_phase(command, status)


def _fail_command_without_connection_health(command: ControlCommand, *, status: str, message: str) -> None:
    finished_at = _now_utc()
    command.status = status
    command.error_message = message
    command.finished_at = finished_at
    _add_command_event(command, status, {"message": message, "finished_at": finished_at.isoformat()})
    _commit_command_phase(command, status)


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
    acquire_lock: bool = True,
) -> ExecutedDeviceCommand:
    binding = device.current_binding
    if binding is None:
        raise DeviceCommandError(f"Device {device.device_id} has no current binding.", status_code=409)

    connection = binding.connection
    if connection is None:
        raise DeviceCommandError(f"Device {device.device_id} is bound to an invalid connection.", status_code=409)
    if not connection.is_enabled:
        raise DeviceCommandError(f"Connection {connection.connection_id} is disabled.", status_code=409)

    try:
        driver = get_driver(device.protocol)
    except DriverNotFoundError as exc:
        raise DeviceCommandError(str(exc), status_code=400) from exc

    transport_obj = None
    if driver.uses_transport:
        try:
            transport_obj = build_transport(connection, payload)
        except TransportTypeNotSupportedError as exc:
            raise DeviceCommandError(str(exc), status_code=400) from exc
    elif str(connection.transport_type or "tcp_socket").strip().lower() not in {"tcp_socket"}:
        raise DeviceCommandError(
            f"Transport type '{connection.transport_type}' is not supported for command execution.",
            status_code=400,
        )
    connection_id = int(connection.connection_id)
    binding_device_id = int(binding.device_id) if binding.device_id is not None else None
    binding_connection_id = int(binding.connection_id) if binding.connection_id is not None else None

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
    _commit_command_phase(command, "queued")

    # For CC230 set_setpoint: inject the remembered write variant so the driver
    # tries the most-recently successful mode first instead of always starting from A.
    effective_payload = payload
    if (
        str(command_name or "").strip().lower() in {"set_setpoint", "set_temperature", "write_setpoint"}
        and str(getattr(device, "protocol", "") or "").strip().lower() == "huber_cc230"
        and "cc230_write_mode" not in payload
    ):
        stored_mode = getattr(connection, "cc230_setpoint_write_mode", None)
        if stored_mode is not None:
            effective_payload = {**payload, "cc230_write_mode": int(stored_mode)}

    request = DeviceCommandRequest(command_name=command_name, payload=effective_payload)
    sent_at = _now_utc()

    try:
        lock_context = _device_command_lock(device.device_id) if acquire_lock else nullcontext()
        with lock_context:
            if driver.uses_transport:
                assert transport_obj is not None
                with transport_obj:
                    command.status = "sent"
                    command.sent_at = sent_at
                    _add_command_event(command, "sent", {"sent_at": sent_at.isoformat()})
                    _commit_command_phase(command, "sent")
                    result = driver.execute(transport=transport_obj, request=request)
            else:
                command.status = "sent"
                command.sent_at = sent_at
                _add_command_event(command, "sent", {"sent_at": sent_at.isoformat()})
                _commit_command_phase(command, "sent")
                result = driver.execute(transport=None, request=request)
    except DeviceCommandError as exc:
        _fail_command_without_connection_health(command, status="failed", message=str(exc))
        raise DeviceCommandError(str(exc), status_code=exc.status_code, command=command, details=exc.details) from exc
    except socket.timeout as exc:
        _fail_command(
            command,
            status="timeout",
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError("Timed out while waiting for a device response.", status_code=504, command=command) from exc
    except DriverValidationError as exc:
        _fail_command(
            command,
            status="failed",
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError(str(exc), status_code=400, command=command) from exc
    except (OSError, DriverError) as exc:
        _fail_command(
            command,
            status="failed",
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
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
            with db.session.no_autoflush:
                _mark_connection_success(connection_id, timestamp=finished_at)
                if binding_device_id is not None and binding_connection_id is not None:
                    _mark_binding_online(binding_device_id, connection_id=binding_connection_id, timestamp=finished_at)
    except Exception:
        pass
    _safe_expire(connection, ["last_seen_at", "last_error", "updated_at"])
    _safe_expire(binding, ["last_seen_at", "is_online"])

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
    _commit_command_phase(command, "response")

    active_control_sensor = _sensor_value_from_command(command_name, result)
    if active_control_sensor is not None:
        _record_active_control_sensor(device.device_id, active_control_sensor)

    try:
        measurement = _persist_measurement(
            device=device,
            command=command,
            payload=payload,
            result=result,
            finished_at=finished_at,
        )
        if measurement is not None:
            _commit_command_phase(command, "measurement")
    except DeviceCommandError:
        _commit_command_phase(command, "measurement_failed")
        raise

    # For CC230 set_setpoint: persist the write mode that worked so the next call
    # can try it first.  Non-fatal: a failure here must not break the command response.
    if (
        str(command_name or "").strip().lower() in {"set_setpoint", "set_temperature", "write_setpoint"}
        and str(getattr(device, "protocol", "") or "").strip().lower() == "huber_cc230"
    ):
        write_mode_used = result.metadata.get("write_mode_used")
        if write_mode_used is not None:
            stored_mode = getattr(connection, "cc230_setpoint_write_mode", None)
            if stored_mode != int(write_mode_used):
                try:
                    db.session.execute(
                        text(
                            "UPDATE device_connection "
                            "SET cc230_setpoint_write_mode=:mode "
                            "WHERE connection_id=:cid"
                        ),
                        {"mode": int(write_mode_used), "cid": connection_id},
                    )
                    db.session.commit()
                    _safe_expire(connection, ["cc230_setpoint_write_mode"])
                except Exception:
                    logging.getLogger(__name__).warning(
                        "CC230: failed to persist setpoint write mode for connection %s.",
                        connection_id,
                        exc_info=True,
                    )

    return ExecutedDeviceCommand(command=command, result=result, measurement=measurement)
