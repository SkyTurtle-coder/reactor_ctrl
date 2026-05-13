from __future__ import annotations

import hmac
import re
from datetime import date, datetime, timezone
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .builder_auth import PROCESS_MANUAL_WRITE_SCOPE, REACTOR_BUILDER_WRITE_SCOPE, RECIPE_WRITE_SCOPE, verify_scoped_token
from .actuator_profiles import get_default_profile_id, normalize_control_definition
from .device_limits import max_rpm_for_protocol
from .extensions import db
from .flowsheet_library import build_symbol_index, load_flowsheet_library
from .models import (
    ControlCommand,
    ControlCommandEvent,
    Device,
    DeviceBindingCurrent,
    DeviceBindingHistory,
    DeviceConnection,
    DeviceManualState,
    DeviceServer,
    Measurement,
    ReactorBuild,
    Recipe,
    RecipeProgramState,
)
from .services import (
    DeviceCommandError,
    TcpSocketConfig,
    ensure_manual_state_snapshot,
    execute_device_command,
    list_supported_protocol_options,
    list_supported_protocols,
    manual_state_to_dict,
    probe_tcp_socket,
    queue_manual_state_update,
    recipe_program_state_to_dict,
    start_recipe_program,
    stop_recipe_program,
    wait_for_manual_state_refresh,
)
from .services.activity_log import activity_log_item_to_dict, load_activity_logs, summarize_activity_logs
from .services.measurement_plot import load_device_plot_series


api_bp = Blueprint("api", __name__, url_prefix="/api")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_REACTOR_BUILD_CANVAS_DIMENSION = 10000
_MAX_REACTOR_BUILD_NODES = 400
_MAX_REACTOR_BUILD_EDGES = 800
_MAX_REACTOR_BUILD_ANCHORS_PER_NODE = 64
_MAX_REACTOR_BUILD_ROUTE_POINTS = 64
_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_REQUESTED_BY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_PROCESS_MANUAL_ALLOWED_COMMANDS = {
    "manual_text",
    "get_setpoint",
    "set_setpoint",
    "get_internal_temp",
    "get_process_temp",
    "get_status",
    "detect_protocol",
    "start",
    "stop",
}
_PROCESS_MANUAL_ALLOWED_PAYLOAD_FIELDS = {
    "text",
    "command_text",
    "encoding",
    "line_ending",
    "response_terminator",
    "expect_response",
    "strip_response",
    "max_response_bytes",
    "response_timeout_ms",
    "write_timeout_ms",
    "connect_timeout_ms",
    "recv_size",
}
_PROCESS_MANUAL_ALLOWED_DRIVER_PAYLOAD_FIELDS = {
    "temp_c",
    "temperature_c",
    "min_setpoint_c",
    "max_setpoint_c",
    "line_ending",
    "max_retries",
    "protocol_variant",
    "cc230_protocol",
    "response_timeout_ms",
    "write_timeout_ms",
    "connect_timeout_ms",
    "recv_size",
}
_PROCESS_MANUAL_MAX_TEXT_LENGTH = 160


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_error(message: str, status_code: int, details: str | None = None):
    payload: dict[str, Any] = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _json_auth_error(message: str, status_code: int):
    response = jsonify({"error": message})
    response.status_code = status_code
    response.headers["WWW-Authenticate"] = f'Bearer realm="{current_app.config.get("API_AUTH_REALM", "reactor_ctrl")}"'
    return response


def _extract_api_token() -> str | None:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    token = request.headers.get("X-API-Token")
    if token is not None:
        token = token.strip()
    return token or None


def _extract_builder_write_token() -> str | None:
    token = request.headers.get("X-Reactor-Builder-Token")
    if token is None:
        return None
    token = token.strip()
    return token or None


def _extract_process_manual_token() -> str | None:
    token = request.headers.get("X-Process-Manual-Token")
    if token is None:
        return None
    token = token.strip()
    return token or None


def _extract_recipe_write_token() -> str | None:
    token = request.headers.get("X-Recipe-Token")
    if token is None:
        return None
    token = token.strip()
    return token or None


def _is_builder_write_request() -> bool:
    if request.method not in {"POST", "PATCH"}:
        return False
    path = request.path.rstrip("/")
    return path == "/api/reactor-builds" or path.startswith("/api/reactor-builds/")


def _is_process_manual_request() -> bool:
    if request.method != "POST":
        return False
    path = request.path.rstrip("/")
    return re.fullmatch(r"/api/(devices/\d+/(commands|manual-state)|process-program/(start|stop))", path) is not None


def _is_recipe_write_request() -> bool:
    if request.method not in {"POST", "PATCH", "DELETE"}:
        return False
    path = request.path.rstrip("/")
    return path == "/api/recipes" or path.startswith("/api/recipes/")


@api_bp.before_request
def require_api_token_for_writes():
    if request.method in _SAFE_METHODS:
        return None

    if not current_app.config.get("API_AUTH_REQUIRED", True):
        return None

    builder_token = _extract_builder_write_token() if _is_builder_write_request() else None
    manual_token = _extract_process_manual_token() if _is_process_manual_request() else None
    recipe_token = _extract_recipe_write_token() if _is_recipe_write_request() else None
    secret_key = current_app.config.get("SECRET_KEY")
    if builder_token and secret_key:
        if verify_scoped_token(
            builder_token,
            secret_key=secret_key,
            expected_scope=REACTOR_BUILDER_WRITE_SCOPE,
        ):
            return None
    if manual_token and secret_key:
        if verify_scoped_token(
            manual_token,
            secret_key=secret_key,
            expected_scope=PROCESS_MANUAL_WRITE_SCOPE,
        ):
            return None
    if recipe_token and secret_key:
        if verify_scoped_token(
            recipe_token,
            secret_key=secret_key,
            expected_scope=RECIPE_WRITE_SCOPE,
        ):
            return None

    expected_token = current_app.config.get("API_AUTH_TOKEN")
    if not expected_token:
        if builder_token is not None:
            return _json_auth_error(
                "Invalid or expired Reactor Builder token. Reload the builder page and try again.",
                401,
            )
        if manual_token is not None:
            return _json_auth_error(
                "Invalid or expired Process Manual token. Reload the process page and try again.",
                401,
            )
        if recipe_token is not None:
            return _json_auth_error(
                "Invalid or expired Recipe token. Reload the recipes page and try again.",
                401,
            )
        return _json_auth_error(
            "API write authentication is enabled but no API_AUTH_TOKEN is configured on the server.",
            503,
        )

    provided_token = _extract_api_token()
    if provided_token is not None and hmac.compare_digest(provided_token, expected_token):
        return None

    if builder_token is not None:
        return _json_auth_error("Invalid or expired Reactor Builder token. Reload the builder page and try again.", 401)
    if manual_token is not None:
        return _json_auth_error("Invalid or expired Process Manual token. Reload the process page and try again.", 401)
    if recipe_token is not None:
        return _json_auth_error("Invalid or expired Recipe token. Reload the recipes page and try again.", 401)

    if provided_token is None:
        return _json_auth_error("Missing API authentication token.", 401)

    return _json_auth_error("Invalid API authentication token.", 401)


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


