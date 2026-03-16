from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import Device, DeviceBindingCurrent, DeviceBindingHistory, DeviceConnection, DeviceServer
from .services import TcpSocketConfig, probe_tcp_socket


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_error(message: str, status_code: int, details: str | None = None):
    payload: dict[str, Any] = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _load_json_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return payload


def _clean_string(value: Any, *, field_name: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"Field '{field_name}' is required.")
        return None

    text = str(value).strip()
    if not text:
        if required:
            raise ValueError(f"Field '{field_name}' must not be empty.")
        return None
    return text


def _parse_bool(value: Any, *, field_name: str) -> bool:
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
    raise ValueError(f"Field '{field_name}' must be a boolean.")


def _parse_int(value: Any, *, field_name: str, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field '{field_name}' must be an integer.") from exc

    if min_value is not None and parsed < min_value:
        raise ValueError(f"Field '{field_name}' must be >= {min_value}.")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"Field '{field_name}' must be <= {max_value}.")
    return parsed


def _parse_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Field '{field_name}' must be an ISO datetime string.") from exc
    raise ValueError(f"Field '{field_name}' must be an ISO datetime string.")


def _validate_choice(value: str | None, *, field_name: str, allowed: set[str], required: bool = False) -> str | None:
    cleaned = _clean_string(value, field_name=field_name, required=required)
    if cleaned is None:
        return None

    normalized = cleaned.lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Field '{field_name}' must be one of: {choices}.")
    return normalized


def _default_nport_tcp_port(port_number: int) -> int:
    return 4000 + port_number


def _get_or_404(model, pk: int, label: str):
    item = db.session.get(model, pk)
    if item is None:
        return None, _json_error(f"{label} with id {pk} was not found.", 404)
    return item, None


def _commit() -> tuple[bool, Any]:
    try:
        db.session.commit()
        return True, None
    except IntegrityError as exc:
        db.session.rollback()
        return False, _json_error("Database constraint violated.", 409, str(exc.orig))


def _binding_to_dict(item: DeviceBindingCurrent | None) -> dict[str, Any] | None:
    if item is None:
        return None

    connection = item.connection
    server = connection.device_server if connection else None
    return {
        "device_id": item.device_id,
        "connection_id": item.connection_id,
        "first_seen_at": _dt(item.first_seen_at),
        "last_seen_at": _dt(item.last_seen_at),
        "is_online": item.is_online,
        "quality_state": item.quality_state,
        "connection": None
        if connection is None
        else {
            "connection_id": connection.connection_id,
            "port_number": connection.port_number,
            "connection_label": connection.connection_label,
            "transport_type": connection.transport_type,
            "tcp_host": connection.tcp_host,
            "tcp_port": connection.tcp_port,
            "device_server": None
            if server is None
            else {
                "device_server_id": server.device_server_id,
                "server_code": server.server_code,
                "display_name": server.display_name,
                "host": server.host,
            },
        },
    }


def _device_to_dict(item: Device) -> dict[str, Any]:
    return {
        "device_id": item.device_id,
        "asset_serial": item.asset_serial,
        "manufacturer_serial": item.manufacturer_serial,
        "display_name": item.display_name,
        "device_type": item.device_type,
        "protocol": item.protocol,
        "firmware_version": item.firmware_version,
        "is_active": item.is_active,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "current_binding": _binding_to_dict(item.current_binding),
    }


def _device_server_to_dict(item: DeviceServer) -> dict[str, Any]:
    return {
        "device_server_id": item.device_server_id,
        "server_code": item.server_code,
        "display_name": item.display_name,
        "vendor": item.vendor,
        "model": item.model,
        "host": item.host,
        "management_port": item.management_port,
        "serial_standard": item.serial_standard,
        "port_count": item.port_count,
        "notes": item.notes,
        "is_active": item.is_active,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "connection_count": len(item.connections),
    }


def _device_connection_to_dict(item: DeviceConnection) -> dict[str, Any]:
    server = item.device_server
    binding = item.current_binding
    device = binding.device if binding else None
    return {
        "connection_id": item.connection_id,
        "device_server_id": item.device_server_id,
        "port_number": item.port_number,
        "connection_label": item.connection_label,
        "transport_type": item.transport_type,
        "tcp_host": item.tcp_host,
        "tcp_port": item.tcp_port,
        "baud_rate": item.baud_rate,
        "data_bits": item.data_bits,
        "parity": item.parity,
        "stop_bits": item.stop_bits,
        "flow_control": item.flow_control,
        "read_timeout_ms": item.read_timeout_ms,
        "write_timeout_ms": item.write_timeout_ms,
        "reconnect_delay_ms": item.reconnect_delay_ms,
        "last_seen_at": _dt(item.last_seen_at),
        "last_error": item.last_error,
        "is_enabled": item.is_enabled,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "device_server": None
        if server is None
        else {
            "device_server_id": server.device_server_id,
            "server_code": server.server_code,
            "display_name": server.display_name,
            "host": server.host,
        },
        "bound_device": None
        if device is None
        else {
            "device_id": device.device_id,
            "asset_serial": device.asset_serial,
            "display_name": device.display_name,
        },
    }


