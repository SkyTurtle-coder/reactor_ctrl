from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from flask import Flask
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload

from ..actuator_profiles import get_default_profile_id
from ..device_limits import IKA_EUROSTAR_60_MAX_RPM
from ..extensions import db
from ..models import Device, DeviceBindingCurrent, DeviceConnection, Recipe, RecipeProgramState, ReactorBuild
from .device_manual_runtime import queue_manual_state_update


_WORKER_EXTENSION_KEY = "recipe_program_reconciler_thread"
_PROGRAM_STATE_ID = 1
_LEASE_STATUS_RUNNING = "running"
_TERMINAL_STATUSES = {"completed", "stopped", "error"}
_NUMERIC_FIELDS = ("temp", "pressure", "rpm")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_isoformat(value: datetime | None) -> str | None:
    normalized = _as_utc_datetime(value)
    return normalized.isoformat() if normalized is not None else None


def _mysql_error_code(exc: OperationalError) -> int | None:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def _is_mysql_record_changed_error(exc: OperationalError) -> bool:
    return _mysql_error_code(exc) == 1020


def _normalized_lookup_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _recipe_program_loop_sleep(app: Flask) -> float:
    milliseconds = max(250, int(app.config.get("RECIPE_PROGRAM_RECONCILER_LOOP_MS", 1000)))
    return milliseconds / 1000.0


def _recipe_program_lease_duration(app: Flask) -> timedelta:
    seconds = max(3, int(app.config.get("RECIPE_PROGRAM_RECONCILER_LEASE_SECONDS", 10)))
    return timedelta(seconds=seconds)


def _is_recipe_actor_node(raw_node: Any) -> bool:
    if not isinstance(raw_node, dict):
        return False
    if _normalized_lookup_value(raw_node.get("category")) == "actuators":
        return True
    control = raw_node.get("control")
    if isinstance(control, dict) and str(control.get("profile_id") or "").strip():
        return True
    return get_default_profile_id(str(raw_node.get("symbol_id") or "").strip()) is not None


def _actor_baseline_state() -> dict[str, float]:
    return {
        "temp": 0.0,
        "pressure": 0.0,
        "rpm": 0.0,
    }


def _copy_global_targets(targets: dict[str, dict[str, float]] | None) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for actor, payload in (targets or {}).items():
        actor_key = str(actor).strip()
        if not actor_key:
            continue
        next_payload = _actor_baseline_state()
        if isinstance(payload, dict):
            for field_name in _NUMERIC_FIELDS:
                raw_value = payload.get(field_name)
                if raw_value in (None, ""):
                    continue
                try:
                    next_payload[field_name] = round(float(raw_value), 2)
                except (TypeError, ValueError):
                    continue
        result[actor_key] = next_payload
    return result


def _step_actor_target(base_targets: dict[str, dict[str, float]], step: dict[str, Any]) -> dict[str, dict[str, float]]:
    next_targets = _copy_global_targets(base_targets)
    actor = str(step.get("actor") or "").strip()
    if not actor:
        return next_targets

    actor_targets = deepcopy(next_targets.get(actor) or _actor_baseline_state())
    for field_name in _NUMERIC_FIELDS:
        raw_value = step.get(field_name)
        if raw_value in (None, ""):
            continue
        actor_targets[field_name] = round(float(raw_value), 2)
    next_targets[actor] = actor_targets
    return next_targets


def _interpolate_targets(
    start_targets: dict[str, dict[str, float]],
    end_targets: dict[str, dict[str, float]],
    *,
    actor: str,
    progress: float,
) -> dict[str, dict[str, float]]:
    ratio = min(1.0, max(0.0, float(progress)))
    current_targets = _copy_global_targets(start_targets)
    current_actor_targets = deepcopy(current_targets.get(actor) or _actor_baseline_state())
    end_actor_targets = deepcopy(end_targets.get(actor) or _actor_baseline_state())
    start_actor_targets = deepcopy(start_targets.get(actor) or _actor_baseline_state())
    for field_name in _NUMERIC_FIELDS:
        start_value = float(start_actor_targets.get(field_name) or 0.0)
        end_value = float(end_actor_targets.get(field_name) or 0.0)
        current_actor_targets[field_name] = round(start_value + ((end_value - start_value) * ratio), 2)
    current_targets[actor] = current_actor_targets
    return current_targets


