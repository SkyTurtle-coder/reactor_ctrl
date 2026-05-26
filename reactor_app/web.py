from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, url_for
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload

from .actuator_profiles import list_actuator_profiles
from .builder_auth import PROCESS_MANUAL_WRITE_SCOPE, REACTOR_BUILDER_WRITE_SCOPE, RECIPE_WRITE_SCOPE, create_scoped_token
from .extensions import db
from .flowsheet_library import group_flowsheet_library, load_flowsheet_library
from .models import ControlCommand, Device, DeviceBindingCurrent, DeviceConnection, DeviceServer, Measurement, ReactorBuild, Recipe
from .process_targets import (
    normalize_lookup_value as _normalized_lookup_value,
    resolve_process_device_targets as _resolve_process_device_targets,
)
from .services.activity_log import load_activity_logs, severity_badge_class, summarize_activity_logs
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
        "log_severity_badge_class": severity_badge_class,
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


def _append_notice(existing: str | None, addition: str | None) -> str | None:
    text = str(addition or "").strip()
    if not text:
        return existing
    if not existing:
        return text
    if text in existing:
        return existing
    return f"{existing} {text}"


@web_bp.get("/")
def index():
    return redirect(url_for("web.process_view"), code=302)


@web_bp.get("/reactor-control-system")
@web_bp.get("/reactor-control")
def legacy_reactor_control_home():
    return redirect(url_for("web.process_view"), code=302)


@web_bp.get("/infrared-camera")
def legacy_infrared_camera_home():
    return redirect(url_for("web.process_view"), code=302)