def _apply_device_payload(item: Device, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "asset_serial" in payload:
        item.asset_serial = _clean_string(payload.get("asset_serial"), field_name="asset_serial", required=True)
    if not partial or "display_name" in payload:
        item.display_name = _clean_string(payload.get("display_name"), field_name="display_name", required=True)
    if not partial or "device_type" in payload:
        item.device_type = _clean_string(payload.get("device_type"), field_name="device_type", required=True)
    if not partial or "protocol" in payload:
        item.protocol = _clean_string(payload.get("protocol"), field_name="protocol", required=True)
    if "manufacturer_serial" in payload:
        item.manufacturer_serial = _clean_string(payload.get("manufacturer_serial"), field_name="manufacturer_serial")
    elif not partial:
        item.manufacturer_serial = None
    if "firmware_version" in payload:
        item.firmware_version = _clean_string(payload.get("firmware_version"), field_name="firmware_version")
    elif not partial:
        item.firmware_version = None
    if "is_active" in payload:
        item.is_active = _parse_bool(payload.get("is_active"), field_name="is_active")


def _apply_device_server_payload(item: DeviceServer, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "server_code" in payload:
        item.server_code = _clean_string(payload.get("server_code"), field_name="server_code", required=True)
    if not partial or "display_name" in payload:
        item.display_name = _clean_string(payload.get("display_name"), field_name="display_name", required=True)
    if not partial or "host" in payload:
        item.host = _clean_string(payload.get("host"), field_name="host", required=True)

    if "vendor" in payload:
        item.vendor = _clean_string(payload.get("vendor"), field_name="vendor", required=True)
    elif not partial and not item.vendor:
        item.vendor = "Moxa"

    if "model" in payload:
        item.model = _clean_string(payload.get("model"), field_name="model")
    elif not partial and not item.model:
        item.model = "NPort 5610-8-DT"

    if "management_port" in payload:
        value = payload.get("management_port")
        item.management_port = (
            None
            if value in (None, "")
            else _parse_int(value, field_name="management_port", min_value=1, max_value=65535)
        )
    elif not partial:
        item.management_port = None

    if "serial_standard" in payload:
        item.serial_standard = _validate_choice(
            payload.get("serial_standard"),
            field_name="serial_standard",
            allowed={"rs232", "rs422", "rs485"},
            required=True,
        )
    elif not partial and not item.serial_standard:
        item.serial_standard = "rs232"

    if "port_count" in payload:
        item.port_count = _parse_int(payload.get("port_count"), field_name="port_count", min_value=1)
    elif not partial and not item.port_count:
        item.port_count = 8

    if "notes" in payload:
        item.notes = _clean_string(payload.get("notes"), field_name="notes")
    elif not partial:
        item.notes = None

    if "is_active" in payload:
        item.is_active = _parse_bool(payload.get("is_active"), field_name="is_active")


def _apply_device_connection_payload(item: DeviceConnection, payload: dict[str, Any], *, partial: bool) -> None:
    server = item.device_server

    if not partial or "device_server_id" in payload:
        device_server_id = _parse_int(payload.get("device_server_id"), field_name="device_server_id", min_value=1)
        server = db.session.get(DeviceServer, device_server_id)
        if server is None:
            raise ValueError(f"DeviceServer with id {device_server_id} was not found.")
        item.device_server_id = device_server_id

    if not partial or "port_number" in payload:
        item.port_number = _parse_int(payload.get("port_number"), field_name="port_number", min_value=1)

    if "connection_label" in payload:
        item.connection_label = _clean_string(payload.get("connection_label"), field_name="connection_label")
    elif not partial and item.port_number is not None:
        item.connection_label = f"Port {item.port_number}"

    if "transport_type" in payload:
        item.transport_type = _validate_choice(
            payload.get("transport_type"),
            field_name="transport_type",
            allowed={"tcp_socket", "rfc2217"},
            required=True,
        )
    elif not partial and not item.transport_type:
        item.transport_type = "tcp_socket"

    if "tcp_host" in payload:
        item.tcp_host = _clean_string(payload.get("tcp_host"), field_name="tcp_host", required=True)
    elif not partial or "device_server_id" in payload:
        if server is None:
            raise ValueError("Field 'tcp_host' is required.")
        item.tcp_host = server.host

    if "tcp_port" in payload:
        item.tcp_port = _parse_int(payload.get("tcp_port"), field_name="tcp_port", min_value=1, max_value=65535)
    elif not partial and item.port_number is not None:
        item.tcp_port = _default_nport_tcp_port(item.port_number)
    elif partial and "port_number" in payload and item.port_number is not None:
        item.tcp_port = _default_nport_tcp_port(item.port_number)

    int_fields = {
        "baud_rate": {"min_value": 1},
        "data_bits": {"min_value": 5, "max_value": 8},
        "stop_bits": {"min_value": 1, "max_value": 2},
        "read_timeout_ms": {"min_value": 1},
        "write_timeout_ms": {"min_value": 1},
        "reconnect_delay_ms": {"min_value": 0},
    }
    for field_name, limits in int_fields.items():
        if field_name in payload:
            setattr(item, field_name, _parse_int(payload.get(field_name), field_name=field_name, **limits))

    if "parity" in payload:
        parity = _clean_string(payload.get("parity"), field_name="parity", required=True)
        assert parity is not None
        parity = parity.upper()
        if parity not in {"N", "E", "O"}:
            raise ValueError("Field 'parity' must be one of: N, E, O.")
        item.parity = parity

    if "flow_control" in payload:
        item.flow_control = _validate_choice(
            payload.get("flow_control"),
            field_name="flow_control",
            allowed={"none", "rtscts", "xonxoff"},
            required=True,
        )

    if "last_seen_at" in payload:
        item.last_seen_at = _parse_datetime(payload.get("last_seen_at"), field_name="last_seen_at")
    elif not partial:
        item.last_seen_at = None

    if "last_error" in payload:
        item.last_error = _clean_string(payload.get("last_error"), field_name="last_error")
    elif not partial:
        item.last_error = None

    if "is_enabled" in payload:
        item.is_enabled = _parse_bool(payload.get("is_enabled"), field_name="is_enabled")

    if server is not None and item.port_number is not None and server.port_count is not None and item.port_number > server.port_count:
        raise ValueError(
            f"Field 'port_number' must be <= port_count ({server.port_count}) of device server {server.device_server_id}."
        )


def _close_open_binding_history(device_id: int, connection_id: int, *, reason: str | None = None) -> None:
    history = (
        DeviceBindingHistory.query.filter_by(device_id=device_id, connection_id=connection_id, bound_to=None)
        .order_by(DeviceBindingHistory.binding_history_id.desc())
        .first()
    )
    if history is not None:
        history.bound_to = _now_utc()
        if reason:
            history.reason = reason


@api_bp.get("/")
def api_index():
    return jsonify(
        {
            "resources": {
                "devices": "/api/devices",
                "device_servers": "/api/device-servers",
                "device_connections": "/api/device-connections",
                "device_binding_example": "/api/devices/<device_id>/binding",
                "device_connection_probe": "/api/device-connections/<connection_id>/probe",
            }
        }
    )


@api_bp.get("/devices")
def list_devices():
    items = Device.query.order_by(Device.device_id.asc()).all()
    return jsonify({"items": [_device_to_dict(item) for item in items]})


@api_bp.post("/devices")
def create_device():
    try:
        payload = _load_json_payload()
        item = Device()
        _apply_device_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_to_dict(item)), 201


@api_bp.get("/devices/<int:device_id>")
def get_device(device_id: int):
    item, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response
    return jsonify(_device_to_dict(item))


@api_bp.patch("/devices/<int:device_id>")
def update_device(device_id: int):
    item, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_device_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_to_dict(item))


