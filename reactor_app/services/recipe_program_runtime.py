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
from ..models import (
    Device,
    DeviceBindingCurrent,
    DeviceConnection,
    Recipe,
    RecipeProgramEvent,
    RecipeProgramRun,
    RecipeProgramState,
    ReactorBuild,
)
from .device_manual_runtime import queue_manual_state_update
from .device_runtime import execute_device_command


_WORKER_EXTENSION_KEY = "recipe_program_reconciler_thread"
_PROGRAM_STATE_ID = 1
_LEASE_STATUS_RUNNING = "running"
_TERMINAL_STATUSES = {"completed", "stopped", "error"}
_NUMERIC_FIELDS = ("temp", "pressure", "rpm")
_HUBER_PROTOCOLS = {"huber_unistat_430", "huber_pilot_one"}
_HUBER_MIN_SETPOINT_C = -40.0
_HUBER_MAX_SETPOINT_C = 150.0
_SAFE_HUBER_SETPOINT_C = 20.0


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
            if "_priority" in payload:
                try:
                    next_payload["_priority"] = int(payload.get("_priority"))
                except (TypeError, ValueError):
                    pass
        result[actor_key] = next_payload
    return result


def _step_actor_refs(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_refs = step.get("actors")
    refs: list[dict[str, Any]] = []
    if isinstance(raw_refs, list):
        for raw_ref in raw_refs:
            if isinstance(raw_ref, str):
                actor = str(raw_ref or "").strip()
                priority = None
            elif isinstance(raw_ref, dict):
                actor = str(raw_ref.get("actor") or "").strip()
                raw_priority = raw_ref.get("priority")
                try:
                    priority = None if raw_priority in (None, "") else int(raw_priority)
                except (TypeError, ValueError):
                    priority = None
            else:
                continue
            if actor:
                refs.append({"actor": actor, "priority": priority})

    if not refs:
        actor = str(step.get("actor") or "").strip()
        if actor:
            refs.append({"actor": actor, "priority": None})

    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = _normalized_lookup_value(ref.get("actor"))
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(ref)
    return deduplicated


def _step_actor_ids(step: dict[str, Any]) -> list[str]:
    return [ref["actor"] for ref in _step_actor_refs(step)]


def _step_actor_target(base_targets: dict[str, dict[str, float]], step: dict[str, Any]) -> dict[str, dict[str, float]]:
    next_targets = _copy_global_targets(base_targets)
    actor_refs = _step_actor_refs(step)
    if not actor_refs:
        return next_targets

    for actor_ref in actor_refs:
        actor = actor_ref["actor"]
        actor_targets = deepcopy(next_targets.get(actor) or _actor_baseline_state())
        for field_name in _NUMERIC_FIELDS:
            raw_value = step.get(field_name)
            if raw_value in (None, ""):
                continue
            actor_targets[field_name] = round(float(raw_value), 2)
        if actor_ref.get("priority") is not None:
            actor_targets["_priority"] = int(actor_ref["priority"])
        next_targets[actor] = actor_targets
    return next_targets


def _interpolate_targets(
    start_targets: dict[str, dict[str, float]],
    end_targets: dict[str, dict[str, float]],
    *,
    actors: list[str],
    progress: float,
) -> dict[str, dict[str, float]]:
    ratio = min(1.0, max(0.0, float(progress)))
    current_targets = _copy_global_targets(start_targets)
    for actor in actors:
        current_actor_targets = deepcopy(current_targets.get(actor) or _actor_baseline_state())
        end_actor_targets = deepcopy(end_targets.get(actor) or _actor_baseline_state())
        start_actor_targets = deepcopy(start_targets.get(actor) or _actor_baseline_state())
        for field_name in _NUMERIC_FIELDS:
            start_value = float(start_actor_targets.get(field_name) or 0.0)
            end_value = float(end_actor_targets.get(field_name) or 0.0)
            current_actor_targets[field_name] = round(start_value + ((end_value - start_value) * ratio), 2)
        if "_priority" in end_actor_targets:
            current_actor_targets["_priority"] = end_actor_targets["_priority"]
        current_targets[actor] = current_actor_targets
    return current_targets


def _parse_numeric_field(payload: dict, field: str) -> float | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError(f"Recipe step field '{field}' must be a number, got: {value!r}")


def _normalize_snapshot_step(raw_step: Any) -> dict[str, Any]:
    payload = raw_step if isinstance(raw_step, dict) else {}
    try:
        delta_time = round(float(payload.get("delta_time") or 0.0), 2)
    except (TypeError, ValueError):
        raise ValueError(f"Recipe step field 'delta_time' must be a number, got: {payload.get('delta_time')!r}")
    actor_refs = _step_actor_refs(payload)
    primary_actor = actor_refs[0]["actor"] if actor_refs else ""
    return {
        "actor": primary_actor,
        "actors": actor_refs,
        "task": str(payload.get("task") or "").strip(),
        "delta_time": delta_time,
        "temp": _parse_numeric_field(payload, "temp"),
        "pressure": _parse_numeric_field(payload, "pressure"),
        "rpm": _parse_numeric_field(payload, "rpm"),
    }


def _has_nonzero_numeric_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        return abs(float(value)) > 0.000001
    except (TypeError, ValueError):
        return True


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
        actors = _step_actor_ids(step)
        next_targets = _step_actor_target(previous_targets, step)
        duration_seconds = max(0.0, round(float(step.get("delta_time") or 0.0) * 60.0, 3))
        elapsed_seconds = max(0.0, (current_time - segment_started_at).total_seconds())

        if duration_seconds <= 0:
            previous_targets = next_targets
            index += 1
            continue

        progress = min(1.0, elapsed_seconds / duration_seconds)
        current_targets = _interpolate_targets(previous_targets, next_targets, actors=actors, progress=progress)
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
        for actor in _step_actor_ids(step):
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
        protocol = _normalized_lookup_value(binding.get("protocol"))
        if profile_id == "motor_rpm":
            if protocol != "ika_eurostar_60":
                raise ValueError(
                    f"Actor '{actor}' is mapped to protocol '{binding.get('protocol') or 'unknown'}'. "
                    "Motor recipe actors require IKA stirrer devices."
                )
        elif profile_id == "hc_system_temperature":
            if protocol not in _HUBER_PROTOCOLS:
                raise ValueError(
                    f"Actor '{actor}' is mapped to protocol '{binding.get('protocol') or 'unknown'}'. "
                    "H/C recipe actors require Huber Unistat/Pilot ONE devices."
                )
        else:
            raise ValueError(
                f"Actor '{actor}' uses profile '{profile_id or 'unknown'}'. "
                "Recipe runtime currently supports Motor and H/C temperature actors."
            )
        bindings.append(binding)

    huber_temp_initialized: set[str] = set()
    for index, step in enumerate(steps, start=1):
        step_actor_ids = _step_actor_ids(step)

        # Which fields are relevant to at least one actor in this step.
        # Used to distinguish "irrelevant zero" from "relevant field for another actor".
        step_relevant_fields: set[str] = set()
        step_actor_profiles: dict[str, str] = {}
        for actor in step_actor_ids:
            binding = next((item for item in bindings if item["actor"] == actor), None)
            if binding is None:
                raise ValueError(f"Step {index} references unknown actor '{actor}'.")
            profile_id = str(binding.get("profile_id") or "").strip()
            step_actor_profiles[actor] = profile_id
            if profile_id == "motor_rpm":
                step_relevant_fields.add("rpm")
            elif profile_id == "hc_system_temperature":
                step_relevant_fields.add("temp")

        for actor in step_actor_ids:
            profile_id = step_actor_profiles[actor]
            if profile_id == "motor_rpm":
                for field_name in ("temp", "pressure"):
                    if field_name not in step_relevant_fields and _has_nonzero_numeric_value(step.get(field_name)):
                        raise ValueError(
                            f"Step {index} contains non-zero {field_name} for motor actor '{actor}'. "
                            "Motor recipe actors support RPM values only."
                        )
                rpm = step.get("rpm")
                if rpm is not None and float(rpm) > IKA_EUROSTAR_60_MAX_RPM:
                    raise ValueError(
                        f"Step {index} requests {rpm:g} rpm for actor '{actor}'. "
                        f"IKA EUROSTAR 60 supports up to {IKA_EUROSTAR_60_MAX_RPM} rpm."
                    )
                continue

            if profile_id == "hc_system_temperature":
                for field_name in ("rpm", "pressure"):
                    if field_name not in step_relevant_fields and _has_nonzero_numeric_value(step.get(field_name)):
                        raise ValueError(
                            f"Step {index} contains non-zero {field_name} for H/C actor '{actor}'. "
                            "H/C recipe actors support temperature values only."
                        )
                temp = step.get("temp")
                if temp is None:
                    if actor not in huber_temp_initialized:
                        raise ValueError(
                            f"Step {index} for H/C actor '{actor}' must define a temperature before it can hold or ramp."
                        )
                    continue
                temp_value = float(temp)
                if temp_value < _HUBER_MIN_SETPOINT_C or temp_value > _HUBER_MAX_SETPOINT_C:
                    raise ValueError(
                        f"Step {index} requests {temp_value:g} degC for actor '{actor}'. "
                        f"Huber setpoints are limited to {_HUBER_MIN_SETPOINT_C:g}..{_HUBER_MAX_SETPOINT_C:g} degC."
                    )
                huber_temp_initialized.add(actor)

        # Null out fields that no actor in this step needs, so irrelevant zero
        # entries from the editor don't propagate into the runtime snapshot.
        for field_name in _NUMERIC_FIELDS:
            if field_name not in step_relevant_fields:
                step[field_name] = None

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


def _binding_summary_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    payload: list[dict[str, Any]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        payload.append(
            {
                "actor": str(binding.get("actor") or "").strip(),
                "device_id": binding.get("device_id"),
                "device_display_name": str(binding.get("device_display_name") or "").strip(),
                "label": str(binding.get("label") or "").strip(),
                "profile_id": str(binding.get("profile_id") or "").strip(),
                "protocol": str(binding.get("protocol") or "").strip(),
            }
        )
    return payload


def _binding_lookup_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    bindings = snapshot.get("bindings") if isinstance(snapshot, dict) and isinstance(snapshot.get("bindings"), list) else []
    return {
        str(binding.get("actor") or "").strip(): binding
        for binding in bindings
        if isinstance(binding, dict) and str(binding.get("actor") or "").strip()
    }


def _target_fields_for_profile(profile_id: str | None) -> tuple[str, ...]:
    normalized = str(profile_id or "").strip()
    if normalized == "motor_rpm":
        return ("rpm",)
    if normalized == "hc_system_temperature":
        return ("temp",)
    return _NUMERIC_FIELDS


def _current_targets_payload(
    targets: dict[str, dict[str, Any]] | None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    binding_lookup = _binding_lookup_from_snapshot(snapshot)
    for actor in sorted((targets or {}).keys(), key=lambda value: value.lower()):
        actor_targets = targets.get(actor) if isinstance(targets.get(actor), dict) else {}
        binding = binding_lookup.get(actor) or {}
        profile_id = str(binding.get("profile_id") or "").strip()
        row = {"actor": actor}
        if profile_id:
            row["profile_id"] = profile_id
        for field_name in _target_fields_for_profile(profile_id):
            raw_value = actor_targets.get(field_name)
            if raw_value in (None, ""):
                row[field_name] = 0.0
                continue
            try:
                row[field_name] = round(float(raw_value), 2)
            except (TypeError, ValueError):
                row[field_name] = 0.0
        payload.append(row)
    return payload


def _applied_targets_payload(applied_targets: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for actor in sorted((applied_targets or {}).keys(), key=lambda value: value.lower()):
        actor_targets = applied_targets.get(actor) if isinstance(applied_targets.get(actor), dict) else {}
        row: dict[str, Any] = {"actor": actor}
        profile_id = str(actor_targets.get("profile_id") or "").strip()
        if profile_id:
            row["profile_id"] = profile_id
        if "rpm" in actor_targets:
            raw_rpm = actor_targets.get("rpm")
            rpm = 0
            if raw_rpm not in (None, ""):
                try:
                    rpm = max(0, int(round(float(raw_rpm))))
                except (TypeError, ValueError):
                    rpm = 0
            row["rpm"] = rpm
        if "temp" in actor_targets:
            try:
                row["temp"] = round(float(actor_targets.get("temp") or 0.0), 2)
            except (TypeError, ValueError):
                row["temp"] = 0.0
        if "pressure" in actor_targets:
            try:
                row["pressure"] = round(float(actor_targets.get("pressure") or 0.0), 2)
            except (TypeError, ValueError):
                row["pressure"] = 0.0
        if "is_on" in actor_targets:
            row["is_on"] = bool(actor_targets.get("is_on"))
        payload.append(row)
    return payload


def _evaluation_payload(
    snapshot: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    include_bindings: bool = False,
) -> dict[str, Any]:
    completed = bool(evaluation.get("completed"))
    active_step_index = int(evaluation.get("active_step_index") or 0)
    payload = {
        "recipe_id": snapshot.get("recipe_id"),
        "reactor_build_id": snapshot.get("reactor_build_id"),
        "recipe_title": str(snapshot.get("recipe_title") or ""),
        "operator_name": str(snapshot.get("operator_name") or ""),
        "build_name": str(snapshot.get("build_name") or ""),
        "total_steps": int(evaluation.get("total_steps") or len(snapshot.get("steps") or [])),
        "completed": completed,
        "active_step_index": None if completed else active_step_index,
        "active_step_number": None if completed else active_step_index + 1,
        "active_step": deepcopy(evaluation.get("active_step")) if isinstance(evaluation.get("active_step"), dict) else None,
        "next_step": deepcopy(evaluation.get("next_step")) if isinstance(evaluation.get("next_step"), dict) else None,
        "step_started_at": _datetime_isoformat(evaluation.get("step_started_at")),
        "step_duration_seconds": round(float(evaluation.get("step_duration_seconds") or 0.0), 3),
        "step_elapsed_seconds": round(float(evaluation.get("step_elapsed_seconds") or 0.0), 3),
        "step_remaining_seconds": round(float(evaluation.get("step_remaining_seconds") or 0.0), 3),
        "step_progress": round(float(evaluation.get("step_progress") or 0.0), 4),
        "current_targets": _current_targets_payload(evaluation.get("current_targets") or {}, snapshot=snapshot),
    }
    if include_bindings:
        payload["bindings"] = _binding_summary_from_snapshot(snapshot)
    return payload


def _evaluate_state_snapshot(state: RecipeProgramState, *, now: datetime | None = None) -> dict[str, Any]:
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
    return _evaluate_program_timeline(
        steps,
        active_step_index=int(state.active_step_index or 0),
        step_started_at=state.step_started_at,
        now=now or _now_utc(),
    )


def _find_open_program_run() -> RecipeProgramRun | None:
    return (
        RecipeProgramRun.query
        .filter(RecipeProgramRun.finished_at.is_(None))
        .order_by(RecipeProgramRun.recipe_program_run_id.desc())
        .first()
    )


def _record_program_event(
    run: RecipeProgramRun,
    event_type: str,
    *,
    state: RecipeProgramState | None = None,
    evaluation: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    include_bindings: bool = False,
) -> None:
    snapshot = {}
    if state is not None and isinstance(state.snapshot_json, dict):
        snapshot = state.snapshot_json
    elif isinstance(run.snapshot_json, dict):
        snapshot = run.snapshot_json

    context_payload = (
        _evaluation_payload(snapshot, evaluation, include_bindings=include_bindings)
        if evaluation is not None
        else ({"bindings": _binding_summary_from_snapshot(snapshot)} if include_bindings else {})
    )
    event_payload = {**context_payload, **(deepcopy(payload) if isinstance(payload, dict) else {})}

    active_step_index = None
    if evaluation is not None and not bool(evaluation.get("completed")):
        active_step_index = int(evaluation.get("active_step_index") or 0)
    elif state is not None and str(state.status or "").strip().lower() == _LEASE_STATUS_RUNNING:
        active_step_index = int(state.active_step_index or 0)

    db.session.add(
        RecipeProgramEvent(
            run=run,
            event_type=event_type,
            active_step_index=active_step_index,
            event_payload=event_payload or None,
        )
    )


def _ensure_open_program_run(state: RecipeProgramState | None) -> RecipeProgramRun | None:
    run = _find_open_program_run()
    if run is not None:
        return run
    if state is None:
        return None

    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    if not snapshot:
        return None

    started_at = _as_utc_datetime(state.started_at) or _now_utc()
    last_progress_at = _as_utc_datetime(state.last_progress_at) or started_at
    run = RecipeProgramRun(
        recipe_id=state.recipe_id,
        reactor_build_id=state.reactor_build_id,
        status=str(state.status or _LEASE_STATUS_RUNNING),
        requested_by=str(state.requested_by or "system"),
        recipe_title=str(state.recipe_title or ""),
        operator_name=str(state.operator_name or ""),
        snapshot_json=deepcopy(snapshot),
        started_at=started_at,
        last_progress_at=last_progress_at,
        last_error=str(state.last_error or "") or None,
    )
    db.session.add(run)
    db.session.flush()
    _record_program_event(
        run,
        "recovered",
        state=state,
        evaluation=_evaluate_state_snapshot(state, now=last_progress_at),
        include_bindings=True,
    )
    return run


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

    if payload["status"] in _TERMINAL_STATUSES:
        payload["active_step_index"] = None
        payload["active_step_number"] = None
        payload["active_step"] = None
        payload["next_step"] = None
        payload["step_started_at"] = None
        payload["step_duration_seconds"] = 0.0
        payload["step_elapsed_seconds"] = 0.0
        payload["step_remaining_seconds"] = 0.0
        payload["step_progress"] = 1.0 if payload["status"] == "completed" else 0.0
        payload["current_targets"] = (
            _current_targets_payload(evaluation.get("current_targets") or {}, snapshot=snapshot)
            if payload["status"] == "completed"
            else []
        )
        return payload

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

    if not payload["current_targets"]:
        payload["current_targets"] = _current_targets_payload(evaluation.get("current_targets") or {}, snapshot=snapshot)

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
    initial_evaluation = _evaluate_program_timeline(
        snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else [],
        active_step_index=0,
        step_started_at=now,
        now=now,
    )
    state.recipe_id = recipe.recipe_id
    state.reactor_build_id = recipe.reactor_build_id
    state.status = _LEASE_STATUS_RUNNING
    state.requested_by = requested_by
    state.recipe_title = recipe.title
    state.operator_name = recipe.operator_name
    state.snapshot_json = snapshot
    state.last_applied_targets_json = {}
    state.active_step_index = 0 if initial_evaluation.get("completed") else int(initial_evaluation.get("active_step_index") or 0)
    state.step_started_at = (
        initial_evaluation.get("step_started_at")
        if not initial_evaluation.get("completed")
        else now
    )
    state.started_at = now
    state.finished_at = None
    state.last_progress_at = now
    state.stop_requested = False
    state.last_error = None
    state.lease_owner = None
    state.lease_expires_at = None

    run = RecipeProgramRun(
        recipe_id=recipe.recipe_id,
        reactor_build_id=recipe.reactor_build_id,
        status=_LEASE_STATUS_RUNNING,
        requested_by=requested_by,
        recipe_title=recipe.title,
        operator_name=recipe.operator_name,
        snapshot_json=deepcopy(snapshot),
        started_at=now,
        last_progress_at=now,
        last_error=None,
    )
    db.session.add(run)
    _record_program_event(
        run,
        "started",
        state=state,
        evaluation=initial_evaluation,
        include_bindings=True,
    )
    db.session.flush()
    return state


def _profile_id_for_binding(binding: dict[str, Any] | None) -> str:
    return str((binding or {}).get("profile_id") or "").strip()


def _protocol_for_binding(binding: dict[str, Any] | None) -> str:
    return _normalized_lookup_value((binding or {}).get("protocol"))


def _is_huber_temperature_binding(binding: dict[str, Any] | None) -> bool:
    return _profile_id_for_binding(binding) == "hc_system_temperature" and _protocol_for_binding(binding) in _HUBER_PROTOCOLS


def _is_ika_motor_binding(binding: dict[str, Any] | None) -> bool:
    return _profile_id_for_binding(binding) == "motor_rpm" and _protocol_for_binding(binding) == "ika_eurostar_60"


def _load_binding_device(binding: dict[str, Any], actor: str) -> Device:
    device_id = binding.get("device_id")
    if device_id in (None, ""):
        raise RuntimeError(f"Actor '{actor}' is no longer mapped to a device.")
    device = db.session.get(Device, int(device_id))
    if device is None:
        raise RuntimeError(f"Device {device_id} for actor '{actor}' was not found.")
    return device


def _manual_text_payload(command_text: str) -> dict[str, Any]:
    return {
        "text": command_text,
        "encoding": "ascii",
        "line_ending": "space_crlf",
        "response_terminator": "none",
        "expect_response": False,
        "strip_response": True,
    }


def _apply_safe_stop_to_binding(
    app: Flask,
    binding: dict[str, Any],
    *,
    requested_by: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    actor = str(binding.get("actor") or "").strip()
    if not actor:
        return None, ["A recipe binding without an actor could not be stopped."]

    errors: list[str] = []
    try:
        device = _load_binding_device(binding, actor)
    except Exception as exc:
        return None, [str(exc)]

    profile_id = _profile_id_for_binding(binding)
    safe_target: dict[str, Any] = {
        "actor": actor,
        "device_id": device.device_id,
        "device_display_name": device.display_name,
        "profile_id": profile_id,
    }

    if _is_huber_temperature_binding(binding):
        safe_target.update({"temp": _SAFE_HUBER_SETPOINT_C, "is_on": False})
        for command_name, payload in (
            (
                "set_setpoint",
                {
                    "temp_c": _SAFE_HUBER_SETPOINT_C,
                    "min_setpoint_c": _HUBER_MIN_SETPOINT_C,
                    "max_setpoint_c": _HUBER_MAX_SETPOINT_C,
                },
            ),
            ("stop", {}),
        ):
            try:
                execute_device_command(
                    device,
                    command_name=command_name,
                    payload=payload,
                    requested_by=requested_by,
                )
            except Exception as exc:
                errors.append(f"{actor}: {command_name} failed: {exc}")
        return safe_target, errors

    if _is_ika_motor_binding(binding):
        safe_target.update({"rpm": 0, "is_on": False})
        try:
            queue_manual_state_update(
                app,
                device,
                desired_is_on=False,
                desired_speed=0,
                requested_by=requested_by,
            )
        except Exception as exc:
            errors.append(f"{actor}: queue safe stirrer state failed: {exc}")

        for command_text in ("OUT_SP_4 0", "STOP_4"):
            try:
                execute_device_command(
                    device,
                    command_name="manual_text",
                    payload=_manual_text_payload(command_text),
                    requested_by=requested_by,
                )
            except Exception as exc:
                errors.append(f"{actor}: {command_text} failed: {exc}")
        return safe_target, errors

    safe_target.update({"is_on": False})
    try:
        queue_manual_state_update(
            app,
            device,
            desired_is_on=False,
            desired_speed=0,
            requested_by=requested_by,
        )
        safe_target["rpm"] = 0
    except Exception as exc:
        errors.append(f"{actor}: queue safe state failed: {exc}")
    return safe_target, errors


def stop_recipe_program(app: Flask, *, requested_by: str) -> RecipeProgramState:
    state = _ensure_program_state()
    run = _ensure_open_program_run(state) if str(state.status or "").strip().lower() == _LEASE_STATUS_RUNNING else _find_open_program_run()
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    safe_targets: dict[str, dict[str, Any]] = {}
    safe_errors: list[str] = []

    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        safe_target, errors = _apply_safe_stop_to_binding(app, binding, requested_by=requested_by)
        if isinstance(safe_target, dict) and safe_target.get("actor"):
            safe_targets[str(safe_target["actor"])] = safe_target
        safe_errors.extend(errors)

    now = _now_utc()
    error_message = "; ".join(safe_errors) if safe_errors else None
    state.status = "error" if error_message else "stopped"
    state.requested_by = requested_by
    state.stop_requested = False
    state.finished_at = now
    state.last_progress_at = now
    state.last_error = error_message
    state.last_applied_targets_json = safe_targets
    state.lease_owner = None
    state.lease_expires_at = None
    if run is not None:
        run.status = state.status
        run.requested_by = requested_by
        run.finished_at = now
        run.last_progress_at = now
        run.last_error = error_message
        _record_program_event(
            run,
            "error" if error_message else "stopped",
            state=state,
            evaluation=_evaluate_state_snapshot(state, now=now),
            payload={
                "error": error_message,
                "applied_targets": _applied_targets_payload(safe_targets),
            } if error_message else {"applied_targets": _applied_targets_payload(safe_targets)},
        )
    db.session.flush()
    return state


def _apply_current_targets(
    app: Flask,
    state: RecipeProgramState,
    current_targets: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    binding_lookup = {
        str(binding.get("actor") or "").strip(): binding
        for binding in bindings
        if isinstance(binding, dict) and str(binding.get("actor") or "").strip()
    }
    applied_lookup = state.last_applied_targets_json if isinstance(state.last_applied_targets_json, dict) else {}
    next_applied_lookup: dict[str, dict[str, Any]] = deepcopy(applied_lookup)
    applied_changes: list[dict[str, Any]] = []

    # Priority is optional per step actor. Equal or missing priorities remain
    # deterministic by actor id; this keeps concurrent multi-actor steps ordered
    # without adding database locks around unrelated devices.
    ordered_targets = sorted(
        current_targets.items(),
        key=lambda item: (
            int((item[1] or {}).get("_priority") or 0) if isinstance(item[1], dict) else 0,
            str(item[0]).lower(),
        ),
    )
    for actor, targets in ordered_targets:
        binding = binding_lookup.get(actor)
        if binding is None:
            continue

        device = _load_binding_device(binding, actor)
        previous_payload = applied_lookup.get(actor) if isinstance(applied_lookup.get(actor), dict) else {}

        if _is_ika_motor_binding(binding):
            rounded_rpm = max(0, int(round(float((targets or {}).get("rpm") or 0.0))))
            desired_is_on = rounded_rpm > 0
            next_payload = {
                "profile_id": "motor_rpm",
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
            applied_changes.append(
                {
                    "actor": actor,
                    "device_id": device.device_id,
                    "device_display_name": device.display_name,
                    "previous": {
                        "profile_id": "motor_rpm",
                        "rpm": max(0, int(round(float(previous_payload.get("rpm") or 0.0)))) if previous_payload else 0,
                        "is_on": bool(previous_payload.get("is_on")) if previous_payload else False,
                    },
                    "current": deepcopy(next_payload),
                }
            )
            continue

        if _is_huber_temperature_binding(binding):
            temp_c = round(float((targets or {}).get("temp") or 0.0), 2)
            if temp_c < _HUBER_MIN_SETPOINT_C or temp_c > _HUBER_MAX_SETPOINT_C:
                raise RuntimeError(
                    f"Recipe target {temp_c:g} degC for actor '{actor}' is outside the "
                    f"Huber safety range {_HUBER_MIN_SETPOINT_C:g}..{_HUBER_MAX_SETPOINT_C:g} degC."
                )
            next_payload = {
                "profile_id": "hc_system_temperature",
                "temp": temp_c,
                "is_on": True,
            }
            if next_applied_lookup.get(actor) == next_payload:
                continue

            execute_device_command(
                device,
                command_name="set_setpoint",
                payload={
                    "temp_c": temp_c,
                    "min_setpoint_c": _HUBER_MIN_SETPOINT_C,
                    "max_setpoint_c": _HUBER_MAX_SETPOINT_C,
                },
                requested_by="recipe_program",
            )
            if not bool(previous_payload.get("is_on")):
                execute_device_command(
                    device,
                    command_name="start",
                    payload={},
                    requested_by="recipe_program",
                )
            next_applied_lookup[actor] = next_payload
            applied_changes.append(
                {
                    "actor": actor,
                    "device_id": device.device_id,
                    "device_display_name": device.display_name,
                    "previous": {
                        "profile_id": "hc_system_temperature",
                        "temp": round(float(previous_payload.get("temp") or 0.0), 2) if previous_payload else 0.0,
                        "is_on": bool(previous_payload.get("is_on")) if previous_payload else False,
                    },
                    "current": deepcopy(next_payload),
                }
            )
            continue

        raise RuntimeError(
            f"Actor '{actor}' uses unsupported recipe binding "
            f"'{_profile_id_for_binding(binding) or 'unknown'}' / '{binding.get('protocol') or 'unknown'}'."
        )

    state.last_applied_targets_json = next_applied_lookup
    return applied_changes


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
        previous_active_step_index = int(state.active_step_index or 0)
        run = _ensure_open_program_run(state)
        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=int(state.active_step_index or 0),
            step_started_at=state.step_started_at,
            now=_now_utc(),
        )
        applied_changes = _apply_current_targets(app, state, evaluation.get("current_targets") or {})

        state.active_step_index = int(evaluation.get("active_step_index") or 0)
        state.step_started_at = evaluation.get("step_started_at")
        state.last_progress_at = _now_utc()
        state.last_error = None
        if run is not None:
            run.status = _LEASE_STATUS_RUNNING
            run.last_progress_at = state.last_progress_at
            run.last_error = None
            if not evaluation.get("completed") and state.active_step_index != previous_active_step_index:
                _record_program_event(run, "step_started", state=state, evaluation=evaluation)
            if applied_changes:
                _record_program_event(
                    run,
                    "targets_applied",
                    state=state,
                    evaluation=evaluation,
                    payload={"changes": applied_changes},
                )
        if evaluation.get("completed"):
            state.status = "completed"
            state.finished_at = _now_utc()
            if run is not None:
                run.status = "completed"
                run.finished_at = state.finished_at
                run.last_progress_at = state.finished_at
                run.last_error = None
                _record_program_event(
                    run,
                    "completed",
                    state=state,
                    evaluation=evaluation,
                    payload={"applied_targets": _applied_targets_payload(state.last_applied_targets_json)},
                )
        _release_program_lease(state)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
        if state is None:
            return
        run = _ensure_open_program_run(state)
        state.status = "error"
        state.last_error = str(exc)
        state.finished_at = _now_utc()
        state.last_progress_at = _now_utc()
        _release_program_lease(state)
        if run is not None:
            run.status = "error"
            run.finished_at = state.finished_at
            run.last_progress_at = state.last_progress_at
            run.last_error = state.last_error
            _record_program_event(
                run,
                "error",
                state=state,
                evaluation=_evaluate_state_snapshot(state, now=state.last_progress_at),
                payload={
                    "error": state.last_error,
                    "applied_targets": _applied_targets_payload(state.last_applied_targets_json),
                },
            )
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