@web_bp.get("/process")
def process_view():
    build_id = request.args.get("build_id", type=int)
    recipe_id = request.args.get("recipe_id", type=int)
    requested_mode = str(request.args.get("mode") or "").strip().lower()
    saved_builds: list[ReactorBuild] = []
    saved_recipes: list[dict[str, Any]] = []
    current_build = None
    current_recipe = None
    selected_build_missing = False
    selected_recipe_missing = False
    process_notice = None
    process_storage_available = True
    recipe_selection_available = True
    selection_mode = "recipe" if requested_mode == "recipe" or recipe_id else "build"
    try:
        saved_builds = (
            ReactorBuild.query.order_by(ReactorBuild.updated_at.desc(), ReactorBuild.reactor_build_id.desc()).all()
        )
        if selection_mode == "build":
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

    try:
        recipe_items = Recipe.query.order_by(Recipe.updated_at.desc(), Recipe.recipe_id.desc()).all()
        saved_recipes = [
            {
                "recipe_id": item.recipe_id,
                "title": item.title,
                "operator_name": item.operator_name,
                "reactor_build_id": item.reactor_build_id,
                "status": item.status,
                "updated_by": item.updated_by,
                "created_by": item.created_by,
                "updated_at": _dt(item.updated_at),
            }
            for item in recipe_items
        ]
        if recipe_id is not None:
            current_recipe = db.session.get(Recipe, recipe_id)
            selected_recipe_missing = current_recipe is None
            if current_recipe is not None and current_recipe.reactor_build_id is not None:
                current_build = db.session.get(ReactorBuild, current_recipe.reactor_build_id)
                if current_build is None:
                    process_notice = _append_notice(
                        process_notice,
                        "The selected recipe is linked to a flowsheet that could not be loaded.",
                    )
    except Exception as exc:
        db.session.rollback()
        recipe_selection_available = False
        if selection_mode == "recipe":
            current_recipe = None
            current_build = None
        process_notice = _append_notice(
            process_notice,
            "Recipes could not be loaded. Build mode remains available.",
        )
        current_app.logger.exception("Process view recipe fallback activated: %s", exc)

    manual_targets: dict[str, dict[str, Any]] = {}
    plot_targets: dict[str, dict[str, Any]] = {}
    if current_build is not None:
        try:
            plot_targets = _resolve_process_device_targets(current_build, categories={"actuators", "sensors"})
            manual_targets = {
                node_id: target
                for node_id, target in plot_targets.items()
                if _normalized_lookup_value(target.get("category")) == "actuators"
            }
        except Exception as exc:
            db.session.rollback()
            if process_notice:
                process_notice = (
                    f"{process_notice} The device mapping for manual control and plotting "
                    "could also not be loaded completely."
                )
            else:
                process_notice = (
                    "The device mapping for manual control and plotting could not be loaded. "
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
            saved_recipes=saved_recipes,
            selected_recipe_id=None if current_recipe is None else current_recipe.recipe_id,
            selected_recipe=None
            if current_recipe is None
            else {
                "recipe_id": current_recipe.recipe_id,
                "title": current_recipe.title,
                "operator_name": current_recipe.operator_name,
                "status": current_recipe.status,
                "reactor_build_id": current_recipe.reactor_build_id,
                "steps": current_recipe.steps_json if isinstance(current_recipe.steps_json, list) else [],
                "updated_at": _dt(current_recipe.updated_at),
            },
            selected_recipe_missing=selected_recipe_missing,
            process_selection_mode=selection_mode,
            process_notice=process_notice,
            process_storage_available=process_storage_available,
            recipe_selection_available=recipe_selection_available,
            manual_targets=manual_targets,
            plot_targets=plot_targets,
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
def recipes_view():
    recipe_id = request.args.get("recipe_id", type=int)
    saved_recipes: list[dict[str, Any]] = []
    saved_builds: list[dict[str, Any]] = []
    current_recipe = None
    selected_recipe_missing = False
    recipe_storage_available = True
    recipe_notice = None

    try:
        items = Recipe.query.order_by(Recipe.updated_at.desc(), Recipe.recipe_id.desc()).all()
        saved_recipes = [
            {
                "recipe_id": r.recipe_id,
                "title": r.title,
                "operator_name": r.operator_name,
                "version": r.version,
                "status": r.status,
                "updated_by": r.updated_by,
                "created_by": r.created_by,
                "updated_at": _dt(r.updated_at),
            }
            for r in items
        ]
        builds = (
            ReactorBuild.query.order_by(
                ReactorBuild.is_active.desc(),
                ReactorBuild.build_name.asc(),
                ReactorBuild.reactor_build_id.desc(),
            ).all()
        )
        saved_builds = [
            {
                "reactor_build_id": b.reactor_build_id,
                "build_name": b.build_name,
                "build_date": b.build_date.isoformat() if b.build_date else None,
                "updated_by": b.updated_by or b.created_by,
                "is_active": bool(b.is_active),
            }
            for b in builds
        ]
        if recipe_id is not None:
            raw = db.session.get(Recipe, recipe_id)
            selected_recipe_missing = raw is None
            if raw is not None:
                current_recipe = {
                    "recipe_id": raw.recipe_id,
                    "title": raw.title,
                    "operator_name": raw.operator_name,
                    "version": raw.version,
                    "status": raw.status,
                    "reactor_build_id": raw.reactor_build_id,
                    "steps": raw.steps_json if isinstance(raw.steps_json, list) else [],
                    "created_by": raw.created_by,
                    "updated_by": raw.updated_by,
                    "updated_at": _dt(raw.updated_at),
                }
    except Exception as exc:
        db.session.rollback()
        recipe_storage_available = False
        recipe_notice = (
            "Recipes could not be loaded. The database may be unavailable."
        )
        current_app.logger.exception("Recipes view database fallback activated: %s", exc)

    recipe_write_token = None
    if current_app.config.get("API_AUTH_REQUIRED", True):
        secret_key = current_app.config.get("SECRET_KEY")
        if secret_key:
            recipe_write_token = create_scoped_token(
                secret_key,
                scope=RECIPE_WRITE_SCOPE,
                ttl_seconds=current_app.config.get("RECIPE_WRITE_TOKEN_TTL_SECONDS", 43200),
            )

    response = make_response(
        render_template(
            "recipes.html",
            active_page="recipes",
            saved_recipes=saved_recipes,
            saved_builds=saved_builds,
            current_recipe=current_recipe,
            selected_recipe_id=recipe_id if current_recipe is not None else None,
            selected_recipe_missing=selected_recipe_missing,
            recipe_notice=recipe_notice,
            recipe_storage_available=recipe_storage_available,
            recipe_write_token=recipe_write_token,
            **_base_context(),
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@web_bp.get("/alerts")
def alerts_view():
    return redirect(url_for("web.logs_view"), code=302)


@web_bp.get("/logs")
def logs_view() -> str:
    page_notice = None
    retention_days = max(1, int(current_app.config.get("ACTIVITY_LOG_RETENTION_DAYS", 7)))
    activity_logs, notice = _run_web_query(
        lambda: load_activity_logs(days=retention_days, limit=120),
        fallback=[],
        log_label="Logs activity query",
        notice="Logs could not be fully loaded. The page is available but no live data is shown.",
    )
    if notice:
        page_notice = notice

    log_summary = summarize_activity_logs(activity_logs)

    return render_template(
        "logs.html",
        active_page="logs",
        activity_logs=activity_logs,
        log_summary=log_summary,
        retention_days=retention_days,
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
    display_targets: dict[str, dict[str, Any]] = {}
    try:
        saved_builds = (
            ReactorBuild.query.order_by(ReactorBuild.updated_at.desc(), ReactorBuild.reactor_build_id.desc()).all()
        )
        current_build = db.session.get(ReactorBuild, build_id) if build_id else None
    except Exception as exc:
        db.session.rollback()
        builder_storage_available = False
        builder_notice = (
            "Saved builds could not be loaded. The library remains available, "
            "but saving and loading are currently restricted."
        )
        current_app.logger.exception("Reactor Builder database fallback activated: %s", exc)

    if current_build is not None and builder_storage_available:
        try:
            display_targets = _resolve_process_device_targets(current_build, categories={"actuators", "sensors"})
        except Exception as exc:
            db.session.rollback()
            builder_notice = _append_notice(
                builder_notice,
                "Display value targets could not be loaded. The flowsheet editor remains available.",
            )
            current_app.logger.exception("Reactor Builder display target fallback activated: %s", exc)

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
        display_targets=display_targets,
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
        notice="Devices could not be loaded. The page is available but no inventory data is shown.",
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
        notice="Device servers could not be loaded. The page is available but no infrastructure data is shown.",
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
        notice="Device connections could not be loaded. The page is available but no transport data is shown.",
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
        notice="Commands could not be loaded. The page is available but no command log is shown.",
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


# ── Data page ─────────────────────────────────────────────────────────────────

def _safe_csv_filename(value: str) -> str:
    """Return a filesystem-safe ASCII filename component (max 64 chars)."""
    cleaned = re.sub(r"[^\w\-]", "_", str(value or "unknown"), flags=re.ASCII)
    return (cleaned[:64] or "unknown").strip("_") or "unknown"


def _data_filter_cutoff(since_days: int | None) -> datetime | None:
    if since_days is None or since_days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _parse_data_export_params() -> tuple[int | None, int | None]:
    """Parse device_id and since_days from the current request args."""
    device_id_raw = request.args.get("device_id", "").strip()
    since_days_raw = request.args.get("since_days", "").strip()
    device_id = int(device_id_raw) if device_id_raw.isdigit() else None
    since_days = int(since_days_raw) if since_days_raw.isdigit() and int(since_days_raw) > 0 else None
    return device_id, since_days


def _load_data_summary() -> dict:
    row = db.session.query(
        func.count(Measurement.measurement_id).label("total_count"),
        func.count(func.distinct(Measurement.device_id)).label("device_count"),
        func.count(func.distinct(Measurement.channel_code)).label("channel_count"),
        func.min(Measurement.measured_at).label("oldest_at"),
        func.max(Measurement.measured_at).label("newest_at"),
    ).one()
    return {
        "total_count": int(row.total_count or 0),
        "device_count": int(row.device_count or 0),
        "channel_count": int(row.channel_count or 0),
        "oldest_at": row.oldest_at,
        "newest_at": row.newest_at,
    }


def _load_channel_stats() -> list:
    return (
        db.session.query(
            Device.device_id,
            Device.display_name.label("device_name"),
            Measurement.channel_code,
            func.count(Measurement.measurement_id).label("row_count"),
            func.min(Measurement.measured_at).label("oldest_at"),
            func.max(Measurement.measured_at).label("latest_at"),
        )
        .join(Device, Device.device_id == Measurement.device_id)
        .group_by(Device.device_id, Device.display_name, Measurement.channel_code)
        .order_by(Device.display_name.asc(), Measurement.channel_code.asc())
        .all()
    )


@web_bp.get("/data")
def data_overview() -> str:
    _fallback_summary = {
        "total_count": 0, "device_count": 0, "channel_count": 0,
        "oldest_at": None, "newest_at": None,
    }
    summary, notice1 = _run_web_query(
        _load_data_summary,
        fallback=_fallback_summary,
        log_label="Data overview summary query",
        notice="Measurement statistics could not be loaded.",
    )
    channel_stats, notice2 = _run_web_query(
        _load_channel_stats,
        fallback=[],
        log_label="Data channel stats query",
        notice="Channel breakdown could not be loaded.",
    )
    devices, _ = _run_web_query(
        lambda: (
            db.session.query(Device.device_id, Device.display_name)
            .join(Measurement, Measurement.device_id == Device.device_id)
            .group_by(Device.device_id, Device.display_name)
            .order_by(Device.display_name.asc())
            .all()
        ),
        fallback=[],
        log_label="Data devices query",
        notice="",
    )
    notices = [n for n in [notice1, notice2] if n]
    return render_template(
        "data.html",
        active_page="data",
        summary=summary,
        channel_stats=channel_stats,
        devices=devices,
        page_notice=" ".join(notices) if notices else None,
        page_notice_tone="error" if notices else None,
        **_base_context(),
    )


@web_bp.get("/data/count.json")
def data_count_json():
    """Return row count for the current filter — used by the export preview."""
    device_id, since_days = _parse_data_export_params()
    cutoff = _data_filter_cutoff(since_days)
    try:
        q = db.session.query(func.count(Measurement.measurement_id))
        if device_id:
            q = q.filter(Measurement.device_id == device_id)
        if cutoff is not None:
            q = q.filter(Measurement.measured_at >= cutoff)
        total = int(q.scalar() or 0)
        return jsonify({"count": total})
    except Exception:
        db.session.rollback()
        return jsonify({"count": None}), 500


@web_bp.get("/data/export.zip")
def data_export_zip():
    """Stream a ZIP archive containing one CSV file per (device, channel) pair."""
    device_id, since_days = _parse_data_export_params()
    cutoff = _data_filter_cutoff(since_days)

    try:
        rows = (
            db.session.query(
                Device.display_name.label("device_name"),
                Measurement.channel_code,
                Measurement.measured_at,
                Measurement.numeric_value,
                Measurement.text_value,
                Measurement.unit,
                Measurement.quality_score,
                Measurement.source,
            )
            .join(Device, Device.device_id == Measurement.device_id)
            .filter(
                *([Measurement.device_id == device_id] if device_id else []),
                *([Measurement.measured_at >= cutoff] if cutoff is not None else []),
            )
            .order_by(Device.display_name.asc(), Measurement.channel_code.asc(), Measurement.measured_at.asc())
            .all()
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Data export query failed: %s", exc)
        return "Export failed due to a database error.", 500

    # Build in-memory ZIP: one CSV per (device, channel_code) pair.
    csv_buffers: dict[str, io.StringIO] = {}
    csv_writers: dict[str, Any] = {}
    _CSV_HEADER = ["measured_at", "numeric_value", "text_value", "unit", "quality_score", "source"]

    for row in rows:
        key = f"{_safe_csv_filename(row.device_name)}__{_safe_csv_filename(row.channel_code)}"
        if key not in csv_buffers:
            buf: io.StringIO = io.StringIO()
            csv_buffers[key] = buf
            csv_writers[key] = csv.writer(buf, lineterminator="\r\n")
            csv_writers[key].writerow(_CSV_HEADER)
        csv_writers[key].writerow([
            row.measured_at.isoformat() if row.measured_at else "",
            "" if row.numeric_value is None else float(row.numeric_value),
            row.text_value or "",
            row.unit or "",
            "" if row.quality_score is None else float(row.quality_score),
            row.source or "",
        ])

    export_ts = datetime.now(timezone.utc)
    readme_lines = [
        "Measurement Export",
        f"Generated : {export_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Filter    : {'device_id=' + str(device_id) if device_id else 'all devices'}"
                   f"  /  {'last ' + str(since_days) + ' days' if since_days else 'all time'}",
        f"Files     : {len(csv_buffers)}",
        f"Total rows: {sum(len(b.getvalue().splitlines()) - 1 for b in csv_buffers.values())}",
        "",
        "Each CSV file contains data for one channel on one device.",
        "Filename pattern: {device}__{channel}.csv",
        "",
        "Columns:",
        "  measured_at    UTC timestamp (ISO 8601)",
        "  numeric_value  Numeric measurement value",
        "  text_value     Text measurement value",
        "  unit           Physical unit (rpm, degC, ...)",
        "  quality_score  Data quality 0.0-1.0 (may be empty)",
        "  source         Data source (poller, manual, ...)",
    ]

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", "\n".join(readme_lines))
        for filename, buf in sorted(csv_buffers.items()):
            zf.writestr(f"{filename}.csv", buf.getvalue())

    zip_buf.seek(0)
    date_str = export_ts.strftime("%Y-%m-%d")
    suffix = f"_device{device_id}" if device_id else ""
    suffix += f"_last{since_days}d" if since_days else ""
    zip_filename = f"measurements{suffix}_{date_str}.zip"

    response = make_response(zip_buf.read())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f'attachment; filename="{zip_filename}"'
    return response


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