def _parse_query_bool(name: str, *, default: bool) -> bool:
    if name not in request.args:
        return default
    return _parse_bool(request.args.get(name), field_name=name)


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


def _parse_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Field '{field_name}' must be an ISO date string (YYYY-MM-DD).") from exc
    raise ValueError(f"Field '{field_name}' must be an ISO date string (YYYY-MM-DD).")


def _validate_choice(value: str | None, *, field_name: str, allowed: set[str], required: bool = False) -> str | None:
    cleaned = _clean_string(value, field_name=field_name, required=required)
    if cleaned is None:
        return None

    normalized = cleaned.lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Field '{field_name}' must be one of: {choices}.")
    return normalized


def _parse_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field '{field_name}' must be a number.") from exc


def _normalize_requested_by(value: Any, *, default: str) -> str:
    cleaned = _clean_string(value, field_name="requested_by") or default
    if not _REQUESTED_BY_PATTERN.fullmatch(cleaned):
        raise ValueError(
            "Field 'requested_by' must contain only letters, numbers, '.', '_', ':' or '-' and no spaces."
        )
    return cleaned


def _validate_process_manual_command_payload(command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized_command = str(command_name or "").strip().lower()
    if normalized_command not in _PROCESS_MANUAL_ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(_PROCESS_MANUAL_ALLOWED_COMMANDS))
        raise ValueError(f"Process manual control may only execute these command names: {allowed}.")

    if normalized_command != "manual_text":
        unexpected_fields = sorted(set(payload) - _PROCESS_MANUAL_ALLOWED_DRIVER_PAYLOAD_FIELDS)
        if unexpected_fields:
            field_list = ", ".join(unexpected_fields)
            raise ValueError(f"Process manual payload contains unsupported fields: {field_list}.")

        sanitized = dict(payload)
        for field_name in ("temp_c", "temperature_c", "min_setpoint_c", "max_setpoint_c"):
            if field_name in sanitized:
                sanitized[field_name] = _parse_float(sanitized[field_name], field_name=f"payload.{field_name}")
        for field_name in ("line_ending", "protocol_variant", "cc230_protocol"):
            if field_name in sanitized:
                sanitized[field_name] = _clean_string(sanitized[field_name], field_name=f"payload.{field_name}")
                if sanitized[field_name] is not None and len(str(sanitized[field_name])) > 32:
                    raise ValueError(f"Field 'payload.{field_name}' must not exceed 32 characters.")

        bounded_int_fields = {
            "max_retries": (0, 5),
            "response_timeout_ms": (100, 60000),
            "write_timeout_ms": (100, 60000),
            "connect_timeout_ms": (100, 60000),
            "recv_size": (1, 65536),
        }
        for field_name, bounds in bounded_int_fields.items():
            if field_name in sanitized:
                sanitized[field_name] = _parse_int(
                    sanitized[field_name],
                    field_name=f"payload.{field_name}",
                    min_value=bounds[0],
                    max_value=bounds[1],
                )
        return sanitized

    unexpected_fields = sorted(set(payload) - _PROCESS_MANUAL_ALLOWED_PAYLOAD_FIELDS)
    if unexpected_fields:
        field_list = ", ".join(unexpected_fields)
        raise ValueError(f"Process manual payload contains unsupported fields: {field_list}.")

    command_text = _clean_string(payload.get("text", payload.get("command_text")), field_name="payload.text", required=True)
    assert command_text is not None
    if len(command_text) > _PROCESS_MANUAL_MAX_TEXT_LENGTH:
        raise ValueError(
            f"Field 'payload.text' must not exceed {_PROCESS_MANUAL_MAX_TEXT_LENGTH} characters for manual control."
        )

    sanitized = dict(payload)
    sanitized.pop("command_text", None)
    sanitized["text"] = command_text

    for field_name in ("encoding", "line_ending", "response_terminator"):
        if field_name in sanitized:
            cleaned = _clean_string(sanitized.get(field_name), field_name=f"payload.{field_name}")
            if cleaned is None:
                sanitized.pop(field_name, None)
            else:
                sanitized[field_name] = cleaned

    for field_name in ("expect_response", "strip_response"):
        if field_name in sanitized:
            sanitized[field_name] = _parse_bool(sanitized[field_name], field_name=f"payload.{field_name}")

    bounded_int_fields = {
        "max_response_bytes": (1, 65536),
        "response_timeout_ms": (100, 60000),
        "write_timeout_ms": (100, 60000),
        "connect_timeout_ms": (100, 60000),
        "recv_size": (1, 65536),
    }
    for field_name, bounds in bounded_int_fields.items():
        if field_name in sanitized:
            sanitized[field_name] = _parse_int(
                sanitized[field_name],
                field_name=f"payload.{field_name}",
                min_value=bounds[0],
                max_value=bounds[1],
            )

    return sanitized


def _builder_symbol_lookup() -> dict[str, dict[str, Any]]:
    symbols = load_flowsheet_library(
        static_folder=current_app.static_folder,
        static_url_path=current_app.static_url_path,
    )
    return build_symbol_index(symbols)


def _validate_instance_id(value: str, *, field_name: str) -> str:
    if not _INSTANCE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"Field '{field_name}' must contain only letters, numbers, '.', '_' or '-' and no spaces."
        )
    return value


def _anchor_point_for_validation(node: dict[str, Any], anchor_id: str | None) -> dict[str, float]:
    anchors = node.get("anchors", [])
    if isinstance(anchors, list) and anchors:
        anchor = None
        if anchor_id is not None:
            anchor = next((item for item in anchors if item.get("id") == anchor_id), None)
        if anchor is None:
            anchor = anchors[0]
        return {
            "x": round(float(node["x"]) + float(node["width"]) * float(anchor["x_ratio"]), 2),
            "y": round(float(node["y"]) + float(node["height"]) * float(anchor["y_ratio"]), 2),
        }

    return {
        "x": round(float(node["x"]) + float(node["width"]) / 2, 2),
        "y": round(float(node["y"]) + float(node["height"]) / 2, 2),
    }


def _validate_orthogonal_route_points(
    edge_id: str,
    route_points: list[dict[str, float]],
    *,
    source_point: dict[str, float],
    target_point: dict[str, float],
) -> None:
    if not route_points:
        return

    points = [source_point, *route_points, target_point]
    for index in range(1, len(points)):
        previous = points[index - 1]
        current = points[index]
        same_x = previous["x"] == current["x"]
        same_y = previous["y"] == current["y"]
        if same_x and same_y:
            raise ValueError(f"Edge '{edge_id}' contains a zero-length routed segment.")
        if not same_x and not same_y:
            raise ValueError(f"Edge '{edge_id}' must keep an orthogonal route with 90 degree segments only.")


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
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Database commit failed.")
        return False, _json_error("Database operation failed.", 500)


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


