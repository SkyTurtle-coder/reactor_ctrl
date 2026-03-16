from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import Device, Rs485Bus, SerialAdapter, UsbHub


api_bp = Blueprint("api", __name__, url_prefix="/api")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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


def _parse_int(value: Any, *, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field '{field_name}' must be an integer.") from exc


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


def _device_to_dict(item: Device) -> dict[str, Any]:
    binding = item.current_binding
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
        "current_binding": None
        if binding is None
        else {
            "bus_id": binding.bus_id,
            "rs485_address": binding.rs485_address,
            "is_online": binding.is_online,
            "quality_state": binding.quality_state,
            "last_seen_at": _dt(binding.last_seen_at),
        },
    }


def _serial_adapter_to_dict(item: SerialAdapter) -> dict[str, Any]:
    bus = item.bus
    return {
        "adapter_id": item.adapter_id,
        "adapter_uid": item.adapter_uid,
        "hub_id": item.hub_id,
        "adapter_label": item.adapter_label,
        "usb_vendor_id": item.usb_vendor_id,
        "usb_product_id": item.usb_product_id,
        "usb_serial": item.usb_serial,
        "usb_location_path": item.usb_location_path,
        "driver_info": item.driver_info,
        "last_seen_port": item.last_seen_port,
        "last_seen_at": _dt(item.last_seen_at),
        "is_active": item.is_active,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "bus": None
        if bus is None
        else {
            "bus_id": bus.bus_id,
            "bus_name": bus.bus_name,
            "protocol": bus.protocol,
        },
    }


