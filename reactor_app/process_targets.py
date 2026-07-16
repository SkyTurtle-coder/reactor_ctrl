from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import joinedload, selectinload

from .models import Device, DeviceBindingCurrent, DeviceConnection, ReactorBuild


def _dt(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def normalize_lookup_value(value: Any) -> str:
    return str(value or "").strip().lower()


_LOOKUP_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_LOOKUP_TRAILING_NUMBER_RE = re.compile(r"^([a-z]+)\d+$")


def server_lookup_values(value: Any) -> list[str]:
    normalized = normalize_lookup_value(value)
    if not normalized:
        return []

    aliases: list[str] = []

    def add(alias: str) -> None:
        alias = alias.strip().lower()
        if alias and alias not in aliases:
            aliases.append(alias)

    add(normalized)
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    add(compact)

    tokens = [token for token in _LOOKUP_TOKEN_SPLIT_RE.split(normalized) if token]
    if tokens:
        add(tokens[0])

    compact_match = _LOOKUP_TRAILING_NUMBER_RE.fullmatch(compact)
    if compact_match:
        add(compact_match.group(1))

    return aliases


def default_measurement_plot_channels_for_target(*, symbol_id: str, protocol: str) -> list[dict[str, Any]]:
    normalized_symbol_id = normalize_lookup_value(symbol_id)
    normalized_protocol = normalize_lookup_value(protocol)
    if normalized_symbol_id == "motor" and normalized_protocol == "ika_eurostar_60":
        return [
            {
                "channel_id": None,
                "channel_code": "ika_actual_rpm",
                "display_name": "Actual RPM",
                "unit": "rpm",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "ika_setpoint_rpm",
                "display_name": "Setpoint RPM",
                "unit": "rpm",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "ika_torque_ncm",
                "display_name": "Torque",
                "unit": "Ncm",
                "value_type": "float",
                "data_source": "measurement",
            },
        ]
    if normalized_symbol_id == "hc_system" and normalized_protocol in {"huber_unistat_430", "huber_pilot_one"}:
        return [
            {
                "channel_id": None,
                "channel_code": "setpoint_C",
                "display_name": "Setpoint",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "actual_temp_C",
                "display_name": "Actual Temperature",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "external_temp_C",
                "display_name": "External Temperature",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
        ]
    if normalized_symbol_id == "hc_system" and normalized_protocol == "huber_cc230":
        channels = [
            {
                "channel_id": None,
                "channel_code": "setpoint_C",
                "display_name": "Setpoint",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "internal_temp_C",
                "display_name": "Internal Temperature",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
            {
                "channel_id": None,
                "channel_code": "external_temp_C",
                "display_name": "External Temperature",
                "unit": "degC",
                "value_type": "float",
                "data_source": "measurement",
            },
        ]
        return channels
    if normalized_symbol_id in {"qi", "qic"} and normalized_protocol in {"mettler_toledo_ics435", "ics435_mtsics"}:
        return [
            {
                "channel_id": None,
                "channel_code": "weight",
                "display_name": "Weight",
                "unit": "",
                "value_type": "float",
                "data_source": "measurement",
            },
        ]
    return []


def resolve_process_device_targets_for_definition(
    definition: dict[str, Any] | None,
    *,
    categories: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    definition = definition if isinstance(definition, dict) else {}
    raw_nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    if not isinstance(raw_nodes, list):
        return {}

    allowed_categories = {str(value).strip().lower() for value in (categories or set()) if str(value).strip()}
    matching_nodes = [
        node
        for node in raw_nodes
        if isinstance(node, dict)
        and (
            not allowed_categories
            or normalize_lookup_value(node.get("category")) in allowed_categories
        )
    ]
    if not matching_nodes:
        return {}

    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server),
            selectinload(Device.channels),
        )
        .order_by(Device.display_name.asc(), Device.device_id.asc())
        .all()
    )

    exact_lookup: dict[tuple[str, str, str], Device] = {}
    connection_lookup: dict[tuple[str, str], Device] = {}
    ambiguous_connection_keys: set[tuple[str, str]] = set()

    for device in devices:
        binding = device.current_binding
        connection = binding.connection if binding is not None else None
        server = connection.device_server if connection is not None else None
        if binding is None or connection is None or server is None:
            continue

        server_keys = [
            *(server_lookup_values(server.server_code)),
            *(server_lookup_values(server.display_name)),
        ]
        protocol = normalize_lookup_value(device.protocol)
        connection_labels = {
            normalize_lookup_value(connection.connection_label),
            normalize_lookup_value(f"Port {connection.port_number}"),
        }

        for server_key in dict.fromkeys(server_keys):
            for connection_label in connection_labels:
                if not server_key or not connection_label:
                    continue
                if protocol:
                    exact_lookup[(server_key, connection_label, protocol)] = device

                connection_key = (server_key, connection_label)
                if connection_key in ambiguous_connection_keys:
                    continue
                existing = connection_lookup.get(connection_key)
                if existing is None:
                    connection_lookup[connection_key] = device
                elif existing.device_id == device.device_id:
                    connection_lookup[connection_key] = device
                else:
                    connection_lookup.pop(connection_key, None)
                    ambiguous_connection_keys.add(connection_key)

    targets: dict[str, dict[str, Any]] = {}
    for node in matching_nodes:
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue

        communication = node.get("communication") if isinstance(node.get("communication"), dict) else {}
        server_code = str(communication.get("device_server_code") or "").strip()
        connection_label = str(communication.get("connection_label") or "").strip()
        protocol = str(communication.get("protocol") or "").strip()
        note = str(communication.get("notes") or "").strip()

        target = {
            "node_id": node_id,
            "instance_id": str(node.get("instance_id") or "").strip(),
            "label": str(node.get("label") or node.get("symbol_id") or "Element").strip(),
            "symbol_id": str(node.get("symbol_id") or "").strip(),
            "category": str(node.get("category") or "").strip(),
            "server_code": server_code,
            "connection_label": connection_label,
            "protocol": protocol,
            "notes": note,
            "is_resolved": False,
            "resolution_note": "",
            "device_id": None,
            "device_display_name": "",
            "asset_serial": "",
            "device_type": "",
            "is_online": False,
            "quality_state": "",
            "last_seen_at": None,
            "port_number": None,
            "channels": [],
        }

        normalized_servers = server_lookup_values(server_code)
        normalized_connection = normalize_lookup_value(connection_label)
        normalized_protocol = normalize_lookup_value(protocol)

        device = None
        if normalized_servers and normalized_connection and normalized_protocol:
            for normalized_server in normalized_servers:
                device = exact_lookup.get((normalized_server, normalized_connection, normalized_protocol))
                if device is not None:
                    break

        if device is None and normalized_servers and normalized_connection:
            for normalized_server in normalized_servers:
                device = connection_lookup.get((normalized_server, normalized_connection))
                if device is not None:
                    if normalized_protocol:
                        target["resolution_note"] = "Resolved by server and connection mapping."
                    break

        if device is None:
            if not normalized_servers or not normalized_connection:
                target["resolution_note"] = "The communication mapping for this flowsheet element is still incomplete."
            else:
                target["resolution_note"] = "No bound device was found for this mapping."
            targets[node_id] = target
            continue

        binding = device.current_binding
        connection = binding.connection if binding is not None else None
        server = connection.device_server if connection is not None else None
        channels = sorted(
            (
                channel
                for channel in device.channels
                if bool(channel.is_active) and str(channel.value_type or "").strip().lower() != "text"
            ),
            key=lambda channel: (
                str(channel.display_name or channel.channel_code or "").strip().lower(),
                str(channel.channel_code or "").strip().lower(),
            ),
        )
        resolved_protocol = device.protocol
        resolved_symbol_id = str(node.get("symbol_id") or "").strip()
        channel_payload = [
            {
                "channel_id": channel.channel_id,
                "channel_code": channel.channel_code,
                "display_name": channel.display_name,
                "unit": channel.unit,
                "value_type": channel.value_type,
                "data_source": "measurement",
            }
            for channel in channels
        ]
        known_channel_codes = {str(item.get("channel_code") or "").strip().lower() for item in channel_payload}
        for expected_channel in default_measurement_plot_channels_for_target(
            symbol_id=resolved_symbol_id,
            protocol=resolved_protocol,
        ):
            expected_code = str(expected_channel.get("channel_code") or "").strip().lower()
            if expected_code and expected_code not in known_channel_codes:
                channel_payload.append(expected_channel)
                known_channel_codes.add(expected_code)
        target.update(
            {
                "server_code": server.server_code if server is not None else server_code,
                "connection_label": (
                    (connection.connection_label or f"Port {connection.port_number}")
                    if connection is not None
                    else connection_label
                ),
                "port_number": connection.port_number if connection is not None else None,
                "protocol": resolved_protocol,
                "is_resolved": True,
                "device_id": device.device_id,
                "device_display_name": device.display_name,
                "asset_serial": device.asset_serial,
                "device_type": device.device_type,
                "is_online": bool(binding.is_online) if binding is not None else False,
                "quality_state": binding.quality_state if binding is not None and binding.quality_state else "",
                "last_seen_at": _dt(binding.last_seen_at if binding is not None else None),
                "channels": channel_payload,
            }
        )
        targets[node_id] = target

    return targets


def resolve_process_device_targets(
    item: ReactorBuild | None,
    *,
    categories: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    definition = item.definition_json if item is not None and isinstance(item.definition_json, dict) else {}
    return resolve_process_device_targets_for_definition(definition, categories=categories)


def resolve_process_manual_targets(item: ReactorBuild | None) -> dict[str, dict[str, Any]]:
    return resolve_process_device_targets(item, categories={"actuators"})
