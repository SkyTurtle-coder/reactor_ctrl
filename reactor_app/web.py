from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, make_response, render_template, request, url_for
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload

from .actuator_profiles import list_actuator_profiles
from .builder_auth import PROCESS_MANUAL_WRITE_SCOPE, REACTOR_BUILDER_WRITE_SCOPE, create_scoped_token
from .extensions import db
from .flowsheet_library import group_flowsheet_library, load_flowsheet_library
from .models import ControlCommand, Device, DeviceBindingCurrent, DeviceConnection, DeviceServer, Measurement, ReactorBuild
from .services.drivers import list_supported_protocol_options, list_supported_protocols, protocol_label


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


def _dt(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


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
    def static_asset(filename: str) -> str:
        asset_url = url_for("static", filename=filename)
        static_root = current_app.static_folder
        if not static_root:
            return asset_url

        asset_path = Path(static_root) / filename
        try:
            version = int(asset_path.stat().st_mtime)
        except OSError:
            return asset_url
        separator = "&" if "?" in asset_url else "?"
        return f"{asset_url}{separator}v={version}"

    return {
        "format_datetime": _format_datetime,
        "status_badge_class": _status_badge_class,
        "bool_badge_class": _bool_badge_class,
        "format_protocol_label": protocol_label,
        "static_asset": static_asset,
    }


def _base_context() -> dict[str, Any]:
    database_url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    return {
        "database_url": _mask_database_url(database_url),
        "api_auth_required": current_app.config.get("API_AUTH_REQUIRED", True),
        "supported_protocols": list_supported_protocols(),
        "supported_protocol_options": list_supported_protocol_options(),
    }


def _reactor_build_summary_to_dict(item: ReactorBuild) -> dict[str, Any]:
    definition = item.definition_json if isinstance(item.definition_json, dict) else {}
    nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    return {
        "reactor_build_id": item.reactor_build_id,
        "build_name": item.build_name,
        "build_date": item.build_date.isoformat() if item.build_date else "",
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "updated_at": _dt(item.updated_at),
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
    }


def _reactor_build_detail_to_dict(item: ReactorBuild | None) -> dict[str, Any] | None:
    if item is None:
        return None
    payload = _reactor_build_summary_to_dict(item)
    payload["definition_json"] = item.definition_json if isinstance(item.definition_json, dict) else {}
    payload["notes"] = item.notes
    payload["is_active"] = item.is_active
    payload["created_at"] = _dt(item.created_at)
    return payload


def _normalized_lookup_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _resolve_process_manual_targets(item: ReactorBuild | None) -> dict[str, dict[str, Any]]:
    definition = item.definition_json if item is not None and isinstance(item.definition_json, dict) else {}
    raw_nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    if not isinstance(raw_nodes, list):
        return {}

    actuator_nodes = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and _normalized_lookup_value(node.get("category")) == "actuators"
    ]
    if not actuator_nodes:
        return {}

    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server)
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

        server_code = _normalized_lookup_value(server.server_code)
        protocol = _normalized_lookup_value(device.protocol)
        connection_labels = {
            _normalized_lookup_value(connection.connection_label),
            _normalized_lookup_value(f"Port {connection.port_number}"),
        }

        for connection_label in connection_labels:
            if not server_code or not connection_label:
                continue
            if protocol:
                exact_lookup[(server_code, connection_label, protocol)] = device

            connection_key = (server_code, connection_label)
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
    for node in actuator_nodes:
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
            "symbol_id": str(node.get("symbol_id") or "").strip(),
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
        }

        normalized_server = _normalized_lookup_value(server_code)
        normalized_connection = _normalized_lookup_value(connection_label)
        normalized_protocol = _normalized_lookup_value(protocol)

        device = None
        if normalized_server and normalized_connection and normalized_protocol:
            device = exact_lookup.get((normalized_server, normalized_connection, normalized_protocol))

        if device is None and normalized_server and normalized_connection:
            device = connection_lookup.get((normalized_server, normalized_connection))
            if device is not None and normalized_protocol:
                target["resolution_note"] = "Resolved by server and connection mapping."

        if device is None:
            if not normalized_server or not normalized_connection:
                target["resolution_note"] = "The communication mapping for this actuator is still incomplete."
            else:
                target["resolution_note"] = "No bound device was found for this mapping."
            targets[node_id] = target
            continue

        binding = device.current_binding
        connection = binding.connection if binding is not None else None
        server = connection.device_server if connection is not None else None
        target.update(
            {
                "server_code": server.server_code if server is not None else server_code,
                "connection_label": (
                    (connection.connection_label or f"Port {connection.port_number}")
                    if connection is not None
                    else connection_label
                ),
                "protocol": device.protocol,
                "is_resolved": True,
                "device_id": device.device_id,
                "device_display_name": device.display_name,
                "asset_serial": device.asset_serial,
                "device_type": device.device_type,
                "is_online": bool(binding.is_online) if binding is not None else False,
                "quality_state": binding.quality_state if binding is not None and binding.quality_state else "",
                "last_seen_at": _dt(binding.last_seen_at if binding is not None else None),
            }
        )
        targets[node_id] = target

    return targets