def _control_command_event_to_dict(item: ControlCommandEvent) -> dict[str, Any]:
    return {
        "command_event_id": item.command_event_id,
        "command_id": item.command_id,
        "event_type": item.event_type,
        "event_payload": item.event_payload,
        "created_at": _dt(item.created_at),
    }


def _control_command_to_dict(item: ControlCommand, *, include_events: bool = False) -> dict[str, Any]:
    payload = {
        "command_id": item.command_id,
        "device_id": item.device_id,
        "request_uuid": item.request_uuid,
        "requested_by": item.requested_by,
        "command_name": item.command_name,
        "command_payload": item.command_payload,
        "status": item.status,
        "requested_at": _dt(item.requested_at),
        "scheduled_for": _dt(item.scheduled_for),
        "sent_at": _dt(item.sent_at),
        "ack_at": _dt(item.ack_at),
        "finished_at": _dt(item.finished_at),
        "retry_count": item.retry_count,
        "error_message": item.error_message,
    }
    if include_events:
        payload["events"] = [
            _control_command_event_to_dict(event)
            for event in sorted(item.events, key=lambda current_event: current_event.command_event_id)
        ]
    return payload


def _device_manual_state_to_dict(item: DeviceManualState | None) -> dict[str, Any] | None:
    return manual_state_to_dict(item)


def _measurement_to_dict(item: Measurement) -> dict[str, Any]:
    quality_score = None if item.quality_score is None else float(item.quality_score)
    return {
        "measurement_id": item.measurement_id,
        "device_id": item.device_id,
        "channel_id": item.channel_id,
        "channel_code": item.channel_code,
        "measured_at": _dt(item.measured_at),
        "ingested_at": _dt(item.ingested_at),
        "numeric_value": item.numeric_value,
        "text_value": item.text_value,
        "unit": item.unit,
        "quality_score": quality_score,
        "raw_payload": item.raw_payload,
        "source": item.source,
    }


@api_bp.get("/logs")
def list_activity_logs():
    try:
        retention_days = max(1, int(current_app.config.get("ACTIVITY_LOG_RETENTION_DAYS", 7)))
        days = _parse_int(
            request.args.get("days", retention_days),
            field_name="days",
            min_value=1,
            max_value=retention_days,
        )
        limit = _parse_int(
            request.args.get("limit", 120),
            field_name="limit",
            min_value=1,
            max_value=300,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)

    items = load_activity_logs(days=days, limit=limit)
    return jsonify(
        {
            "items": [activity_log_item_to_dict(item) for item in items],
            "summary": summarize_activity_logs(items),
            "retention_days": retention_days,
        }
    )


def _reactor_build_to_dict(item: ReactorBuild, *, include_definition: bool = True) -> dict[str, Any]:
    definition = item.definition_json if isinstance(item.definition_json, dict) else {}
    nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    payload = {
        "reactor_build_id": item.reactor_build_id,
        "build_name": item.build_name,
        "build_date": item.build_date.isoformat() if item.build_date else None,
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "notes": item.notes,
        "is_active": item.is_active,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
    }
    if include_definition:
        payload["definition_json"] = definition
    return payload


