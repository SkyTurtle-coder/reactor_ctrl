from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import ControlCommand, Device, DeviceBindingCurrent, DeviceConnection, DeviceServer, Measurement
from .services.drivers import list_supported_protocols


web_bp = Blueprint("web", __name__)


def _mask_database_url(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url

    scheme, tail = url.split("://", 1)
    userinfo, hostinfo = tail.split("@", 1)
    if ":" not in userinfo:
        return f"{scheme}://{userinfo}@{hostinfo}"

    username, _password = userinfo.split(":", 1)
    return f"{scheme}://{username}:***@{hostinfo}"


def _format_datetime(value) -> str:
    if value is None:
        return "n/a"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _status_badge_class(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "succeeded", "completed", "acked", "ok", "online", "active", "configured"}:
        return "badge-success"
    if normalized in {"queued", "pending", "running", "processing", "sent"}:
        return "badge-warning"
    if normalized in {"error", "failed", "timeout", "offline", "disabled"}:
        return "badge-danger"
    return "badge-muted"


def _bool_badge_class(value: bool) -> str:
    return "badge-success" if value else "badge-muted"


@web_bp.app_context_processor
def inject_layout_helpers() -> dict[str, Any]:
    return {
        "format_datetime": _format_datetime,
        "status_badge_class": _status_badge_class,
        "bool_badge_class": _bool_badge_class,
    }


def _base_context() -> dict[str, Any]:
    database_url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    return {
        "database_url": _mask_database_url(database_url),
        "api_auth_required": current_app.config.get("API_AUTH_REQUIRED", True),
        "supported_protocols": list_supported_protocols(),
    }


def _load_flowsheet_library() -> list[dict[str, Any]]:
    manifest_path = current_app.static_folder
    if manifest_path is None:
        return []

    try:
        manifest_file = Path(manifest_path) / "flowsheet" / "library" / "manifest.json"
        if not manifest_file.exists():
            return []
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current_app.logger.exception("Failed to load flowsheet library manifest.")
        return []

    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list):
        return []

    normalized_symbols: list[dict[str, Any]] = []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        svg_file = str(item.get("svg_file", "")).strip()
        if not svg_file:
            continue
        normalized = dict(item)
        normalized["svg_url"] = f"{current_app.static_url_path}/flowsheet/library/{svg_file}"
        normalized_symbols.append(normalized)
    return normalized_symbols


def _control_summary() -> dict[str, int]:
    reactors_total = db.session.query(func.count(Device.device_id)).scalar() or 0
    configured_bindings_total = db.session.query(func.count(DeviceBindingCurrent.device_id)).scalar() or 0
    online_devices_total = (
        db.session.query(func.count(DeviceBindingCurrent.device_id))
        .filter(DeviceBindingCurrent.is_online.is_(True))
        .scalar()
        or 0
    )
    measurements_total = db.session.query(func.count(Measurement.measurement_id)).scalar() or 0
    alerts_total = (
        (db.session.query(func.count(ControlCommand.command_id)).filter(ControlCommand.status.in_(("failed", "timeout"))).scalar() or 0)
        + (db.session.query(func.count(DeviceConnection.connection_id)).filter(DeviceConnection.last_error.is_not(None)).scalar() or 0)
    )
    return {
        "reactors_total": reactors_total,
        "configured_bindings_total": configured_bindings_total,
        "online_devices_total": online_devices_total,
        "measurements_total": measurements_total,
        "alerts_total": alerts_total,
    }


@web_bp.get("/")
def index() -> str:
    summary = _control_summary()
    feature_tiles = [
        {
            "title": "View Process",
            "endpoint": "web.process_view",
            "eyebrow": "Live",
            "description": "Zeigt den laufenden Prozess, aktive Reaktoren, letzte Messwerte und aktuelle Ausfuehrungen.",
            "stats": [
                {"label": "Online", "value": summary["online_devices_total"]},
                {"label": "Measurements", "value": summary["measurements_total"]},
            ],
        },
        {
            "title": "Recipes",
            "endpoint": "web.recipes_view",
            "eyebrow": "Steuerung",
            "description": "Rezepte fuer die Reaktorsteuerung anlegen, versionieren und spaeter gezielt ausfuehren.",
            "stats": [
                {"label": "Status", "value": "Planned"},
                {"label": "Scope", "value": "Batch"},
            ],
        },
        {
            "title": "Alerts",
            "endpoint": "web.alerts_view",
            "eyebrow": "Sicherheit",
            "description": "Alle anliegenden Fehler, Kommunikationsprobleme und Command-Abweichungen zentral sichtbar machen.",
            "stats": [
                {"label": "Open", "value": summary["alerts_total"]},
                {"label": "Focus", "value": "Live"},
            ],
        },
        {
            "title": "Reactor Builder",
            "endpoint": "web.reactor_builder_view",
            "eyebrow": "Setup",
            "description": "Reaktoren mit Aktoren, Sensoren, Ports und Kommunikationspfaden kontrolliert konfigurieren.",
            "stats": [
                {"label": "Reactors", "value": summary["reactors_total"]},
                {"label": "Bindings", "value": summary["configured_bindings_total"]},
            ],
        },
    ]
    return render_template(
        "index.html",
        active_page="home",
        summary=summary,
        feature_tiles=feature_tiles,
        **_base_context(),
    )