@api_bp.put("/devices/<int:device_id>/binding")
def bind_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        connection_id = _parse_int(payload.get("connection_id"), field_name="connection_id")
        quality_state = _clean_string(payload.get("quality_state"), field_name="quality_state") or "configured"
        is_online = _parse_bool(payload.get("is_online"), field_name="is_online") if "is_online" in payload else False
        reason = _clean_string(payload.get("reason"), field_name="reason")
    except ValueError as exc:
        return _json_error(str(exc), 400)

    connection = db.session.get(DeviceConnection, connection_id)
    if connection is None:
        return _json_error(f"DeviceConnection with id {connection_id} was not found.", 404)

    occupied = DeviceBindingCurrent.query.filter_by(connection_id=connection_id).one_or_none()
    if occupied is not None and occupied.device_id != device_id:
        return _json_error(f"Connection {connection_id} is already bound to device {occupied.device_id}.", 409)

    current = device.current_binding
    now = _now_utc()
    if current is not None and current.connection_id != connection_id:
        _close_open_binding_history(device.device_id, current.connection_id, reason="rebinding")
        db.session.delete(current)
        db.session.flush()

    current = device.current_binding
    if current is None:
        current = DeviceBindingCurrent(
            device_id=device.device_id,
            connection_id=connection.connection_id,
            first_seen_at=now,
            last_seen_at=now,
            is_online=is_online,
            quality_state=quality_state,
        )
        db.session.add(current)
        db.session.add(
            DeviceBindingHistory(
                device_id=device.device_id,
                connection_id=connection.connection_id,
                bound_from=now,
                reason=reason or "manual_bind",
            )
        )
    else:
        current.connection_id = connection.connection_id
        current.last_seen_at = now
        current.is_online = is_online
        current.quality_state = quality_state

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_to_dict(device))