def _validate_reactor_build_definition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Field 'definition_json' must be a JSON object.")

    canvas_value = value.get("canvas", {})
    if canvas_value in (None, ""):
        canvas_value = {}
    if not isinstance(canvas_value, dict):
        raise ValueError("Field 'definition_json.canvas' must be a JSON object.")

    canvas_width = int(round(_parse_float(canvas_value.get("width", 2400), field_name="definition_json.canvas.width")))
    canvas_height = int(round(_parse_float(canvas_value.get("height", 1600), field_name="definition_json.canvas.height")))
    if canvas_width < 200 or canvas_height < 200:
        raise ValueError("Field 'definition_json.canvas' must be at least 200x200.")
    if canvas_width > _MAX_REACTOR_BUILD_CANVAS_DIMENSION or canvas_height > _MAX_REACTOR_BUILD_CANVAS_DIMENSION:
        raise ValueError(
            f"Field 'definition_json.canvas' must not exceed {_MAX_REACTOR_BUILD_CANVAS_DIMENSION}x{_MAX_REACTOR_BUILD_CANVAS_DIMENSION}."
        )

    symbol_lookup = _builder_symbol_lookup()
    if not symbol_lookup:
        raise ValueError("The flowsheet library is not available on the server.")

    raw_nodes = value.get("nodes", [])
    if raw_nodes in (None, ""):
        raw_nodes = []
    if not isinstance(raw_nodes, list):
        raise ValueError("Field 'definition_json.nodes' must be a list.")
    if len(raw_nodes) > _MAX_REACTOR_BUILD_NODES:
        raise ValueError(f"Field 'definition_json.nodes' must not contain more than {_MAX_REACTOR_BUILD_NODES} items.")

    normalized_nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    instance_ids: set[str] = set()
    node_anchor_ids: dict[str, set[str]] = {}
    for index, node in enumerate(raw_nodes, start=1):
        if not isinstance(node, dict):
            raise ValueError(f"Node {index} in 'definition_json.nodes' must be an object.")

        node_id = _clean_string(node.get("id"), field_name=f"definition_json.nodes[{index}].id", required=True)
        symbol_id = _clean_string(
            node.get("symbol_id"),
            field_name=f"definition_json.nodes[{index}].symbol_id",
            required=True,
        )
        instance_id = _clean_string(
            node.get("instance_id"),
            field_name=f"definition_json.nodes[{index}].instance_id",
            required=True,
        )

        x_value = _parse_float(node.get("x", 0), field_name=f"definition_json.nodes[{index}].x")
        y_value = _parse_float(node.get("y", 0), field_name=f"definition_json.nodes[{index}].y")
        width_value = _parse_float(node.get("width", 120), field_name=f"definition_json.nodes[{index}].width")
        height_value = _parse_float(node.get("height", 80), field_name=f"definition_json.nodes[{index}].height")

        if width_value <= 0 or height_value <= 0:
            raise ValueError(f"Node {index} width and height must be > 0.")

        assert node_id is not None
        assert symbol_id is not None
        symbol = symbol_lookup.get(symbol_id)
        if symbol is None:
            raise ValueError(f"Symbol '{symbol_id}' is not registered in the flowsheet library.")
        if node_id in node_ids:
            raise ValueError(f"Node id '{node_id}' is duplicated in 'definition_json.nodes'.")
        node_ids.add(node_id)
        assert instance_id is not None
        instance_id = _validate_instance_id(
            instance_id,
            field_name=f"definition_json.nodes[{index}].instance_id",
        )
        if instance_id.lower() in instance_ids:
            raise ValueError(f"Element ID '{instance_id}' is duplicated in 'definition_json.nodes'.")
        instance_ids.add(instance_id.lower())

        communication_value = node.get("communication", {})
        if communication_value in (None, ""):
            communication_value = {}
        if not isinstance(communication_value, dict):
            raise ValueError(f"Field 'definition_json.nodes[{index}].communication' must be an object.")

        communication_payload = {
            "device_server_code": _clean_string(
                communication_value.get("device_server_code"),
                field_name=f"definition_json.nodes[{index}].communication.device_server_code",
            ),
            "connection_label": _clean_string(
                communication_value.get("connection_label"),
                field_name=f"definition_json.nodes[{index}].communication.connection_label",
            ),
            "protocol": _clean_string(
                communication_value.get("protocol"),
                field_name=f"definition_json.nodes[{index}].communication.protocol",
            ),
            "notes": _clean_string(
                communication_value.get("notes"),
                field_name=f"definition_json.nodes[{index}].communication.notes",
            ),
        }

        try:
            control_payload = normalize_control_definition(symbol_id, node.get("control"))
        except ValueError as exc:
            raise ValueError(f"Node {index} control is invalid: {exc}") from exc

        raw_anchors = node.get("anchors", [])
        if raw_anchors in (None, ""):
            raw_anchors = []
        if not isinstance(raw_anchors, list):
            raise ValueError(f"Field 'definition_json.nodes[{index}].anchors' must be a list.")
        if len(raw_anchors) > _MAX_REACTOR_BUILD_ANCHORS_PER_NODE:
            raise ValueError(
                f"Field 'definition_json.nodes[{index}].anchors' must not contain more than {_MAX_REACTOR_BUILD_ANCHORS_PER_NODE} items."
            )

        normalized_anchors: list[dict[str, Any]] = []
        anchor_ids_for_node: set[str] = set()
        for anchor_index, anchor in enumerate(raw_anchors, start=1):
            if not isinstance(anchor, dict):
                raise ValueError(
                    f"Anchor {anchor_index} in 'definition_json.nodes[{index}].anchors' must be an object."
                )

            anchor_id = _clean_string(
                anchor.get("id"),
                field_name=f"definition_json.nodes[{index}].anchors[{anchor_index}].id",
                required=True,
            )
            x_ratio = _parse_float(
                anchor.get("x_ratio"),
                field_name=f"definition_json.nodes[{index}].anchors[{anchor_index}].x_ratio",
            )
            y_ratio = _parse_float(
                anchor.get("y_ratio"),
                field_name=f"definition_json.nodes[{index}].anchors[{anchor_index}].y_ratio",
            )
            side = _validate_choice(
                anchor.get("side"),
                field_name=f"definition_json.nodes[{index}].anchors[{anchor_index}].side",
                allowed={"north", "south", "east", "west"},
                required=False,
            )

            if not 0 <= x_ratio <= 1:
                raise ValueError(
                    f"Field 'definition_json.nodes[{index}].anchors[{anchor_index}].x_ratio' must be between 0 and 1."
                )
            if not 0 <= y_ratio <= 1:
                raise ValueError(
                    f"Field 'definition_json.nodes[{index}].anchors[{anchor_index}].y_ratio' must be between 0 and 1."
                )

            assert anchor_id is not None
            if anchor_id in anchor_ids_for_node:
                raise ValueError(f"Anchor id '{anchor_id}' is duplicated on node '{node_id}'.")
            anchor_ids_for_node.add(anchor_id)
            normalized_anchors.append(
                {
                    "id": anchor_id,
                    "x_ratio": round(x_ratio, 6),
                    "y_ratio": round(y_ratio, 6),
                    "side": side,
                }
            )

        node_anchor_ids[node_id] = anchor_ids_for_node
        normalized_nodes.append(
            {
                "id": node_id,
                "symbol_id": symbol_id,
                "instance_id": instance_id,
                "label": _clean_string(symbol.get("label"), field_name=f"definition_json.nodes[{index}].label")
                or symbol_id,
                "category": _clean_string(
                    symbol.get("category"),
                    field_name=f"definition_json.nodes[{index}].category",
                ),
                "svg_url": _clean_string(
                    symbol.get("svg_url"),
                    field_name=f"definition_json.nodes[{index}].svg_url",
                ),
                "x": round(x_value, 2),
                "y": round(y_value, 2),
                "width": round(width_value, 2),
                "height": round(height_value, 2),
                "communication": communication_payload,
                "control": control_payload,
                "anchors": normalized_anchors,
            }
        )

    raw_edges = value.get("edges", [])
    if raw_edges in (None, ""):
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("Field 'definition_json.edges' must be a list.")
    if len(raw_edges) > _MAX_REACTOR_BUILD_EDGES:
        raise ValueError(f"Field 'definition_json.edges' must not contain more than {_MAX_REACTOR_BUILD_EDGES} items.")

    normalized_edges: list[dict[str, Any]] = []
    normalized_node_lookup = {node["id"]: node for node in normalized_nodes}
    edge_ids: set[str] = set()
    edge_connections: set[tuple[str, str]] = set()
    for index, edge in enumerate(raw_edges, start=1):
        if not isinstance(edge, dict):
            raise ValueError(f"Edge {index} in 'definition_json.edges' must be an object.")

        edge_id = _clean_string(edge.get("id"), field_name=f"definition_json.edges[{index}].id", required=True)
        source_node_id = _clean_string(
            edge.get("source_node_id"),
            field_name=f"definition_json.edges[{index}].source_node_id",
            required=True,
        )
        target_node_id = _clean_string(
            edge.get("target_node_id"),
            field_name=f"definition_json.edges[{index}].target_node_id",
            required=True,
        )
        source_anchor_id = _clean_string(
            edge.get("source_anchor_id"),
            field_name=f"definition_json.edges[{index}].source_anchor_id",
        )
        target_anchor_id = _clean_string(
            edge.get("target_anchor_id"),
            field_name=f"definition_json.edges[{index}].target_anchor_id",
        )
        raw_route_points = edge.get("route_points", [])
        if raw_route_points in (None, ""):
            raw_route_points = []
        if not isinstance(raw_route_points, list):
            raise ValueError(f"Field 'definition_json.edges[{index}].route_points' must be a list.")
        if len(raw_route_points) > _MAX_REACTOR_BUILD_ROUTE_POINTS:
            raise ValueError(
                f"Field 'definition_json.edges[{index}].route_points' must not contain more than {_MAX_REACTOR_BUILD_ROUTE_POINTS} items."
            )

        assert edge_id is not None
        assert source_node_id is not None
        assert target_node_id is not None

        if edge_id in edge_ids:
            raise ValueError(f"Edge id '{edge_id}' is duplicated in 'definition_json.edges'.")
        if source_node_id not in node_ids:
            raise ValueError(f"Edge {edge_id} references missing source node '{source_node_id}'.")
        if target_node_id not in node_ids:
            raise ValueError(f"Edge {edge_id} references missing target node '{target_node_id}'.")
        if source_node_id == target_node_id:
            raise ValueError(f"Edge {edge_id} must connect two different nodes.")
        if source_anchor_id is not None and source_anchor_id not in node_anchor_ids.get(source_node_id, set()):
            raise ValueError(f"Edge {edge_id} references missing source anchor '{source_anchor_id}'.")
        if target_anchor_id is not None and target_anchor_id not in node_anchor_ids.get(target_node_id, set()):
            raise ValueError(f"Edge {edge_id} references missing target anchor '{target_anchor_id}'.")
        connection_key = tuple(
            sorted(
                (
                    f"{source_node_id}:{source_anchor_id or ''}",
                    f"{target_node_id}:{target_anchor_id or ''}",
                )
            )
        )
        if connection_key in edge_connections:
            raise ValueError(f"Edge '{edge_id}' duplicates an existing connection.")

        normalized_route_points: list[dict[str, float]] = []
        for point_index, point in enumerate(raw_route_points, start=1):
            if not isinstance(point, dict):
                raise ValueError(
                    f"Route point {point_index} in 'definition_json.edges[{index}].route_points' must be an object."
                )

            x_value = _parse_float(
                point.get("x"),
                field_name=f"definition_json.edges[{index}].route_points[{point_index}].x",
            )
            y_value = _parse_float(
                point.get("y"),
                field_name=f"definition_json.edges[{index}].route_points[{point_index}].y",
            )
            normalized_route_points.append(
                {
                    "x": round(x_value, 2),
                    "y": round(y_value, 2),
                }
            )

        _validate_orthogonal_route_points(
            edge_id,
            normalized_route_points,
            source_point=_anchor_point_for_validation(normalized_node_lookup[source_node_id], source_anchor_id),
            target_point=_anchor_point_for_validation(normalized_node_lookup[target_node_id], target_anchor_id),
        )

        edge_ids.add(edge_id)
        edge_connections.add(connection_key)
        normalized_edges.append(
            {
                "id": edge_id,
                "source_node_id": source_node_id,
                "source_anchor_id": source_anchor_id,
                "target_node_id": target_node_id,
                "target_anchor_id": target_anchor_id,
                "route_points": normalized_route_points,
            }
        )

    return {
        "canvas": {
            "width": canvas_width,
            "height": canvas_height,
        },
        "nodes": normalized_nodes,
        "edges": normalized_edges,
    }