def _control_summary() -> dict[str, int]:
    try:
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
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Control summary fallback activated because database access failed: %s", exc)
        return {
            "reactors_total": 0,
            "configured_bindings_total": 0,
            "online_devices_total": 0,
            "measurements_total": 0,
            "alerts_total": 0,
        }


def _run_web_query(loader, *, fallback, log_label: str, notice: str):
    try:
        return loader(), None
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("%s fallback activated: %s", log_label, exc)
        return fallback, notice


@web_bp.get("/")
def software_portal() -> str:
    software_tiles = [
        {
            "title": "Reactor Control System",
            "href": url_for("web.index"),
            "eyebrow": "Process Control",
            "description": "Steuerung, Monitoring, Recipes, Alerts und Reactor Builder fuer das Reaktorsystem.",
            "stats": [
                {"label": "Area", "value": "Reactor"},
                {"label": "Mode", "value": "Control"},
            ],
            "cta": "Open software",
        },
        {
            "title": "InfraredCamera",
            "href": url_for("web.infrared_camera_home"),
            "eyebrow": "Imaging",
            "description": "Zugang zur Software fuer die Infrarotkamera und ihre Auswertung.",
            "stats": [
                {"label": "Area", "value": "Camera"},
                {"label": "Mode", "value": "Thermal"},
            ],
            "cta": "Open software",
        },
    ]
    return render_template(
        "software_portal.html",
        active_page="software_portal",
        software_tiles=software_tiles,
    )