@api_bp.delete("/devices/<int:device_id>/binding")
def unbind_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    current = device.current_binding
    if current is None:
        return _json_error(f"Device {device_id} has no current binding.", 404)

    _close_open_binding_history(device.device_id, current.connection_id, reason="manual_unbind")
    db.session.delete(current)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.delete("/devices/<int:device_id>")
def delete_device(device_id: int):
    item, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.get("/device-servers")
def list_device_servers():
    items = DeviceServer.query.order_by(DeviceServer.device_server_id.asc()).all()
    return jsonify({"items": [_device_server_to_dict(item) for item in items]})


@api_bp.post("/device-servers")
def create_device_server():
    try:
        payload = _load_json_payload()
        item = DeviceServer()
        _apply_device_server_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_server_to_dict(item)), 201


@api_bp.get("/device-servers/<int:device_server_id>")
def get_device_server(device_server_id: int):
    item, error_response = _get_or_404(DeviceServer, device_server_id, "DeviceServer")
    if error_response:
        return error_response
    return jsonify(_device_server_to_dict(item))


@api_bp.patch("/device-servers/<int:device_server_id>")
def update_device_server(device_server_id: int):
    item, error_response = _get_or_404(DeviceServer, device_server_id, "DeviceServer")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_device_server_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_server_to_dict(item))


@api_bp.delete("/device-servers/<int:device_server_id>")
def delete_device_server(device_server_id: int):
    item, error_response = _get_or_404(DeviceServer, device_server_id, "DeviceServer")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.get("/device-connections")
def list_device_connections():
    items = DeviceConnection.query.order_by(DeviceConnection.connection_id.asc()).all()
    return jsonify({"items": [_device_connection_to_dict(item) for item in items]})


@api_bp.post("/device-connections")
def create_device_connection():
    try:
        payload = _load_json_payload()
        item = DeviceConnection()
        _apply_device_connection_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_connection_to_dict(item)), 201


@api_bp.post("/device-connections/<int:connection_id>/probe")
def probe_device_connection(connection_id: int):
    item, error_response = _get_or_404(DeviceConnection, connection_id, "DeviceConnection")
    if error_response:
        return error_response

    if item.transport_type != "tcp_socket":
        return _json_error(f"Transport type '{item.transport_type}' is not supported for probing.", 400)

    checked_at = _now_utc()
    probe_result = probe_tcp_socket(
        TcpSocketConfig(
            host=item.tcp_host,
            port=item.tcp_port,
            connect_timeout_s=max(item.read_timeout_ms, item.write_timeout_ms, 3000) / 1000,
            read_timeout_s=item.read_timeout_ms / 1000,
            write_timeout_s=item.write_timeout_ms / 1000,
        )
    )

    if probe_result.reachable:
        item.last_seen_at = checked_at
        item.last_error = None
    else:
        item.last_error = probe_result.error

    ok, error_response = _commit()
    if not ok:
        return error_response

    return jsonify(
        {
            "connection": _device_connection_to_dict(item),
            "probe": {
                "checked_at": _dt(checked_at),
                "reachable": probe_result.reachable,
                "latency_ms": probe_result.latency_ms,
                "error": probe_result.error,
            },
        }
    )


@api_bp.get("/device-connections/<int:connection_id>")
def get_device_connection(connection_id: int):
    item, error_response = _get_or_404(DeviceConnection, connection_id, "DeviceConnection")
    if error_response:
        return error_response
    return jsonify(_device_connection_to_dict(item))


@api_bp.patch("/device-connections/<int:connection_id>")
def update_device_connection(connection_id: int):
    item, error_response = _get_or_404(DeviceConnection, connection_id, "DeviceConnection")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_device_connection_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_device_connection_to_dict(item))


@api_bp.delete("/device-connections/<int:connection_id>")
def delete_device_connection(connection_id: int):
    item, error_response = _get_or_404(DeviceConnection, connection_id, "DeviceConnection")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204