def _apply_reactor_build_payload(item: ReactorBuild, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "build_name" in payload:
        item.build_name = _clean_string(payload.get("build_name"), field_name="build_name", required=True)
    if not partial or "build_date" in payload:
        item.build_date = _parse_date(payload.get("build_date"), field_name="build_date")
    if not partial:
        item.created_by = _clean_string(payload.get("created_by"), field_name="created_by", required=True)
    if "updated_by" in payload:
        item.updated_by = _clean_string(payload.get("updated_by"), field_name="updated_by")
    elif not partial:
        item.updated_by = item.created_by
    if not partial or "definition_json" in payload:
        item.definition_json = _validate_reactor_build_definition(payload.get("definition_json"))
    if "notes" in payload:
        item.notes = _clean_string(payload.get("notes"), field_name="notes")
    elif not partial:
        item.notes = None
    if "is_active" in payload:
        item.is_active = _parse_bool(payload.get("is_active"), field_name="is_active")
    if item.updated_by is None:
        item.updated_by = item.created_by


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
                "device_protocols": "/api/device-protocols",
                "reactor_builds": "/api/reactor-builds",
                "device_servers": "/api/device-servers",
                "device_connections": "/api/device-connections",
                "device_binding_example": "/api/devices/<device_id>/binding",
                "device_commands": "/api/devices/<device_id>/commands",
                "device_measurements": "/api/devices/<device_id>/measurements",
                "device_connection_probe": "/api/device-connections/<connection_id>/probe",
            }
        }
    )


@api_bp.get("/device-protocols")
def list_device_protocol_options():
    return jsonify({"items": list_supported_protocols(), "options": list_supported_protocol_options()})


@api_bp.get("/reactor-builds")
def list_reactor_builds():
    items = (
        ReactorBuild.query.order_by(ReactorBuild.updated_at.desc(), ReactorBuild.reactor_build_id.desc()).all()
    )
    return jsonify({"items": [_reactor_build_to_dict(item, include_definition=False) for item in items]})


@api_bp.post("/reactor-builds")
def create_reactor_build():
    try:
        payload = _load_json_payload()
        item = ReactorBuild()
        _apply_reactor_build_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_reactor_build_to_dict(item, include_definition=True)), 201


@api_bp.get("/reactor-builds/<int:reactor_build_id>")
def get_reactor_build(reactor_build_id: int):
    item, error_response = _get_or_404(ReactorBuild, reactor_build_id, "ReactorBuild")
    if error_response:
        return error_response
    return jsonify(_reactor_build_to_dict(item, include_definition=True))


@api_bp.patch("/reactor-builds/<int:reactor_build_id>")
def update_reactor_build(reactor_build_id: int):
    item, error_response = _get_or_404(ReactorBuild, reactor_build_id, "ReactorBuild")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_reactor_build_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_reactor_build_to_dict(item, include_definition=True))


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

    if item.manual_state is not None:
        db.session.delete(item.manual_state)
    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.get("/devices/<int:device_id>/measurements")