def _bus_to_dict(item: Rs485Bus) -> dict[str, Any]:
    adapter = item.adapter
    return {
        "bus_id": item.bus_id,
        "adapter_id": item.adapter_id,
        "bus_name": item.bus_name,
        "protocol": item.protocol,
        "baud_rate": item.baud_rate,
        "data_bits": item.data_bits,
        "parity": item.parity,
        "stop_bits": item.stop_bits,
        "poll_interval_ms": item.poll_interval_ms,
        "timeout_ms": item.timeout_ms,
        "is_enabled": item.is_enabled,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "adapter": None
        if adapter is None
        else {
            "adapter_id": adapter.adapter_id,
            "adapter_uid": adapter.adapter_uid,
            "last_seen_port": adapter.last_seen_port,
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


def _apply_serial_adapter_payload(item: SerialAdapter, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "adapter_uid" in payload:
        item.adapter_uid = _clean_string(payload.get("adapter_uid"), field_name="adapter_uid", required=True)

    if "hub_id" in payload:
        hub_id = payload.get("hub_id")
        if hub_id in (None, ""):
            item.hub_id = None
        else:
            hub_id = _parse_int(hub_id, field_name="hub_id")
            if db.session.get(UsbHub, hub_id) is None:
                raise ValueError(f"UsbHub with id {hub_id} was not found.")
            item.hub_id = hub_id

    optional_string_fields = (
        "adapter_label",
        "usb_vendor_id",
        "usb_product_id",
        "usb_serial",
        "usb_location_path",
        "driver_info",
        "last_seen_port",
    )
    for field_name in optional_string_fields:
        if field_name in payload:
            setattr(item, field_name, _clean_string(payload.get(field_name), field_name=field_name))
        elif not partial and getattr(item, field_name, None) is None:
            setattr(item, field_name, None)

    if "last_seen_at" in payload:
        item.last_seen_at = _parse_datetime(payload.get("last_seen_at"), field_name="last_seen_at")
    elif not partial:
        item.last_seen_at = None

    if "is_active" in payload:
        item.is_active = _parse_bool(payload.get("is_active"), field_name="is_active")


def _apply_bus_payload(item: Rs485Bus, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "adapter_id" in payload:
        adapter_id = _parse_int(payload.get("adapter_id"), field_name="adapter_id")
        if db.session.get(SerialAdapter, adapter_id) is None:
            raise ValueError(f"SerialAdapter with id {adapter_id} was not found.")
        item.adapter_id = adapter_id

    if not partial or "bus_name" in payload:
        item.bus_name = _clean_string(payload.get("bus_name"), field_name="bus_name", required=True)

    if "protocol" in payload:
        item.protocol = _clean_string(payload.get("protocol"), field_name="protocol", required=True)
    elif not partial and not item.protocol:
        item.protocol = "modbus_rtu"

    int_fields = ("baud_rate", "data_bits", "stop_bits", "poll_interval_ms", "timeout_ms")
    for field_name in int_fields:
        if field_name in payload:
            setattr(item, field_name, _parse_int(payload.get(field_name), field_name=field_name))

    if "parity" in payload:
        parity = _clean_string(payload.get("parity"), field_name="parity", required=True)
        assert parity is not None
        parity = parity.upper()
        if parity not in {"N", "E", "O"}:
            raise ValueError("Field 'parity' must be one of: N, E, O.")
        item.parity = parity

    if "is_enabled" in payload:
        item.is_enabled = _parse_bool(payload.get("is_enabled"), field_name="is_enabled")


@api_bp.get("/")
def api_index():
    return jsonify(
        {
            "resources": {
                "devices": "/api/devices",
                "serial_adapters": "/api/serial-adapters",
                "rs485_buses": "/api/rs485-buses",
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


@api_bp.get("/serial-adapters")
def list_serial_adapters():
    items = SerialAdapter.query.order_by(SerialAdapter.adapter_id.asc()).all()
    return jsonify({"items": [_serial_adapter_to_dict(item) for item in items]})


@api_bp.post("/serial-adapters")
def create_serial_adapter():
    try:
        payload = _load_json_payload()
        item = SerialAdapter()
        _apply_serial_adapter_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_serial_adapter_to_dict(item)), 201


@api_bp.get("/serial-adapters/<int:adapter_id>")
def get_serial_adapter(adapter_id: int):
    item, error_response = _get_or_404(SerialAdapter, adapter_id, "SerialAdapter")
    if error_response:
        return error_response
    return jsonify(_serial_adapter_to_dict(item))


@api_bp.patch("/serial-adapters/<int:adapter_id>")
def update_serial_adapter(adapter_id: int):
    item, error_response = _get_or_404(SerialAdapter, adapter_id, "SerialAdapter")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_serial_adapter_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_serial_adapter_to_dict(item))


@api_bp.delete("/serial-adapters/<int:adapter_id>")
def delete_serial_adapter(adapter_id: int):
    item, error_response = _get_or_404(SerialAdapter, adapter_id, "SerialAdapter")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.get("/rs485-buses")
def list_rs485_buses():
    items = Rs485Bus.query.order_by(Rs485Bus.bus_id.asc()).all()
    return jsonify({"items": [_bus_to_dict(item) for item in items]})


@api_bp.post("/rs485-buses")
def create_rs485_bus():
    try:
        payload = _load_json_payload()
        item = Rs485Bus()
        _apply_bus_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_bus_to_dict(item)), 201


@api_bp.get("/rs485-buses/<int:bus_id>")
def get_rs485_bus(bus_id: int):
    item, error_response = _get_or_404(Rs485Bus, bus_id, "Rs485Bus")
    if error_response:
        return error_response
    return jsonify(_bus_to_dict(item))


@api_bp.patch("/rs485-buses/<int:bus_id>")
def update_rs485_bus(bus_id: int):
    item, error_response = _get_or_404(Rs485Bus, bus_id, "Rs485Bus")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_bus_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_bus_to_dict(item))


@api_bp.delete("/rs485-buses/<int:bus_id>")
def delete_rs485_bus(bus_id: int):
    item, error_response = _get_or_404(Rs485Bus, bus_id, "Rs485Bus")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204