def _normalize_snapshot_step(raw_step: Any) -> dict[str, Any]:
    payload = raw_step if isinstance(raw_step, dict) else {}
    return {
        "actor": str(payload.get("actor") or "").strip(),
        "task": str(payload.get("task") or "").strip(),
        "delta_time": round(float(payload.get("delta_time") or 0.0), 2),
        "temp": None if payload.get("temp") in (None, "") else round(float(payload.get("temp")), 2),
        "pressure": None if payload.get("pressure") in (None, "") else round(float(payload.get("pressure")), 2),
        "rpm": None if payload.get("rpm") in (None, "") else round(float(payload.get("rpm")), 2),
    }


def _evaluate_program_timeline(
    steps: list[dict[str, Any]],
    *,
    active_step_index: int,
    step_started_at: datetime | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_steps = [_normalize_snapshot_step(step) for step in (steps or []) if isinstance(step, dict)]
    current_time = _as_utc_datetime(now) or _now_utc()
    segment_started_at = _as_utc_datetime(step_started_at) or current_time
    index = max(0, int(active_step_index or 0))

    previous_targets: dict[str, dict[str, float]] = {}
    for completed_step in normalized_steps[:index]:
        previous_targets = _step_actor_target(previous_targets, completed_step)

    while index < len(normalized_steps):
        step = normalized_steps[index]
        actor = str(step.get("actor") or "").strip()
        next_targets = _step_actor_target(previous_targets, step)
        duration_seconds = max(0.0, round(float(step.get("delta_time") or 0.0) * 60.0, 3))
        elapsed_seconds = max(0.0, (current_time - segment_started_at).total_seconds())

        if duration_seconds <= 0:
            previous_targets = next_targets
            index += 1
            continue

        progress = min(1.0, elapsed_seconds / duration_seconds)
        current_targets = _interpolate_targets(previous_targets, next_targets, actor=actor, progress=progress)
        if elapsed_seconds < duration_seconds:
            return {
                "completed": False,
                "active_step_index": index,
                "step_started_at": segment_started_at,
                "active_step": deepcopy(step),
                "next_step": deepcopy(normalized_steps[index + 1]) if index + 1 < len(normalized_steps) else None,
                "step_duration_seconds": duration_seconds,
                "step_elapsed_seconds": elapsed_seconds,
                "step_remaining_seconds": max(0.0, duration_seconds - elapsed_seconds),
                "step_progress": progress,
                "previous_targets": previous_targets,
                "target_targets": next_targets,
                "current_targets": current_targets,
                "total_steps": len(normalized_steps),
            }

        previous_targets = next_targets
        index += 1
        segment_started_at = segment_started_at + timedelta(seconds=duration_seconds)

    return {
        "completed": True,
        "active_step_index": len(normalized_steps),
        "step_started_at": segment_started_at,
        "active_step": None,
        "next_step": None,
        "step_duration_seconds": 0.0,
        "step_elapsed_seconds": 0.0,
        "step_remaining_seconds": 0.0,
        "step_progress": 1.0,
        "previous_targets": previous_targets,
        "target_targets": previous_targets,
        "current_targets": previous_targets,
        "total_steps": len(normalized_steps),
    }


def _build_target_lookup(item: ReactorBuild | None) -> dict[str, dict[str, Any]]:
    definition = item.definition_json if item is not None and isinstance(item.definition_json, dict) else {}
    raw_nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    if not isinstance(raw_nodes, list):
        return {}

    allowed_nodes = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and _is_recipe_actor_node(node)
    ]
    if not allowed_nodes:
        return {}

    devices = (
        Device.query.options(
            joinedload(Device.current_binding)
            .joinedload(DeviceBindingCurrent.connection)
            .joinedload(DeviceConnection.device_server),
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
    for raw_node in allowed_nodes:
        instance_id = str(raw_node.get("instance_id") or "").strip()
        if not instance_id:
            continue

        symbol_id = str(raw_node.get("symbol_id") or "").strip()
        control = raw_node.get("control") if isinstance(raw_node.get("control"), dict) else {}
        communication = raw_node.get("communication") if isinstance(raw_node.get("communication"), dict) else {}
        server_code = str(communication.get("device_server_code") or "").strip()
        connection_label = str(communication.get("connection_label") or "").strip()
        protocol = str(communication.get("protocol") or "").strip()

        target = {
            "actor": instance_id,
            "node_id": str(raw_node.get("id") or "").strip(),
            "label": str(raw_node.get("label") or symbol_id or instance_id).strip(),
            "symbol_id": symbol_id,
            "profile_id": str(control.get("profile_id") or get_default_profile_id(symbol_id) or "").strip(),
            "server_code": server_code,
            "connection_label": connection_label,
            "protocol": protocol,
            "device_id": None,
            "device_display_name": "",
            "is_resolved": False,
            "resolution_note": "",
        }

        normalized_server = _normalized_lookup_value(server_code)
        normalized_connection = _normalized_lookup_value(connection_label)
        normalized_protocol = _normalized_lookup_value(protocol)
        device = None
        if normalized_server and normalized_connection and normalized_protocol:
            device = exact_lookup.get((normalized_server, normalized_connection, normalized_protocol))
        if device is None and normalized_server and normalized_connection:
            device = connection_lookup.get((normalized_server, normalized_connection))

        if device is None:
            target["resolution_note"] = "No bound device was found for this actor."
            targets[instance_id] = target
            continue

        target["device_id"] = device.device_id
        target["device_display_name"] = device.display_name
        target["protocol"] = device.protocol
        target["is_resolved"] = True
        targets[instance_id] = target

    return targets


def _program_snapshot_for_recipe(recipe: Recipe, recipe_build: ReactorBuild) -> dict[str, Any]:
    steps = [_normalize_snapshot_step(step) for step in (recipe.steps_json if isinstance(recipe.steps_json, list) else [])]
    if not steps:
        raise ValueError("The selected recipe does not contain any steps.")

    bindings_by_actor = _build_target_lookup(recipe_build)
    actors_in_recipe = []
    seen_actors: set[str] = set()
    for step in steps:
        actor = str(step.get("actor") or "").strip()
        if not actor or actor in seen_actors:
            continue
        seen_actors.add(actor)
        actors_in_recipe.append(actor)

    bindings: list[dict[str, Any]] = []
    for actor in actors_in_recipe:
        binding = bindings_by_actor.get(actor)
        if binding is None:
            raise ValueError(f"Actor '{actor}' is not present on the selected flowsheet.")
        if not binding.get("is_resolved") or not binding.get("device_id"):
            raise ValueError(f"Actor '{actor}' is not mapped to a controllable device.")
        profile_id = str(binding.get("profile_id") or "").strip()
        if profile_id != "motor_rpm":
            raise ValueError(
                f"Actor '{actor}' uses profile '{profile_id or 'unknown'}'. Recipe runtime currently supports Motor actors only."
            )
        protocol = _normalized_lookup_value(binding.get("protocol"))
        if protocol != "ika_eurostar_60":
            raise ValueError(
                f"Actor '{actor}' is mapped to protocol '{binding.get('protocol') or 'unknown'}'. "
                "Recipe runtime currently supports IKA stirrer devices only."
            )
        bindings.append(binding)

    for index, step in enumerate(steps, start=1):
        actor = str(step.get("actor") or "").strip()
        binding = next((item for item in bindings if item["actor"] == actor), None)
        if binding is None:
            raise ValueError(f"Step {index} references unknown actor '{actor}'.")
        if step.get("temp") is not None or step.get("pressure") is not None:
            raise ValueError(
                f"Step {index} contains Temp/Pressure values for actor '{actor}'. "
                "Recipe runtime currently applies RPM-controlled motor steps only."
            )
        rpm = step.get("rpm")
        if rpm is not None and float(rpm) > IKA_EUROSTAR_60_MAX_RPM:
            raise ValueError(
                f"Step {index} requests {rpm:g} rpm for actor '{actor}'. "
                f"IKA EUROSTAR 60 supports up to {IKA_EUROSTAR_60_MAX_RPM} rpm."
            )

    return {
        "recipe_id": recipe.recipe_id,
        "reactor_build_id": recipe_build.reactor_build_id,
        "recipe_title": recipe.title,
        "operator_name": recipe.operator_name,
        "build_name": recipe_build.build_name,
        "steps": steps,
        "bindings": bindings,
    }


def _default_program_payload() -> dict[str, Any]:
    return {
        "status": "idle",
        "recipe_id": None,
        "reactor_build_id": None,
        "recipe_title": "",
        "operator_name": "",
        "build_name": "",
        "requested_by": "",
        "started_at": None,
        "finished_at": None,
        "last_progress_at": None,
        "last_error": "",
        "stop_requested": False,
        "total_steps": 0,
        "active_step_index": None,
        "active_step_number": None,
        "active_step": None,
        "next_step": None,
        "step_started_at": None,
        "step_duration_seconds": 0.0,
        "step_elapsed_seconds": 0.0,
        "step_remaining_seconds": 0.0,
        "step_progress": 0.0,
        "current_targets": [],
        "bindings": [],
    }


def recipe_program_state_to_dict(item: RecipeProgramState | None) -> dict[str, Any]:
    if item is None:
        return _default_program_payload()

    payload = _default_program_payload()
    payload.update(
        {
            "status": str(item.status or "idle"),
            "recipe_id": item.recipe_id,
            "reactor_build_id": item.reactor_build_id,
            "recipe_title": str(item.recipe_title or ""),
            "operator_name": str(item.operator_name or ""),
            "requested_by": str(item.requested_by or ""),
            "started_at": _datetime_isoformat(item.started_at),
            "finished_at": _datetime_isoformat(item.finished_at),
            "last_progress_at": _datetime_isoformat(item.last_progress_at),
            "last_error": str(item.last_error or ""),
            "stop_requested": bool(item.stop_requested),
        }
    )

    snapshot = item.snapshot_json if isinstance(item.snapshot_json, dict) else {}
    payload["build_name"] = str(snapshot.get("build_name") or "")
    payload["bindings"] = deepcopy(snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else [])

    steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
    evaluation = _evaluate_program_timeline(
        steps,
        active_step_index=int(item.active_step_index or 0),
        step_started_at=item.step_started_at,
        now=_now_utc(),
    )
    payload["total_steps"] = int(evaluation.get("total_steps") or 0)
    if not evaluation.get("completed"):
        active_step_index = int(evaluation.get("active_step_index") or 0)
        payload["active_step_index"] = active_step_index
        payload["active_step_number"] = active_step_index + 1
        payload["active_step"] = deepcopy(evaluation.get("active_step"))
        payload["next_step"] = deepcopy(evaluation.get("next_step"))
        payload["step_started_at"] = _datetime_isoformat(evaluation.get("step_started_at"))
        payload["step_duration_seconds"] = round(float(evaluation.get("step_duration_seconds") or 0.0), 3)
        payload["step_elapsed_seconds"] = round(float(evaluation.get("step_elapsed_seconds") or 0.0), 3)
        payload["step_remaining_seconds"] = round(float(evaluation.get("step_remaining_seconds") or 0.0), 3)
        payload["step_progress"] = round(float(evaluation.get("step_progress") or 0.0), 4)
    elif payload["status"] in _TERMINAL_STATUSES:
        payload["active_step_index"] = None
        payload["active_step_number"] = None
        payload["current_targets"] = [
            {"actor": actor, **targets}
            for actor, targets in sorted(
                (evaluation.get("current_targets") or {}).items(),
                key=lambda entry: entry[0].lower(),
            )
        ]

    if not payload["current_targets"]:
        payload["current_targets"] = [
            {"actor": actor, **targets}
            for actor, targets in sorted(
                (evaluation.get("current_targets") or {}).items(),
                key=lambda entry: entry[0].lower(),
            )
        ]

    return payload


def _ensure_program_state() -> RecipeProgramState:
    state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
    if state is not None:
        return state

    state = RecipeProgramState(
        recipe_program_state_id=_PROGRAM_STATE_ID,
        status="idle",
        active_step_index=0,
        stop_requested=False,
    )
    db.session.add(state)
    db.session.flush()
    return state


def start_recipe_program(app: Flask, recipe: Recipe, *, requested_by: str) -> RecipeProgramState:
    recipe_build = db.session.get(ReactorBuild, recipe.reactor_build_id)
    if recipe_build is None:
        raise ValueError("The selected recipe is not linked to a valid flowsheet.")

    snapshot = _program_snapshot_for_recipe(recipe, recipe_build)
    state = _ensure_program_state()
    if str(state.status or "") == _LEASE_STATUS_RUNNING:
        raise ValueError("Another recipe program is already running. Stop it before starting a new one.")

    now = _now_utc()
    state.recipe_id = recipe.recipe_id
    state.reactor_build_id = recipe.reactor_build_id
    state.status = _LEASE_STATUS_RUNNING
    state.requested_by = requested_by
    state.recipe_title = recipe.title
    state.operator_name = recipe.operator_name
    state.snapshot_json = snapshot
    state.last_applied_targets_json = {}
    state.active_step_index = 0
    state.step_started_at = now
    state.started_at = now
    state.finished_at = None
    state.last_progress_at = now
    state.stop_requested = False
    state.last_error = None
    state.lease_owner = None
    state.lease_expires_at = None
    db.session.flush()
    return state


def stop_recipe_program(app: Flask, *, requested_by: str) -> RecipeProgramState:
    state = _ensure_program_state()
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []

    for binding in bindings:
        device_id = binding.get("device_id")
        if device_id in (None, ""):
            continue
        device = db.session.get(Device, int(device_id))
        if device is None:
            continue
        queue_manual_state_update(
            app,
            device,
            desired_is_on=False,
            desired_speed=0,
            requested_by=requested_by,
        )

    now = _now_utc()
    state.status = "stopped"
    state.requested_by = requested_by
    state.stop_requested = False
    state.finished_at = now
    state.last_progress_at = now
    state.last_error = None
    state.last_applied_targets_json = {}
    state.lease_owner = None
    state.lease_expires_at = None
    db.session.flush()
    return state


def _apply_current_targets(app: Flask, state: RecipeProgramState, current_targets: dict[str, dict[str, float]]) -> None:
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    binding_lookup = {
        str(binding.get("actor") or "").strip(): binding
        for binding in bindings
        if isinstance(binding, dict) and str(binding.get("actor") or "").strip()
    }
    applied_lookup = state.last_applied_targets_json if isinstance(state.last_applied_targets_json, dict) else {}
    next_applied_lookup: dict[str, dict[str, Any]] = deepcopy(applied_lookup)

    for actor, targets in current_targets.items():
        binding = binding_lookup.get(actor)
        if binding is None:
            continue

        device_id = binding.get("device_id")
        if device_id in (None, ""):
            raise RuntimeError(f"Actor '{actor}' is no longer mapped to a device.")
        device = db.session.get(Device, int(device_id))
        if device is None:
            raise RuntimeError(f"Device {device_id} for actor '{actor}' was not found.")

        rounded_rpm = max(0, int(round(float((targets or {}).get("rpm") or 0.0))))
        desired_is_on = rounded_rpm > 0
        next_payload = {
            "rpm": rounded_rpm,
            "is_on": desired_is_on,
        }
        if next_applied_lookup.get(actor) == next_payload:
            continue

        queue_manual_state_update(
            app,
            device,
            desired_is_on=desired_is_on,
            desired_speed=rounded_rpm,
            requested_by="recipe_program",
        )
        next_applied_lookup[actor] = next_payload

    state.last_applied_targets_json = next_applied_lookup


def _claim_program_state(app: Flask, worker_id: str) -> bool:
    now = _now_utc()
    lease_until = now + _recipe_program_lease_duration(app)
    try:
        claimed = (
            db.session.query(RecipeProgramState)
            .filter(
                RecipeProgramState.recipe_program_state_id == _PROGRAM_STATE_ID,
                RecipeProgramState.status == _LEASE_STATUS_RUNNING,
                or_(RecipeProgramState.lease_expires_at.is_(None), RecipeProgramState.lease_expires_at < now),
            )
            .update(
                {
                    RecipeProgramState.lease_owner: worker_id,
                    RecipeProgramState.lease_expires_at: lease_until,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        if _is_mysql_record_changed_error(exc):
            return False
        raise
    return bool(claimed)


def _release_program_lease(state: RecipeProgramState) -> None:
    state.lease_owner = None
    state.lease_expires_at = None


def _process_recipe_program_state(app: Flask, *, worker_id: str) -> None:
    state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
    if state is None or state.lease_owner != worker_id or str(state.status or "") != _LEASE_STATUS_RUNNING:
        return

    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
    try:
        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=int(state.active_step_index or 0),
            step_started_at=state.step_started_at,
            now=_now_utc(),
        )
        _apply_current_targets(app, state, evaluation.get("current_targets") or {})

        state.active_step_index = int(evaluation.get("active_step_index") or 0)
        state.step_started_at = evaluation.get("step_started_at")
        state.last_progress_at = _now_utc()
        state.last_error = None
        if evaluation.get("completed"):
            state.status = "completed"
            state.finished_at = _now_utc()
        _release_program_lease(state)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
        if state is None:
            return
        state.status = "error"
        state.last_error = str(exc)
        state.finished_at = _now_utc()
        state.last_progress_at = _now_utc()
        _release_program_lease(state)
        db.session.commit()
        app.logger.warning("Recipe program reconciler failed: %s", exc)


def _reconciler_loop(app: Flask, worker_id: str) -> None:
    loop_sleep = _recipe_program_loop_sleep(app)
    while True:
        try:
            with app.app_context():
                if not _claim_program_state(app, worker_id):
                    db.session.remove()
                    time.sleep(loop_sleep)
                    continue
                _process_recipe_program_state(app, worker_id=worker_id)
                db.session.remove()
        except Exception:
            with app.app_context():
                db.session.rollback()
                db.session.remove()
                app.logger.exception("Recipe program reconciler loop crashed.")
            time.sleep(max(loop_sleep, 1.0))


def start_recipe_program_reconciler(app: Flask) -> None:
    if not app.config.get("RECIPE_PROGRAM_RECONCILER_ENABLED", True):
        return
    if app.config.get("SQLALCHEMY_DATABASE_URI") == "sqlite:///:memory:":
        return
    if app.extensions.get(_WORKER_EXTENSION_KEY):
        return

    worker_id = uuid4().hex
    thread = threading.Thread(
        target=_reconciler_loop,
        name=f"recipe-program-reconciler-{worker_id[:8]}",
        args=(app, worker_id),
        daemon=True,
    )
    thread.start()
    app.extensions[_WORKER_EXTENSION_KEY] = thread
