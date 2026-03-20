from __future__ import annotations

import hmac
from datetime import date, datetime, timezone
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from .builder_auth import REACTOR_BUILDER_WRITE_SCOPE, verify_scoped_token
from .extensions import db
from .models import (
    ControlCommand,
    ControlCommandEvent,
    Device,
    DeviceBindingCurrent,
    DeviceBindingHistory,
    DeviceConnection,
    DeviceServer,
    Measurement,
    ReactorBuild,
)
from .services import DeviceCommandError, TcpSocketConfig, execute_device_command, list_supported_protocols, probe_tcp_socket


api_bp = Blueprint("api", __name__, url_prefix="/api")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


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


def _is_builder_write_request() -> bool:
    if request.method not in {"POST", "PATCH"}:
        return False
    path = request.path.rstrip("/")
    return path == "/api/reactor-builds" or path.startswith("/api/reactor-builds/")


@api_bp.before_request
def require_api_token_for_writes():
    if request.method in _SAFE_METHODS:
        return None

    if not current_app.config.get("API_AUTH_REQUIRED", True):
        return None

    builder_token = _extract_builder_write_token() if _is_builder_write_request() else None
    secret_key = current_app.config.get("SECRET_KEY")
    if builder_token and secret_key:
        if verify_scoped_token(
            builder_token,
            secret_key=secret_key,
            expected_scope=REACTOR_BUILDER_WRITE_SCOPE,
        ):
            return None

    expected_token = current_app.config.get("API_AUTH_TOKEN")
    if not expected_token:
        if builder_token is not None:
            return _json_auth_error(
                "Invalid or expired Reactor Builder token. Reload the builder page and try again.",
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

    raw_nodes = value.get("nodes", [])
    if raw_nodes in (None, ""):
        raw_nodes = []
    if not isinstance(raw_nodes, list):
        raise ValueError("Field 'definition_json.nodes' must be a list.")

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
        label = _clean_string(node.get("label"), field_name=f"definition_json.nodes[{index}].label") or symbol_id
        category = _clean_string(node.get("category"), field_name=f"definition_json.nodes[{index}].category")
        svg_url = _clean_string(node.get("svg_url"), field_name=f"definition_json.nodes[{index}].svg_url")

        x_value = _parse_float(node.get("x", 0), field_name=f"definition_json.nodes[{index}].x")
        y_value = _parse_float(node.get("y", 0), field_name=f"definition_json.nodes[{index}].y")
        width_value = _parse_float(node.get("width", 120), field_name=f"definition_json.nodes[{index}].width")
        height_value = _parse_float(node.get("height", 80), field_name=f"definition_json.nodes[{index}].height")

        if width_value <= 0 or height_value <= 0:
            raise ValueError(f"Node {index} width and height must be > 0.")

        assert node_id is not None
        assert symbol_id is not None
        if node_id in node_ids:
            raise ValueError(f"Node id '{node_id}' is duplicated in 'definition_json.nodes'.")
        node_ids.add(node_id)
        assert instance_id is not None
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

        raw_anchors = node.get("anchors", [])
        if raw_anchors in (None, ""):
            raw_anchors = []
        if not isinstance(raw_anchors, list):
            raise ValueError(f"Field 'definition_json.nodes[{index}].anchors' must be a list.")

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
                "label": label,
                "category": category,
                "svg_url": svg_url,
                "x": round(x_value, 2),
                "y": round(y_value, 2),
                "width": round(width_value, 2),
                "height": round(height_value, 2),
                "communication": communication_payload,
                "anchors": normalized_anchors,
            }
        )

    raw_edges = value.get("edges", [])
    if raw_edges in (None, ""):
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("Field 'definition_json.edges' must be a list.")

    normalized_edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
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

        edge_ids.add(edge_id)
        normalized_edges.append(
            {
                "id": edge_id,
                "source_node_id": source_node_id,
                "source_anchor_id": source_anchor_id,
                "target_node_id": target_node_id,
                "target_anchor_id": target_anchor_id,
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
    if not partial or "created_by" in payload:
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
    return jsonify({"items": list_supported_protocols()})


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
    except ValueError as exc:
        return _json_error(str(exc), 400)

    channel_code = _clean_string(request.args.get("channel_code"), field_name="channel_code")
    query = Measurement.query.filter_by(device_id=device.device_id)
    if channel_code:
        query = query.filter(Measurement.channel_code == channel_code)
    items = (
        query.order_by(Measurement.measured_at.desc(), Measurement.measurement_id.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"items": [_measurement_to_dict(item) for item in items]})


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


@api_bp.post("/devices/<int:device_id>/commands")
def execute_command_for_device(device_id: int):
    device, error_response = _get_or_404(Device, device_id, "Device")
    if error_response:
        return error_response

    try:
        body = _load_json_payload()
        command_name = _clean_string(body.get("command_name"), field_name="command_name", required=True)
        requested_by = _clean_string(body.get("requested_by"), field_name="requested_by") or "api"
        command_payload = body.get("payload", {})
        if not isinstance(command_payload, dict):
            raise ValueError("Field 'payload' must be a JSON object.")
        assert command_name is not None
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
