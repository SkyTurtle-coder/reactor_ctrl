from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, render_template
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import ControlCommand, Device, DeviceBindingCurrent, DeviceConnection, DeviceServer
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
    if normalized in {"success", "succeeded", "completed", "ok", "online", "active", "configured"}:
        return "badge-success"
    if normalized in {"queued", "pending", "running", "processing"}:
        return "badge-warning"
    if normalized in {"error", "failed", "offline", "disabled"}:
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


def _dashboard_summary() -> dict[str, int]:
    devices_total = db.session.query(func.count(Device.device_id)).scalar() or 0
    device_servers_total = db.session.query(func.count(DeviceServer.device_server_id)).scalar() or 0
    connections_total = db.session.query(func.count(DeviceConnection.connection_id)).scalar() or 0
    enabled_connections_total = (
        db.session.query(func.count(DeviceConnection.connection_id))
        .filter(DeviceConnection.is_enabled.is_(True))
        .scalar()
        or 0
    )
    bindings_total = db.session.query(func.count(DeviceBindingCurrent.device_id)).scalar() or 0
    online_devices_total = (
        db.session.query(func.count(DeviceBindingCurrent.device_id))
        .filter(DeviceBindingCurrent.is_online.is_(True))
        .scalar()
        or 0
    )
    return {
        "devices_total": devices_total,
        "device_servers_total": device_servers_total,
        "connections_total": connections_total,
        "enabled_connections_total": enabled_connections_total,
        "bindings_total": bindings_total,
        "online_devices_total": online_devices_total,
    }


@web_bp.get("/")
def index() -> str:
    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server)
        )
        .order_by(Device.display_name.asc(), Device.device_id.asc())
        .limit(6)
        .all()
    )
    connections = (
        DeviceConnection.query.options(
            joinedload(DeviceConnection.device_server),
            joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
        )
        .order_by(DeviceConnection.connection_id.asc())
        .limit(6)
        .all()
    )
    recent_commands = (
        ControlCommand.query.options(joinedload(ControlCommand.device))
        .order_by(ControlCommand.requested_at.desc(), ControlCommand.command_id.desc())
        .limit(8)
        .all()
    )
    quickstart_endpoints = [
        {"label": "/health", "description": "Flask-Prozess und Webschicht pruefen."},
        {"label": "/health/db", "description": "DB-Erreichbarkeit gegen MySQL pruefen."},
        {"label": "/api/", "description": "API-Wurzel mit Ressourcenuebersicht."},
        {"label": "/api/device-protocols", "description": "Unterstuetzte RS-232-Protokolle anzeigen."},
    ]
    return render_template(
        "index.html",
        active_page="dashboard",
        summary=_dashboard_summary(),
        devices=devices,
        connections=connections,
        recent_commands=recent_commands,
        quickstart_endpoints=quickstart_endpoints,
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
        "completed": sum(1 for item in commands if item.status == "completed"),
        "failed": sum(1 for item in commands if item.status == "failed"),
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