@web_bp.get("/process")
def process_view() -> str:
    process_title = (request.args.get("title") or "").strip() or "No Active Process"
    process_step = (request.args.get("step") or "").strip() or "Step pending from recipe"
    return render_template(
        "process.html",
        active_page="process",
        summary=_control_summary(),
        process_title=process_title,
        process_step=process_step,
        **_base_context(),
    )


@web_bp.get("/recipes")
def recipes_view() -> str:
    recipe_sections = [
        {
            "title": "Recipe Library",
            "text": "Hier entsteht die zentrale Rezeptbibliothek fuer Prozessschritte, Sollwerte und Ablaufdefinitionen.",
        },
        {
            "title": "Versioning",
            "text": "Rezeptstaende sollen spaeter nachvollziehbar freigegeben, geaendert und pro Lauf dokumentiert werden.",
        },
        {
            "title": "Execution Profiles",
            "text": "Die Oberflaeche ist fuer Batch-, Halte- und Regelphasen vorgesehen, bleibt aktuell aber bewusst schlank.",
        },
    ]
    return render_template(
        "recipes.html",
        active_page="recipes",
        recipe_sections=recipe_sections,
        **_base_context(),
    )


@web_bp.get("/alerts")
def alerts_view() -> str:
    command_alerts = (
        ControlCommand.query.options(joinedload(ControlCommand.device))
        .filter(ControlCommand.status.in_(("failed", "timeout")))
        .order_by(ControlCommand.requested_at.desc(), ControlCommand.command_id.desc())
        .limit(20)
        .all()
    )
    connection_alerts = (
        DeviceConnection.query.options(
            joinedload(DeviceConnection.device_server),
            joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
        )
        .filter(DeviceConnection.last_error.is_not(None))
        .order_by(DeviceConnection.updated_at.desc(), DeviceConnection.connection_id.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "alerts.html",
        active_page="alerts",
        command_alerts=command_alerts,
        connection_alerts=connection_alerts,
        summary=_control_summary(),
        **_base_context(),
    )


@web_bp.get("/reactor-builder")
def reactor_builder_view() -> str:
    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server)
        )
        .order_by(Device.display_name.asc(), Device.device_id.asc())
        .all()
    )
    connections = (
        DeviceConnection.query.options(
            joinedload(DeviceConnection.device_server),
            joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
        )
        .order_by(DeviceConnection.connection_id.asc())
        .all()
    )
    servers = (
        DeviceServer.query.options(selectinload(DeviceServer.connections))
        .order_by(DeviceServer.display_name.asc(), DeviceServer.device_server_id.asc())
        .all()
    )
    return render_template(
        "reactor_builder.html",
        active_page="reactor_builder",
        devices=devices,
        connections=connections,
        servers=servers,
        symbol_library=_load_flowsheet_library(),
        summary=_control_summary(),
        **_base_context(),
    )


@web_bp.get("/devices")
def devices_overview() -> str:
    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server)
        )
        .order_by(Device.display_name.asc(), Device.device_id.asc())
        .all()
    )
    summary = {
        "total": len(devices),
        "active": sum(1 for item in devices if bool(item.is_active)),
        "bound": sum(1 for item in devices if item.current_binding is not None),
        "online": sum(1 for item in devices if item.current_binding and item.current_binding.is_online),
    }
    return render_template(
        "devices.html",
        active_page="devices",
        devices=devices,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/device-servers")
def device_servers_overview() -> str:
    servers = (
        DeviceServer.query.options(selectinload(DeviceServer.connections))
        .order_by(DeviceServer.display_name.asc(), DeviceServer.device_server_id.asc())
        .all()
    )
    summary = {
        "total": len(servers),
        "active": sum(1 for item in servers if bool(item.is_active)),
        "configured_ports": sum(int(item.port_count or 0) for item in servers),
        "defined_connections": sum(len(item.connections) for item in servers),
    }
    return render_template(
        "device_servers.html",
        active_page="device_servers",
        servers=servers,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/device-connections")
def device_connections_overview() -> str:
    connections = (
        DeviceConnection.query.options(
            joinedload(DeviceConnection.device_server),
            joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
        )
        .order_by(DeviceConnection.connection_id.asc())
        .all()
    )
    summary = {
        "total": len(connections),
        "enabled": sum(1 for item in connections if bool(item.is_enabled)),
        "bound": sum(1 for item in connections if item.current_binding is not None),
        "recently_seen": sum(1 for item in connections if item.last_seen_at is not None),
    }
    return render_template(
        "device_connections.html",
        active_page="device_connections",
        connections=connections,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/commands")
def commands_overview() -> str:
    commands = (
        ControlCommand.query.options(joinedload(ControlCommand.device))
        .order_by(ControlCommand.requested_at.desc(), ControlCommand.command_id.desc())
        .limit(50)
        .all()
    )
    summary = {
        "total": len(commands),
        "queued": sum(1 for item in commands if item.status == "queued"),
        "acked": sum(1 for item in commands if item.status == "acked"),
        "failed": sum(1 for item in commands if item.status in {"failed", "timeout"}),
    }
    return render_template(
        "commands.html",
        active_page="commands",
        commands=commands,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@web_bp.get("/health/db")
def health_db():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "reachable"})
    except Exception as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "database": "unreachable",
                    "details": str(exc),
                }
            ),
            500,
        )