def list_device_measurements(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        limit_raw = request.args.get("limit", 100)
        limit = _parse_int(limit_raw, field_name="limit", min_value=1, max_value=1000)
        since_minutes_raw = request.args.get("since_minutes")
        since_minutes = (
            _parse_int(since_minutes_raw, field_name="since_minutes", min_value=1, max_value=30 * 24 * 60)
            if since_minutes_raw not in (None, "")
            else None
        )
        max_points_raw = request.args.get("max_points")
        max_points = (
            _parse_int(max_points_raw, field_name="max_points", min_value=2, max_value=2000)
            if max_points_raw not in (None, "")
            else None
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)

    channel_code = _clean_string(request.args.get("channel_code"), field_name="channel_code")
    query = Measurement.query.filter_by(device_id=device.device_id)
    if channel_code:
        query = query.filter(Measurement.channel_code == channel_code)
    if since_minutes is not None:
        if not channel_code:
            return _json_error("Field 'channel_code' is required when using 'since_minutes'.", 400)
        series = load_device_plot_series(
            device_id=device.device_id,
            channel_codes=[channel_code],
            since_minutes=since_minutes,
            max_points=max_points or limit,
        )
        items = (series[0] if series else {"items": []}).get("items", [])
        return jsonify({"items": items})
    else:
        items = (
            query.order_by(Measurement.measured_at.desc(), Measurement.measurement_id.desc())
            .limit(limit)
            .all()
        )
    return jsonify({"items": [_measurement_to_dict(item) for item in items]})


@api_bp.get("/devices/<int:device_id>/plot-series")
def list_device_plot_series(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        since_minutes = _parse_int(
            request.args.get("since_minutes", 60),
            field_name="since_minutes",
            min_value=1,
            max_value=30 * 24 * 60,
        )
        max_points = _parse_int(
            request.args.get("max_points", 240),
            field_name="max_points",
            min_value=2,
            max_value=2000,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)

    channel_codes = []
    for raw_value in request.args.getlist("channel_code"):
        cleaned = _clean_string(raw_value, field_name="channel_code")
        if cleaned:
            channel_codes.append(cleaned)
    if not channel_codes:
        return _json_error("At least one 'channel_code' query parameter is required.", 400)
    if len(channel_codes) > 24:
        return _json_error("At most 24 channel_code values may be requested at once.", 400)

    series = load_device_plot_series(
        device_id=device.device_id,
        channel_codes=channel_codes,
        since_minutes=since_minutes,
        max_points=max_points,
    )
    return jsonify(
        {
            "device_id": device.device_id,
            "since_minutes": since_minutes,
            "max_points": max_points,
            "series": series,
        }
    )


@api_bp.get("/devices/<int:device_id>/commands")
def list_device_commands(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    include_events = request.args.get("include_events", "").strip().lower() in {"1", "true", "yes"}
    items = (
        ControlCommand.query.filter_by(device_id=device.device_id)
        .order_by(ControlCommand.command_id.desc())
        .limit(100)
        .all()
    )
    return jsonify({"items": [_control_command_to_dict(item, include_events=include_events) for item in items]})


@api_bp.get("/devices/<int:device_id>/manual-state")
def get_manual_state_for_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        watch = _parse_query_bool("watch", default=False)
        refresh = _parse_query_bool("refresh", default=False)
        await_ms = _parse_int(
            request.args.get("await_ms", 0),
            field_name="await_ms",
            min_value=0,
            max_value=5000,
        )
        requested_by = _normalize_requested_by(request.args.get("requested_by"), default="process_view")
        existing_state = db.session.get(DeviceManualState, device.device_id)
        previous_reported_at = None if existing_state is None else existing_state.last_reported_at
        state = ensure_manual_state_snapshot(
            current_app,
            device,
            requested_by=requested_by,
            watch=watch,
            refresh=refresh,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response

    if refresh and await_ms > 0:
        state = wait_for_manual_state_refresh(
            current_app,
            device.device_id,
            previous_reported_at=previous_reported_at,
            timeout_ms=await_ms,
        ) or state
    return jsonify({"state": _device_manual_state_to_dict(state)})


@api_bp.post("/devices/<int:device_id>/manual-state")
def queue_manual_state_for_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        body = _load_json_payload()
        requested_by = _normalize_requested_by(body.get("requested_by"), default="api")
        if _extract_process_manual_token() is not None:
            requested_by = "process_manual"
        desired_is_on = _parse_bool(body.get("is_on"), field_name="is_on")
        desired_speed = _parse_int(
            body.get("speed"),
            field_name="speed",
            min_value=0,
            max_value=max_rpm_for_protocol(device.protocol, default=10000),
        )
        if desired_is_on and desired_speed <= 0:
            raise ValueError("Field 'speed' must be greater than 0 when 'is_on' is true.")
        state = queue_manual_state_update(
            current_app,
            device,
            desired_is_on=desired_is_on,
            desired_speed=desired_speed,
            requested_by=requested_by,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify({"state": _device_manual_state_to_dict(state)}), 202


@api_bp.post("/devices/<int:device_id>/commands")
def execute_command_for_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        body = _load_json_payload()
        command_name = _clean_string(body.get("command_name"), field_name="command_name", required=True)
        requested_by = _normalize_requested_by(body.get("requested_by"), default="api")
        command_payload = body.get("payload", {})
        if not isinstance(command_payload, dict):
            raise ValueError("Field 'payload' must be a JSON object.")
        assert command_name is not None
        if _extract_process_manual_token() is not None:
            requested_by = "process_manual"
            command_payload = _validate_process_manual_command_payload(command_name, command_payload)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    try:
        execution = execute_device_command(
            device,
            command_name=command_name,
            payload=command_payload,
            requested_by=requested_by,
        )
    except DeviceCommandError as exc:
        if exc.command is not None:
            ok, error_response = _commit()
            if not ok:
                return error_response
            return (
                jsonify(
                    {
                        "error": str(exc),
                        "details": exc.details,
                        "command": _control_command_to_dict(exc.command, include_events=True),
                    }
                ),
                exc.status_code,
            )
        return _json_error(str(exc), exc.status_code, str(exc.details) if exc.details else None)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return (
        jsonify(
            {
                "command": _control_command_to_dict(execution.command, include_events=True),
                "result": {
                    "acknowledged": execution.result.acknowledged,
                    "response_text": execution.result.response_text,
                    "response_hex": execution.result.response_hex,
                    "metadata": execution.result.metadata,
                },
                "measurement": None if execution.measurement is None else _measurement_to_dict(execution.measurement),
            }
        ),
        201,
    )


@api_bp.get("/commands/<int:command_id>")
def get_command(command_id: int):
    item, error_response = _get_or_404(ControlCommand, command_id, "ControlCommand")
    if error_response:
        return error_response
    return jsonify(_control_command_to_dict(item, include_events=True))


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


# ---------------------------------------------------------------------------
# Recipe helpers
# ---------------------------------------------------------------------------

_RECIPE_ALLOWED_STATUSES = {"draft", "approved", "archived"}
_RECIPE_MAX_STEPS = 500
_RECIPE_STEP_NUMERIC_FIELDS = ("delta_time", "temp", "pressure", "rpm")
_RECIPE_STEP_TARGET_FIELDS = ("temp", "pressure", "rpm")
_RECIPE_MIN_TEMP_C = -40.0
_RECIPE_MAX_TEMP_C = 150.0


def _normalized_recipe_actor_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_recipe_actor_node(raw_node: Any) -> bool:
    if not isinstance(raw_node, dict):
        return False

    category = _normalized_recipe_actor_key(raw_node.get("category"))
    if category == "actuators":
        return True

    control = raw_node.get("control")
    if isinstance(control, dict) and str(control.get("profile_id") or "").strip():
        return True

    symbol_id = str(raw_node.get("symbol_id") or "").strip()
    return get_default_profile_id(symbol_id) is not None


def _recipe_allowed_actor_instance_ids(item: ReactorBuild | None) -> list[str]:
    return list(_recipe_allowed_actor_lookup(item).keys())


def _recipe_allowed_actor_lookup(item: ReactorBuild | None) -> dict[str, dict[str, Any]]:
    definition = item.definition_json if item is not None and isinstance(item.definition_json, dict) else {}
    raw_nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    if not isinstance(raw_nodes, list):
        return {}

    actors: dict[str, dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for raw_node in raw_nodes:
        if not _is_recipe_actor_node(raw_node):
            continue

        instance_id = str(raw_node.get("instance_id") or "").strip()
        if not instance_id:
            continue

        normalized_key = _normalized_recipe_actor_key(instance_id)
        if normalized_key in seen_keys:
            continue

        seen_keys.add(normalized_key)
        symbol_id = str(raw_node.get("symbol_id") or "").strip()
        control = raw_node.get("control") if isinstance(raw_node.get("control"), dict) else {}
        profile_id = str(control.get("profile_id") or get_default_profile_id(symbol_id) or "").strip()
        actors[instance_id] = {
            "actor": instance_id,
            "profile_id": profile_id,
            "symbol_id": symbol_id,
        }

    return actors


def _recipe_to_dict(item: Recipe, *, include_steps: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recipe_id": item.recipe_id,
        "title": item.title,
        "operator_name": item.operator_name,
        "version": item.version,
        "status": item.status,
        "reactor_build_id": item.reactor_build_id,
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "is_active": item.is_active,
        "created_at": _dt(item.created_at),
        "updated_at": _dt(item.updated_at),
    }
    if include_steps:
        payload["steps"] = item.steps_json if isinstance(item.steps_json, list) else []
    return payload


def _recipe_target_fields_for_actor(actor_meta: dict[str, Any] | None) -> tuple[str, ...]:
    profile_id = str((actor_meta or {}).get("profile_id") or "").strip().lower()
    symbol_id = str((actor_meta or {}).get("symbol_id") or "").strip().lower()
    if not profile_id and not symbol_id:
        return ()
    if profile_id == "hc_system_temperature":
        return ("temp",)
    if profile_id in {"motor_rpm", "pump_rpm"}:
        return ("rpm",)
    if "pressure" in profile_id or "vacuum" in profile_id or "pressure" in symbol_id or "vacuum" in symbol_id:
        return ("pressure",)
    if "pump" in profile_id or "pump" in symbol_id:
        return ("rpm",)
    return _RECIPE_STEP_TARGET_FIELDS


def _parse_recipe_actor_priority(raw_value: Any, *, field_name: str) -> int | None:
    if raw_value in (None, ""):
        return None
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field '{field_name}' must be an integer or null.") from exc
    return parsed


def _parse_recipe_step_actor_refs(raw: dict[str, Any], index: int) -> list[dict[str, Any]]:
    raw_refs = raw.get("actors")
    refs: list[dict[str, Any]] = []
    if isinstance(raw_refs, list):
        for ref_index, raw_ref in enumerate(raw_refs, start=1):
            field_name = f"steps[{index}].actors[{ref_index}]"
            if isinstance(raw_ref, str):
                actor = _clean_string(raw_ref, field_name=field_name) or ""
                priority = None
            elif isinstance(raw_ref, dict):
                actor = _clean_string(raw_ref.get("actor"), field_name=f"{field_name}.actor") or ""
                priority = _parse_recipe_actor_priority(raw_ref.get("priority"), field_name=f"{field_name}.priority")
            else:
                raise ValueError(f"Field '{field_name}' must be an object or actor id string.")
            if actor:
                refs.append({"actor": actor, "priority": priority})
    else:
        actor = _clean_string(raw.get("actor"), field_name=f"steps[{index}].actor") or ""
        if actor:
            refs.append({"actor": actor, "priority": None})

    deduplicated: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for ref in refs:
        normalized_key = _normalized_recipe_actor_key(ref.get("actor"))
        if not normalized_key or normalized_key in seen_keys:
            continue
        seen_keys.add(normalized_key)
        deduplicated.append(ref)
    return deduplicated


def _parse_recipe_step(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Step {index} must be an object.")

    actor_refs = _parse_recipe_step_actor_refs(raw, index)
    actor = actor_refs[0]["actor"] if actor_refs else ""
    task = _clean_string(raw.get("task"), field_name=f"steps[{index}].task") or ""

    normalized: dict[str, Any] = {"actor": actor, "actors": actor_refs, "task": task}
    for field in _RECIPE_STEP_NUMERIC_FIELDS:
        raw_val = raw.get(field)
        if raw_val in (None, ""):
            normalized[field] = None
        else:
            parsed = _parse_float(raw_val, field_name=f"steps[{index}].{field}")
            if field == "temp":
                if parsed < _RECIPE_MIN_TEMP_C or parsed > _RECIPE_MAX_TEMP_C:
                    raise ValueError(
                        f"Field 'steps[{index}].temp' must be between "
                        f"{_RECIPE_MIN_TEMP_C:g} and {_RECIPE_MAX_TEMP_C:g}."
                    )
            elif parsed < 0:
                raise ValueError(f"Field 'steps[{index}].{field}' must be >= 0.")
            normalized[field] = round(parsed, 2)
    return normalized


def _validate_recipe_steps(
    raw_steps: Any,
    *,
    allowed_actor_ids: list[str] | None = None,
    allowed_actor_lookup: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if raw_steps in (None, ""):
        return []
    if not isinstance(raw_steps, list):
        raise ValueError("Field 'steps' must be a list.")
    if len(raw_steps) > _RECIPE_MAX_STEPS:
        raise ValueError(f"Field 'steps' must not contain more than {_RECIPE_MAX_STEPS} items.")

    if allowed_actor_lookup is None:
        allowed_actor_lookup = {
            str(actor_id).strip(): {"actor": str(actor_id).strip(), "profile_id": "", "symbol_id": ""}
            for actor_id in (allowed_actor_ids or [])
            if str(actor_id or "").strip()
        }
    allowed_actor_by_key = {
        _normalized_recipe_actor_key(actor_id): actor_meta
        for actor_id, actor_meta in allowed_actor_lookup.items()
        if str(actor_id or "").strip()
    }
    result: list[dict[str, Any]] = []
    initialized_fields_by_actor: dict[str, set[str]] = {}
    for index, raw in enumerate(raw_steps, start=1):
        step = _parse_recipe_step(raw, index)
        # Skip fully empty trailing rows
        if not step["actors"] and not step["task"] and all(step[f] is None for f in _RECIPE_STEP_NUMERIC_FIELDS):
            continue

        if not step["actors"]:
            raise ValueError(f"Field 'steps[{index}].actors' must contain at least one actor.")

        normalized_refs: list[dict[str, Any]] = []
        for ref in step["actors"]:
            normalized_actor_key = _normalized_recipe_actor_key(ref.get("actor"))
            if allowed_actor_by_key and normalized_actor_key not in allowed_actor_by_key:
                raise ValueError(
                    f"Field 'steps[{index}].actors' must match actuator instance_ids from the selected flowsheet."
                )
            actor_meta = allowed_actor_by_key.get(normalized_actor_key) or {"actor": ref["actor"], "profile_id": "", "symbol_id": ""}
            canonical_actor = str(actor_meta.get("actor") or ref["actor"]).strip()
            target_fields = _recipe_target_fields_for_actor(actor_meta)
            initialized_fields = initialized_fields_by_actor.setdefault(canonical_actor, set())
            missing_initial_fields = [
                field_name
                for field_name in target_fields
                if field_name not in initialized_fields and step.get(field_name) is None
            ]
            if missing_initial_fields:
                field_label = ", ".join(missing_initial_fields)
                raise ValueError(
                    f"Step {index} for actor '{canonical_actor}' must define {field_label} before it can hold or ramp."
                )
            for field_name in target_fields:
                if step.get(field_name) is not None:
                    initialized_fields.add(field_name)
            normalized_refs.append({"actor": canonical_actor, "priority": ref.get("priority")})

        step["actors"] = normalized_refs
        step["actor"] = normalized_refs[0]["actor"]

        result.append(step)
    return result


def _apply_recipe_payload(item: Recipe, payload: dict[str, Any], *, partial: bool) -> None:
    if not partial or "title" in payload:
        item.title = _clean_string(payload.get("title"), field_name="title", required=True)
    if not partial or "operator_name" in payload:
        item.operator_name = _clean_string(payload.get("operator_name"), field_name="operator_name", required=True)
    if not partial:
        item.created_by = _clean_string(payload.get("created_by"), field_name="created_by", required=True)
    if "updated_by" in payload:
        item.updated_by = _clean_string(payload.get("updated_by"), field_name="updated_by")
    elif not partial:
        item.updated_by = item.created_by

    recipe_build: ReactorBuild | None = None
    if "reactor_build_id" in payload:
        raw_build_id = payload.get("reactor_build_id")
        if raw_build_id in (None, ""):
            item.reactor_build_id = None
        else:
            try:
                item.reactor_build_id = int(raw_build_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("Field 'reactor_build_id' must be an integer or null.") from exc
    elif not partial and item.reactor_build_id is None:
        item.reactor_build_id = None

    if item.reactor_build_id is None:
        raise ValueError("Field 'reactor_build_id' is required.")

    recipe_build = db.session.get(ReactorBuild, item.reactor_build_id)
    if recipe_build is None:
        raise ValueError(f"ReactorBuild with id {item.reactor_build_id} was not found.")

    allowed_actor_lookup = _recipe_allowed_actor_lookup(recipe_build)
    if not allowed_actor_lookup:
        raise ValueError("The selected flowsheet does not contain any actors.")

    if not partial or "steps" in payload:
        item.steps_json = _validate_recipe_steps(payload.get("steps"), allowed_actor_lookup=allowed_actor_lookup)
    if "status" in payload:
        item.status = _validate_choice(
            payload.get("status"),
            field_name="status",
            allowed=_RECIPE_ALLOWED_STATUSES,
            required=True,
        )
    if "is_active" in payload:
        item.is_active = _parse_bool(payload.get("is_active"), field_name="is_active")
    if item.updated_by is None and not partial:
        item.updated_by = item.created_by


# ---------------------------------------------------------------------------
# Recipe endpoints
# ---------------------------------------------------------------------------

@api_bp.get("/recipes")
def list_recipes():
    items = Recipe.query.order_by(Recipe.updated_at.desc(), Recipe.recipe_id.desc()).all()
    return jsonify({"items": [_recipe_to_dict(item, include_steps=False) for item in items]})


@api_bp.post("/recipes")
def create_recipe():
    try:
        payload = _load_json_payload()
        item = Recipe()
        _apply_recipe_payload(item, payload, partial=False)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    db.session.add(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_recipe_to_dict(item, include_steps=True)), 201


@api_bp.get("/recipes/<int:recipe_id>")
def get_recipe(recipe_id: int):
    item, error_response = _get_or_404(Recipe, recipe_id, "Recipe")
    if error_response:
        return error_response
    return jsonify(_recipe_to_dict(item, include_steps=True))


@api_bp.patch("/recipes/<int:recipe_id>")
def update_recipe(recipe_id: int):
    item, error_response = _get_or_404(Recipe, recipe_id, "Recipe")
    if error_response:
        return error_response

    try:
        payload = _load_json_payload()
        _apply_recipe_payload(item, payload, partial=True)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify(_recipe_to_dict(item, include_steps=True))


@api_bp.delete("/recipes/<int:recipe_id>")
def delete_recipe(recipe_id: int):
    item, error_response = _get_or_404(Recipe, recipe_id, "Recipe")
    if error_response:
        return error_response

    db.session.delete(item)
    ok, error_response = _commit()
    if not ok:
        return error_response
    return "", 204


@api_bp.get("/process-program")
def get_process_program():
    item = db.session.get(RecipeProgramState, 1)
    return jsonify({"program": recipe_program_state_to_dict(item)})


@api_bp.post("/process-program/start")
def start_process_program():
    try:
        body = _load_json_payload()
        recipe_id = _parse_int(body.get("recipe_id"), field_name="recipe_id", min_value=1)
        requested_by = _normalize_requested_by(body.get("requested_by"), default="process_recipe")
        if _extract_process_manual_token() is not None:
            requested_by = "process_recipe"
    except ValueError as exc:
        return _json_error(str(exc), 400)

    recipe, error_response = _get_or_404(Recipe, recipe_id, "Recipe")
    if error_response:
        return error_response

    try:
        item = start_recipe_program(current_app, recipe, requested_by=requested_by)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify({"program": recipe_program_state_to_dict(item)}), 202


@api_bp.post("/process-program/stop")
def stop_process_program():
    try:
        body = _load_json_payload()
        requested_by = _normalize_requested_by(body.get("requested_by"), default="process_recipe")
        if _extract_process_manual_token() is not None:
            requested_by = "process_recipe"
    except ValueError as exc:
        return _json_error(str(exc), 400)

    try:
        item = stop_recipe_program(current_app, requested_by=requested_by)
    except DeviceCommandError as exc:
        return _json_error(str(exc), exc.status_code, str(exc.details) if exc.details else None)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    ok, error_response = _commit()
    if not ok:
        return error_response
    return jsonify({"program": recipe_program_state_to_dict(item)}), 202