@web_bp.get("/reactor-control-system")
@web_bp.get("/reactor-control")
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
            "description": "Rezepte zentral verwalten, freigeben und fuer reproduzierbare Prozessablaeufe bereitstellen.",
            "stats": [
                {"label": "Status", "value": "Library"},
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


@web_bp.get("/infrared-camera")
def infrared_camera_home() -> str:
    return render_template(
        "infrared_camera.html",
        active_page="infrared_camera",
    )


@web_bp.get("/process")
def process_view():
    build_id = request.args.get("build_id", type=int)
    saved_builds: list[ReactorBuild] = []
    current_build = None
    selected_build_missing = False
    process_notice = None
    process_storage_available = True
    try:
        saved_builds = (
            ReactorBuild.query.order_by(ReactorBuild.updated_at.desc(), ReactorBuild.reactor_build_id.desc()).all()
        )
        current_build = db.session.get(ReactorBuild, build_id) if build_id else None
        selected_build_missing = build_id is not None and current_build is None
    except Exception as exc:
        db.session.rollback()
        process_storage_available = False
        process_notice = (
            "Flowsheets could not be loaded. The process page is still available, "
            "but selection and manual control are currently limited."
        )
        current_app.logger.exception("Process view database fallback activated: %s", exc)

    manual_targets: dict[str, dict[str, Any]] = {}
    if current_build is not None:
        try:
            manual_targets = _resolve_process_manual_targets(current_build)
        except Exception as exc:
            db.session.rollback()
            if process_notice:
                process_notice = (
                    f"{process_notice} The device mapping for manual control "
                    "could also not be loaded completely."
                )
            else:
                process_notice = (
                    "The device mapping for manual control could not be loaded. "
                    "The flowsheet remains visible."
                )
            current_app.logger.exception("Process view manual target fallback activated: %s", exc)

    manual_write_token = None
    if current_app.config.get("API_AUTH_REQUIRED", True):
        secret_key = current_app.config.get("SECRET_KEY")
        if secret_key:
            manual_write_token = create_scoped_token(
                secret_key,
                scope=PROCESS_MANUAL_WRITE_SCOPE,
                ttl_seconds=current_app.config.get("PROCESS_MANUAL_WRITE_TOKEN_TTL_SECONDS", 43200),
            )

    response = make_response(
        render_template(
            "process.html",
            active_page="process",
            summary=_control_summary(),
            saved_builds=[_reactor_build_summary_to_dict(item) for item in saved_builds],
            selected_build_id=None if current_build is None else current_build.reactor_build_id,
            selected_build=_reactor_build_detail_to_dict(current_build),
            selected_build_missing=selected_build_missing,
            process_notice=process_notice,
            process_storage_available=process_storage_available,
            manual_targets=manual_targets,
            manual_write_token=manual_write_token,
            actuator_profiles=list_actuator_profiles(),
            **_base_context(),
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@web_bp.get("/recipes")
def recipes_view() -> str:
    recipe_sections = [
        {
            "title": "Recipe Library",
            "text": "Zentrale Ablage fuer freigegebene Rezeptdefinitionen mit einheitlichen Namen, Parametern und Prozessphasen.",
        },
        {
            "title": "Versioning",
            "text": "Freigabestaende und Aenderungen bleiben nachvollziehbar, damit jeder Lauf auf einen klaren Rezeptstand verweist.",
        },
        {
            "title": "Execution Profiles",
            "text": "Batch-, Halte- und Regelphasen werden in einer gemeinsamen Struktur fuer Bedienung und Dokumentation zusammengefuehrt.",
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
    page_notice = None
    command_alerts, notice = _run_web_query(
        lambda: (
            ControlCommand.query.options(joinedload(ControlCommand.device))
            .filter(ControlCommand.status.in_(("failed", "timeout")))
            .order_by(ControlCommand.requested_at.desc(), ControlCommand.command_id.desc())
            .limit(20)
            .all()
        ),
        fallback=[],
        log_label="Alerts command query",
        notice="Alerts konnten nicht vollstaendig geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch keine Live-Daten.",
    )
    if notice:
        page_notice = notice

    connection_alerts, notice = _run_web_query(
        lambda: (
            DeviceConnection.query.options(
                joinedload(DeviceConnection.device_server),
                joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
            )
            .filter(DeviceConnection.last_error.is_not(None))
            .order_by(DeviceConnection.updated_at.desc(), DeviceConnection.connection_id.desc())
            .limit(20)
            .all()
        ),
        fallback=[],
        log_label="Alerts connection query",
        notice="Alerts konnten nicht vollstaendig geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch keine Live-Daten.",
    )
    if notice:
        page_notice = page_notice or notice

    return render_template(
        "alerts.html",
        active_page="alerts",
        command_alerts=command_alerts,
        connection_alerts=connection_alerts,
        page_notice=page_notice,
        page_notice_tone="error" if page_notice else None,
        summary=_control_summary(),
        **_base_context(),
    )


@web_bp.get("/reactor-builder")
def reactor_builder_view() -> str:
    symbol_library = load_flowsheet_library(
        static_folder=current_app.static_folder,
        static_url_path=current_app.static_url_path,
    )
    build_id = request.args.get("build_id", type=int)
    saved_builds: list[ReactorBuild] = []
    current_build = None
    builder_notice = None
    builder_storage_available = True
    try:
        saved_builds = (
            ReactorBuild.query.order_by(ReactorBuild.updated_at.desc(), ReactorBuild.reactor_build_id.desc()).all()
        )
        current_build = db.session.get(ReactorBuild, build_id) if build_id else None
    except Exception as exc:
        db.session.rollback()
        builder_storage_available = False
        builder_notice = (
            "Gespeicherte Builds konnten nicht geladen werden. Die Library bleibt verfuegbar, "
            "Speichern und Laden sind derzeit jedoch eingeschraenkt."
        )
        current_app.logger.exception("Reactor Builder database fallback activated: %s", exc)

    builder_name = (
        current_build.build_name
        if current_build is not None
        else (request.args.get("name") or "").strip() or "Untitled Reactor Build"
    )
    builder_user = (
        current_build.updated_by or current_build.created_by
        if current_build is not None
        else (request.args.get("user") or "").strip() or "operator"
    )
    builder_date = (
        current_build.build_date.isoformat()
        if current_build is not None and current_build.build_date is not None
        else (request.args.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    )
    builder_write_token = None
    if builder_storage_available and current_app.config.get("API_AUTH_REQUIRED", True):
        secret_key = current_app.config.get("SECRET_KEY")
        if secret_key:
            builder_write_token = create_scoped_token(
                secret_key,
                scope=REACTOR_BUILDER_WRITE_SCOPE,
                ttl_seconds=current_app.config.get("BUILDER_WRITE_TOKEN_TTL_SECONDS", 43200),
            )

    return render_template(
        "reactor_builder.html",
        active_page="reactor_builder",
        current_build_id=None if current_build is None else current_build.reactor_build_id,
        current_build=_reactor_build_detail_to_dict(current_build),
        builder_notice=builder_notice,
        builder_storage_available=builder_storage_available,
        builder_write_token=builder_write_token,
        builder_name=builder_name,
        builder_user=builder_user,
        builder_date=builder_date,
        library_symbol_total=len(symbol_library),
        symbol_categories=group_flowsheet_library(symbol_library),
        actuator_profiles=list_actuator_profiles(),
        saved_builds=[_reactor_build_summary_to_dict(item) for item in saved_builds],
        summary=_control_summary(),
        **_base_context(),
    )


@web_bp.get("/devices")
def devices_overview() -> str:
    devices, page_notice = _run_web_query(
        lambda: (
            Device.query.options(
                joinedload(Device.current_binding)
                .joinedload(DeviceBindingCurrent.connection)
                .joinedload(DeviceConnection.device_server)
            )
            .order_by(Device.display_name.asc(), Device.device_id.asc())
            .all()
        ),
        fallback=[],
        log_label="Devices overview query",
        notice="Geraete konnten nicht geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch keine Inventardaten.",
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
        page_notice=page_notice,
        page_notice_tone="error" if page_notice else None,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/device-servers")
def device_servers_overview() -> str:
    servers, page_notice = _run_web_query(
        lambda: (
            DeviceServer.query.options(selectinload(DeviceServer.connections))
            .order_by(DeviceServer.display_name.asc(), DeviceServer.device_server_id.asc())
            .all()
        ),
        fallback=[],
        log_label="Device servers overview query",
        notice="Device-Server konnten nicht geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch keine Infrastrukturdaten.",
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
        page_notice=page_notice,
        page_notice_tone="error" if page_notice else None,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/device-connections")
def device_connections_overview() -> str:
    connections, page_notice = _run_web_query(
        lambda: (
            DeviceConnection.query.options(
                joinedload(DeviceConnection.device_server),
                joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
            )
            .order_by(DeviceConnection.connection_id.asc())
            .all()
        ),
        fallback=[],
        log_label="Device connections overview query",
        notice="Device-Verbindungen konnten nicht geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch keine Transportdaten.",
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
        page_notice=page_notice,
        page_notice_tone="error" if page_notice else None,
        summary=summary,
        **_base_context(),
    )


@web_bp.get("/commands")
def commands_overview() -> str:
    commands, page_notice = _run_web_query(
        lambda: (
            ControlCommand.query.options(joinedload(ControlCommand.device))
            .order_by(ControlCommand.requested_at.desc(), ControlCommand.command_id.desc())
            .limit(50)
            .all()
        ),
        fallback=[],
        log_label="Commands overview query",
        notice="Steuerbefehle konnten nicht geladen werden. Die Seite bleibt erreichbar, zeigt derzeit jedoch kein Command-Log.",
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
        page_notice=page_notice,
        page_notice_tone="error" if page_notice else None,
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
        db.session.rollback()
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
