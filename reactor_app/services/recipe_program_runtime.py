from __future__ import annotations

import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from flask import Flask
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError, OperationalError
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
from .command_dispatcher import cancel_runtime_commands, dispatch_device_command, is_runtime_interrupted_error
from .command_model import CommandPriority, CommandSource, DeviceCommand
from .device_manual_runtime import queue_manual_state_update
from .device_runtime import DeviceCommandError, device_command_sequence_lock, is_device_busy_error
from .runtime_status import RuntimeStatus


_WORKER_EXTENSION_KEY = "recipe_program_reconciler_thread"
_PROGRAM_STATE_ID = 1
_LEASE_STATUS_RUNNING = "running"
_TERMINAL_STATUSES = {"completed", "stopped", "error"}
_TRANSIENT_MYSQL_ERROR_CODES = {1020, 1205, 1213}

# Per-recipe transient-error retry tracking.
# Key: RecipeProgramState.recipe_program_state_id (always 1 in practice).
# Value: consecutive transient device errors on the current recipe execution.
# Reset to 0 on any successful reconciler cycle.
_transient_error_counts: dict[int, int] = {}
_MAX_TRANSIENT_ERRORS = 3      # retries before a generic transient error becomes fatal
_MAX_DEVICE_BUSY_ERRORS = 8    # retries for device lock contention (scheduling artifact, not hardware)
_RETRYABLE_RUNTIME_STATUSES: frozenset[str] = frozenset({"timeout", "expired"})
_NUMERIC_FIELDS = ("temp", "pressure", "rpm")
_STATUS_FIELD = "is_on"
_CONTROL_SENSOR_FIELD = "control_sensor"
_CONTROL_SENSOR_VALUES = {"internal", "external"}
_DEFAULT_CONTROL_SENSOR = "internal"
_SENSOR_SELECT_SETTLE_SECONDS = 0.2
_PARAM_TO_NUMERIC_FIELD = {
    "target_temp_c": "temp",
    "pressure_mbar_a": "pressure",
    "rpm": "rpm",
}
_NUMERIC_TO_PARAM_FIELD = {
    "temp": "target_temp_c",
    "pressure": "pressure_mbar_a",
    "rpm": "rpm",
}
_PRIORITY_MIN = 1
_PRIORITY_MAX = 10
_HUBER_PROTOCOLS = {"huber_unistat_430", "huber_pilot_one", "huber_cc230"}
_HUBER_MIN_SETPOINT_C = -40.0
_HUBER_MAX_SETPOINT_C = 150.0
_HUBER_SETPOINT_LIMITS_BY_PROTOCOL = {
    "huber_unistat_430": (-40.0, 150.0),
    "huber_pilot_one": (-40.0, 150.0),
    "huber_cc230": (-40.0, 150.0),
}
_SAFE_HUBER_SETPOINT_C = 20.0


class RecipeProgramDeviceCommandError(RuntimeError):
    pass


class RecipeProgramCommandInterrupted(RuntimeError):
    pass


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


def _is_transient_mysql_error(exc: OperationalError) -> bool:
    return _mysql_error_code(exc) in _TRANSIENT_MYSQL_ERROR_CODES


def _is_transient_mysql_error_exc(exc: Exception) -> bool:
    """Like _is_transient_mysql_error but accepts any Exception type."""
    if not isinstance(exc, OperationalError):
        return False
    return _mysql_error_code(exc) in _TRANSIENT_MYSQL_ERROR_CODES


def _device_error_runtime_status(exc: "DeviceCommandError") -> str | None:
    """Return the runtime_status embedded in a DeviceCommandError, or None."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    return str(details.get("runtime_status") or "").strip().lower() or None


def _is_transient_device_error(exc: "DeviceCommandError") -> bool:
    """Return True if the DeviceCommandError is likely transient and safe to retry.

    Transient errors:
    - Queue/execution timeout (runtime_status timeout or expired, or HTTP 504)
    - Device-busy without a non-retryable runtime_status (HTTP 409)
    - Socket-level connection/timeout errors embedded in the message
    - DB persistence failures whose root cause is a transient MySQL error
      (1020/1205/1213) — these are InnoDB concurrency conflicts, not device
      failures, and must never put the recipe into ERROR state.

    Non-transient errors (not retried):
    - Validation errors (400)
    - Cancelled / preempted / skipped (programme-level interrupts)
    - Driver-level hardware errors
    """
    runtime_status = _device_error_runtime_status(exc)
    if runtime_status in _RETRYABLE_RUNTIME_STATUSES:
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code == 504:
        return True
    if status_code == 409 and runtime_status in (None, ""):
        # device-busy without an explicit status is a lock-contention timeout
        return True
    # DB persistence failure (500) whose root cause is a transient MySQL error.
    if status_code == 500:
        cause = getattr(exc, "__cause__", None)
        if isinstance(cause, Exception) and _is_transient_mysql_error_exc(cause):
            return True
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return True
    return False


def _is_duplicate_key_error(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    code = int(args[0]) if args else None
    if code == 1062:
        return True
    message = str(original or exc)
    return "UNIQUE constraint failed" in message or "Duplicate entry" in message


def _normalized_lookup_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _huber_setpoint_limits(protocol: Any) -> tuple[float, float]:
    return _HUBER_SETPOINT_LIMITS_BY_PROTOCOL.get(
        _normalized_lookup_value(protocol),
        (_HUBER_MIN_SETPOINT_C, _HUBER_MAX_SETPOINT_C),
    )


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
            if "_order" in payload:
                try:
                    next_payload["_order"] = int(payload.get("_order"))
                except (TypeError, ValueError):
                    pass
            if _STATUS_FIELD in payload and payload.get(_STATUS_FIELD) is not None:
                next_payload[_STATUS_FIELD] = bool(payload.get(_STATUS_FIELD))
            if _CONTROL_SENSOR_FIELD in payload and payload.get(_CONTROL_SENSOR_FIELD):
                next_payload[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(payload.get(_CONTROL_SENSOR_FIELD))
        result[actor_key] = next_payload
    return result


def _normalize_actor_priority(value: Any, fallback: int | None = None) -> int | None:
    if value in (None, ""):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    if parsed < _PRIORITY_MIN or parsed > _PRIORITY_MAX:
        return fallback
    return parsed


def _normalize_control_sensor(value: Any, fallback: str = _DEFAULT_CONTROL_SENSOR) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _CONTROL_SENSOR_VALUES:
        return normalized
    fallback_normalized = str(fallback or "").strip().lower()
    return fallback_normalized if fallback_normalized in _CONTROL_SENSOR_VALUES else _DEFAULT_CONTROL_SENSOR


def _actor_params_from_ref(raw_ref: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {field_name: None for field_name in _NUMERIC_FIELDS}
    params[_STATUS_FIELD] = None
    params[_CONTROL_SENSOR_FIELD] = None
    raw_params = raw_ref.get("params") if isinstance(raw_ref.get("params"), dict) else {}
    status_value = raw_params.get("status_on")
    if status_value is None and _STATUS_FIELD in raw_params:
        status_value = raw_params.get(_STATUS_FIELD)
    if status_value is not None:
        if not isinstance(status_value, bool):
            raise ValueError("Recipe actor params.status_on must be true, false, or null.")
        params[_STATUS_FIELD] = status_value
    for param_field, numeric_field in _PARAM_TO_NUMERIC_FIELD.items():
        value = raw_params.get(param_field)
        if value in (None, "") and numeric_field in raw_params:
            value = raw_params.get(numeric_field)
        if value in (None, ""):
            continue
        try:
            params[numeric_field] = round(float(value), 2)
        except (TypeError, ValueError):
            continue
    control_sensor = raw_params.get(_CONTROL_SENSOR_FIELD)
    if control_sensor not in (None, ""):
        normalized_sensor = str(control_sensor).strip().lower()
        if normalized_sensor not in _CONTROL_SENSOR_VALUES:
            raise ValueError("Recipe actor params.control_sensor must be 'internal' or 'external'.")
        params[_CONTROL_SENSOR_FIELD] = normalized_sensor
    return params


def _step_actor_refs(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_refs = step.get("actors")
    refs: list[dict[str, Any]] = []
    if isinstance(raw_refs, list):
        for index, raw_ref in enumerate(raw_refs):
            if isinstance(raw_ref, dict):
                actor = str(raw_ref.get("actor_id") or raw_ref.get("actor") or "").strip()
                raw_object = raw_ref
            else:
                raise ValueError("Recipe steps must use actor objects in steps[].actors.")
            if actor:
                refs.append(
                    {
                        "actor": actor,
                        "actor_id": actor,
                        "actor_type": str(raw_object.get("actor_type") or "").strip(),
                        "priority": _normalize_actor_priority(raw_object.get("priority"), min(index + 1, _PRIORITY_MAX)),
                        "params": _actor_params_from_ref(raw_object, step),
                        "_order": index,
                    }
                )

    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = _normalized_lookup_value(ref.get("actor"))
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(ref)
    return [
        ref
        for _index, ref in sorted(
            enumerate(deduplicated),
            key=lambda pair: (
                _normalize_actor_priority(pair[1].get("priority"), _PRIORITY_MAX) or _PRIORITY_MAX,
                int(pair[1].get("_order") or pair[0]),
            ),
        )
    ]


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
        params = actor_ref.get("params") if isinstance(actor_ref.get("params"), dict) else {}
        has_target_value = False
        status_value = params.get(_STATUS_FIELD)
        if status_value is not None:
            actor_targets[_STATUS_FIELD] = bool(status_value)
            has_target_value = True
        control_sensor = params.get(_CONTROL_SENSOR_FIELD)
        if control_sensor not in (None, ""):
            actor_targets[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(control_sensor)
            has_target_value = True

        for field_name in _NUMERIC_FIELDS:
            if status_value is False:
                actor_targets[field_name] = 0.0
                continue
            raw_value = params.get(field_name)
            if raw_value in (None, ""):
                continue
            has_target_value = True
            actor_targets[field_name] = round(float(raw_value), 2)
        if not has_target_value and actor not in next_targets:
            continue
        if actor_ref.get("priority") is not None:
            actor_targets["_priority"] = int(actor_ref["priority"])
        actor_targets["_order"] = int(actor_ref.get("_order") or actor_refs.index(actor_ref))
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
        if "_order" in end_actor_targets:
            current_actor_targets["_order"] = end_actor_targets["_order"]
        if _STATUS_FIELD in end_actor_targets:
            current_actor_targets[_STATUS_FIELD] = bool(end_actor_targets.get(_STATUS_FIELD))
        elif _STATUS_FIELD in start_actor_targets:
            current_actor_targets[_STATUS_FIELD] = bool(start_actor_targets.get(_STATUS_FIELD))
        if _CONTROL_SENSOR_FIELD in end_actor_targets and end_actor_targets.get(_CONTROL_SENSOR_FIELD):
            current_actor_targets[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(end_actor_targets.get(_CONTROL_SENSOR_FIELD))
        elif _CONTROL_SENSOR_FIELD in start_actor_targets and start_actor_targets.get(_CONTROL_SENSOR_FIELD):
            current_actor_targets[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(start_actor_targets.get(_CONTROL_SENSOR_FIELD))
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
    forbidden_fields = [
        field_name
        for field_name in ("actor", "actor_id", *_NUMERIC_FIELDS)
        if field_name in payload
    ]
    if forbidden_fields:
        raise ValueError(
            "Recipe steps must use the current steps[].actors[].params structure; "
            f"unsupported top-level field(s): {', '.join(sorted(set(forbidden_fields)))}."
        )
    try:
        delta_time = round(float(payload.get("delta_time", payload.get("delta_min")) or 0.0), 2)
    except (TypeError, ValueError):
        raise ValueError(
            f"Recipe step field 'delta_time' must be a number, got: {payload.get('delta_time', payload.get('delta_min'))!r}"
        )
    actor_refs = _step_actor_refs(payload)
    return {
        "actors": actor_refs,
        "task": str(payload.get("task") or "").strip(),
        "delta_time": delta_time,
        "delta_min": delta_time,
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
        next_targets = _step_actor_target(previous_targets, step)
        actors = [actor for actor in _step_actor_ids(step) if actor in next_targets or actor in previous_targets]
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

    # Expunge DeviceConnection and DeviceServer objects now that all needed
    # metadata has been read above.  The background device runtime updates
    # last_seen_at / last_error / updated_at on these rows via raw SQL
    # concurrently.  Leaving the ORM objects in the session risks a stale-value
    # flush that races with those writes and triggers MySQL error 1020
    # ("Record has changed since last read").
    _expunged_server_ids: set[int] = set()
    for device in devices:
        binding = device.current_binding
        if binding is None:
            continue
        connection = binding.connection
        if connection is None:
            continue
        server = connection.device_server
        if server is not None and server.device_server_id not in _expunged_server_ids:
            try:
                db.session.expunge(server)
                _expunged_server_ids.add(server.device_server_id)
            except Exception:
                pass
        try:
            db.session.expunge(connection)
        except Exception:
            pass

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
                    "H/C recipe actors require supported Huber thermostat devices."
                )
        else:
            raise ValueError(
                f"Actor '{actor}' uses profile '{profile_id or 'unknown'}'. "
                "Recipe runtime currently supports Motor and H/C temperature actors."
            )
        bindings.append(binding)

    binding_by_actor = {str(binding.get("actor") or "").strip(): binding for binding in bindings}
    initialized_fields_by_actor: dict[str, set[str]] = {}
    for index, step in enumerate(steps, start=1):
        actor_refs = _step_actor_refs(step)
        step_actor_ids = [ref["actor"] for ref in actor_refs]

        step_actor_profiles: dict[str, str] = {}
        step_actor_protocols: dict[str, str] = {}
        for actor in step_actor_ids:
            binding = binding_by_actor.get(actor)
            if binding is None:
                raise ValueError(f"Step {index} references unknown actor '{actor}'.")
            profile_id = str(binding.get("profile_id") or "").strip()
            step_actor_profiles[actor] = profile_id
            step_actor_protocols[actor] = _protocol_for_binding(binding)

        normalized_actor_refs: list[dict[str, Any]] = []
        for actor_ref in actor_refs:
            actor = actor_ref["actor"]
            profile_id = step_actor_profiles[actor]
            target_fields = set(_target_fields_for_profile(profile_id))
            raw_params = actor_ref.get("params") if isinstance(actor_ref.get("params"), dict) else {}
            status_on = raw_params.get(_STATUS_FIELD)
            raw_control_sensor = raw_params.get(_CONTROL_SENSOR_FIELD)
            params = {
                field_name: raw_params.get(field_name)
                for field_name in _NUMERIC_FIELDS
            }
            if status_on is not None and not isinstance(status_on, bool):
                raise ValueError(f"Step {index} actor '{actor}' has invalid status_on; expected true, false, or null.")
            control_sensor = None
            if profile_id == "hc_system_temperature":
                if raw_control_sensor not in (None, "") and str(raw_control_sensor).strip().lower() not in _CONTROL_SENSOR_VALUES:
                    raise ValueError(
                        f"Step {index} actor '{actor}' has invalid control_sensor; expected internal or external."
                    )
                control_sensor = _normalize_control_sensor(raw_control_sensor)
            elif raw_control_sensor not in (None, ""):
                control_sensor = None
            for field_name, value in list(params.items()):
                if field_name in target_fields:
                    continue
                if _has_nonzero_numeric_value(value):
                    raise ValueError(
                        f"Step {index} contains non-zero {field_name} for actor '{actor}'. "
                        f"Actor profile '{profile_id or 'unknown'}' does not support this field."
                    )
                params[field_name] = None

            if status_on is False:
                for field_name in target_fields:
                    if params.get(field_name) is not None:
                        raise ValueError(
                            f"Step {index} actor '{actor}' sets {field_name} while status_on is false. "
                            "OFF steps must not send setpoints."
                        )

            if profile_id == "motor_rpm":
                rpm = params.get("rpm")
                if rpm is not None and float(rpm) > IKA_EUROSTAR_60_MAX_RPM:
                    raise ValueError(
                        f"Step {index} requests {rpm:g} rpm for actor '{actor}'. "
                        f"IKA EUROSTAR 60 supports up to {IKA_EUROSTAR_60_MAX_RPM} rpm."
                    )
            elif profile_id == "hc_system_temperature":
                temp = params.get("temp")
                if temp is not None:
                    temp_value = float(temp)
                    min_setpoint, max_setpoint = _huber_setpoint_limits(step_actor_protocols.get(actor))
                    if temp_value < min_setpoint or temp_value > max_setpoint:
                        raise ValueError(
                            f"Step {index} requests {temp_value:g} degC for actor '{actor}'. "
                            f"Huber setpoints are limited to {min_setpoint:g}..{max_setpoint:g} degC."
                        )

            initialized_fields = initialized_fields_by_actor.setdefault(actor, set())
            if status_on is not False:
                missing_initial_fields = [
                    field_name
                    for field_name in target_fields
                    if field_name not in initialized_fields and params.get(field_name) is None
                ]
                if missing_initial_fields:
                    raise ValueError(
                        f"Step {index} for actor '{actor}' must define {', '.join(missing_initial_fields)} "
                        "before it can hold, ramp, or turn on."
                    )
            for field_name in target_fields:
                if params.get(field_name) is not None:
                    initialized_fields.add(field_name)

            normalized_actor_refs.append(
                {
                    "actor": actor,
                    "actor_id": actor,
                    "actor_type": str(actor_ref.get("actor_type") or "").strip(),
                    "priority": _normalize_actor_priority(actor_ref.get("priority"), len(normalized_actor_refs) + 1),
                    "params": {
                        "status_on": status_on,
                        "control_sensor": control_sensor,
                        "target_temp_c": params.get("temp"),
                        "pressure_mbar_a": params.get("pressure"),
                        "rpm": params.get("rpm"),
                    },
                }
            )

        step["actors"] = normalized_actor_refs

    return {
        "recipe_id": recipe.recipe_id,
        "reactor_build_id": recipe_build.reactor_build_id,
        "recipe_title": recipe.title,
        "operator_name": recipe.operator_name,
        "build_name": recipe_build.build_name,
        "steps": steps,
        "bindings": bindings,
        "safe_state": recipe.safe_state_json if isinstance(recipe.safe_state_json, list) else [],
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
    ordered_targets = sorted(
        (targets or {}).items(),
        key=lambda item: (
            int((item[1] or {}).get("_priority") or _PRIORITY_MAX) if isinstance(item[1], dict) else _PRIORITY_MAX,
            int((item[1] or {}).get("_order") or 0) if isinstance(item[1], dict) else 0,
            str(item[0]).lower(),
        ),
    )
    for actor, actor_targets in ordered_targets:
        actor_targets = targets.get(actor) if isinstance(targets.get(actor), dict) else {}
        binding = binding_lookup.get(actor) or {}
        profile_id = str(binding.get("profile_id") or "").strip()
        row = {"actor": actor}
        if profile_id:
            row["profile_id"] = profile_id
        if _STATUS_FIELD in actor_targets and actor_targets.get(_STATUS_FIELD) is not None:
            row[_STATUS_FIELD] = bool(actor_targets.get(_STATUS_FIELD))
        if profile_id == "hc_system_temperature":
            row[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(actor_targets.get(_CONTROL_SENSOR_FIELD))
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
        if profile_id == "hc_system_temperature":
            row[_CONTROL_SENSOR_FIELD] = _normalize_control_sensor(actor_targets.get(_CONTROL_SENSOR_FIELD))
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

    try:
        state = RecipeProgramState(
            recipe_program_state_id=_PROGRAM_STATE_ID,
            status="idle",
            active_step_index=0,
            stop_requested=False,
        )
        db.session.add(state)
        db.session.flush()
    except IntegrityError:
        # Another worker inserted the singleton row first — re-read and return it.
        db.session.rollback()
        state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
        if state is None:
            raise RuntimeError("RecipeProgramState singleton could not be created or found.")
    return state


def start_recipe_program(app: Flask, recipe: Recipe, *, requested_by: str) -> RecipeProgramState:
    recipe_build = db.session.get(ReactorBuild, recipe.reactor_build_id)
    if recipe_build is None:
        raise ValueError("The selected recipe is not linked to a valid flowsheet.")

    snapshot = _program_snapshot_for_recipe(recipe, recipe_build)

    # Acquire a row-level lock before checking the running status.
    # Without this lock, two concurrent POST /api/recipe-programs/start requests
    # can both read status="idle" and both proceed to start a recipe (TOCTOU race).
    # SELECT ... FOR UPDATE serializes the check-and-update within the same DB
    # transaction, making it atomic on InnoDB/MariaDB.
    try:
        dialect = str(getattr(getattr(db.session, "get_bind", lambda: None)(), "dialect", None) or "")
        if "mysql" in str(dialect).lower() or "mariadb" in str(dialect).lower():
            db.session.execute(
                text(
                    "SELECT recipe_program_state_id FROM recipe_program_state "
                    "WHERE recipe_program_state_id = :sid FOR UPDATE"
                ),
                {"sid": _PROGRAM_STATE_ID},
            )
    except Exception:
        pass

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


def _publish_program_stop_request(state: RecipeProgramState) -> None:
    if str(state.status or "").strip().lower() != _LEASE_STATUS_RUNNING:
        return
    state.stop_requested = True
    state.lease_owner = None
    state.lease_expires_at = None
    # Commit before sending safe-stop commands so a claimed worker can see the abort.
    db.session.flush()
    db.session.commit()


def _program_claim_allows_target_application(state: RecipeProgramState, worker_id: str | None) -> bool:
    if not worker_id:
        return True
    try:
        db.session.refresh(state)
    except Exception:
        return False
    return (
        state.lease_owner == worker_id
        and str(state.status or "").strip().lower() == _LEASE_STATUS_RUNNING
        and not bool(state.stop_requested)
    )


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


def _positive_int_config(app: Flask, key: str, default: int, *, min_value: int = 1) -> int:
    try:
        value = int(app.config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, value)


def _recipe_command_payload(app: Flask, payload: dict[str, Any] | None) -> dict[str, Any]:
    command_payload = deepcopy(payload or {})
    command_payload.setdefault(
        "connect_timeout_ms",
        _positive_int_config(app, "RECIPE_PROGRAM_DEVICE_CONNECT_TIMEOUT_MS", 3000),
    )
    command_payload.setdefault(
        "response_timeout_ms",
        _positive_int_config(app, "RECIPE_PROGRAM_DEVICE_RESPONSE_TIMEOUT_MS", 1200),
    )
    command_payload.setdefault(
        "write_timeout_ms",
        _positive_int_config(app, "RECIPE_PROGRAM_DEVICE_WRITE_TIMEOUT_MS", 1200),
    )
    command_payload.setdefault(
        "max_retries",
        _positive_int_config(app, "RECIPE_PROGRAM_DEVICE_MAX_RETRIES", 1, min_value=0),
    )
    return command_payload


def _recipe_step_label(evaluation: dict[str, Any] | None) -> str:
    if not isinstance(evaluation, dict):
        return "current step"
    active_step = evaluation.get("active_step") if isinstance(evaluation.get("active_step"), dict) else None
    try:
        active_step_number = int(evaluation.get("active_step_index") or 0) + 1
    except (TypeError, ValueError):
        active_step_number = 1
    if active_step is None:
        return "final target application"
    task = str(active_step.get("task") or "").strip()
    return f"step {active_step_number}{f' ({task})' if task else ''}"


def _device_command_failure_message(
    exc: DeviceCommandError,
    *,
    evaluation: dict[str, Any] | None,
    actor: str,
    binding: dict[str, Any],
    device: Device,
    command_name: str,
) -> str:
    command = getattr(exc, "command", None)
    device_name = str(getattr(device, "display_name", "") or binding.get("device_display_name") or "").strip()
    device_id = getattr(device, "device_id", binding.get("device_id", "unknown"))
    protocol = str(binding.get("protocol") or getattr(device, "protocol", "") or "unknown").strip()
    detail = str(getattr(command, "error_message", "") or str(exc) or "").strip()
    status = str(getattr(command, "status", "") or "").strip()
    command_id = getattr(command, "command_id", None)
    details = getattr(exc, "details", None)
    error_kind = str(details.get("error_kind") or "").strip().lower() if isinstance(details, dict) else ""
    device_success = bool(details.get("device_success")) if isinstance(details, dict) else False

    message = (
        f"Recipe device command failed at {_recipe_step_label(evaluation)}: "
        f"actor '{actor}', command '{command_name}', device '{device_name or device_id}' "
        f"(ID {device_id}, protocol {protocol})."
    )
    if status:
        message = f"{message} Command status: {status}."
    if command_id:
        message = f"{message} Command ID: {command_id}."
    if detail:
        if error_kind == "persistence":
            outcome = "device outcome confirmed" if device_success else "device outcome not confirmed"
            message = f"{message} Persistence error: {detail} ({outcome})."
        else:
            message = f"{message} Device error: {detail}"
    return message


def _execute_recipe_device_command(
    app: Flask,
    *,
    evaluation: dict[str, Any] | None,
    actor: str,
    binding: dict[str, Any],
    device: Device,
    command_name: str,
    payload: dict[str, Any] | None,
    requested_by: str,
    priority: int = CommandPriority.RECIPE,
    source: str = CommandSource.RECIPE,
    acquire_lock: bool = True,
) -> Any:
    try:
        return dispatch_device_command(
            device,
            DeviceCommand(
                device_id=device.device_id,
                command_type=command_name,
                payload=_recipe_command_payload(app, payload),
                priority=priority,
                source=source,
                requested_by=requested_by,
            ),
            acquire_lock=acquire_lock,
            app=app,
        )
    except DeviceCommandError as exc:
        if is_runtime_interrupted_error(exc):
            raise RecipeProgramCommandInterrupted(str(exc)) from exc
        raise RecipeProgramDeviceCommandError(
            _device_command_failure_message(
                exc,
                evaluation=evaluation,
                actor=actor,
                binding=binding,
                device=device,
                command_name=command_name,
            )
        ) from exc


def _execute_recipe_device_command_sequence(
    app: Flask,
    *,
    evaluation: dict[str, Any] | None,
    actor: str,
    binding: dict[str, Any],
    device: Device,
    commands: list[tuple[str, dict[str, Any] | None]],
    requested_by: str,
    worker_state: RecipeProgramState | None = None,
    worker_id: str | None = None,
) -> bool:
    with device_command_sequence_lock(device.device_id, timeout_s=20.0):
        expected_setpoint_c: float | None = None
        for command_name, payload in commands:
            if worker_state is not None and not _program_claim_allows_target_application(worker_state, worker_id):
                return False
            if command_name == "set_setpoint" and isinstance(payload, dict):
                try:
                    expected_setpoint_c = round(float(payload.get("temp_c")), 2)
                except (TypeError, ValueError):
                    expected_setpoint_c = None
            try:
                execution = _execute_recipe_device_command(
                    app,
                    evaluation=evaluation,
                    actor=actor,
                    binding=binding,
                    device=device,
                    command_name=command_name,
                    payload=payload,
                    requested_by=requested_by,
                    acquire_lock=False,
                )
            except RecipeProgramCommandInterrupted:
                return False
            if command_name in {"select_internal_sensor", "select_external_sensor"}:
                time.sleep(_SENSOR_SELECT_SETTLE_SECONDS)
            if command_name == "get_setpoint" and expected_setpoint_c is not None:
                metadata = getattr(getattr(execution, "result", None), "metadata", None)
                if not isinstance(metadata, dict):
                    continue
                readback = metadata.get("value")
                try:
                    readback_c = round(float(readback), 2)
                except (TypeError, ValueError) as exc:
                    raise RecipeProgramDeviceCommandError(
                        f"Recipe setpoint readback for actor '{actor}' did not return a numeric value."
                    ) from exc
                if abs(readback_c - expected_setpoint_c) > 0.75:
                    raise RecipeProgramDeviceCommandError(
                        f"Recipe setpoint readback mismatch for actor '{actor}': "
                        f"requested {expected_setpoint_c:g} degC, read back {readback_c:g} degC."
                    )
    return True


def _apply_safe_stop_to_binding(
    app: Flask,
    binding: dict[str, Any],
    *,
    requested_by: str,
    safe_params: dict[str, Any] | None = None,
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
        min_setpoint, max_setpoint = _huber_setpoint_limits(binding.get("protocol"))
        safe_p = safe_params or {}
        safe_temp = float(safe_p["target_temp_c"]) if safe_p.get("target_temp_c") is not None else _SAFE_HUBER_SETPOINT_C
        safe_temp = max(min_setpoint, min(max_setpoint, safe_temp))
        safe_target.update({"temp": safe_temp, "is_on": False})
        for command_name, payload in (
            (
                "set_setpoint",
                {
                    "temp_c": safe_temp,
                    "min_setpoint_c": min_setpoint,
                    "max_setpoint_c": max_setpoint,
                },
            ),
            ("stop", {}),
        ):
            try:
                _execute_recipe_device_command(
                    app,
                    evaluation=None,
                    actor=actor,
                    binding=binding,
                    device=device,
                    command_name=command_name,
                    payload=payload,
                    requested_by=requested_by,
                    priority=CommandPriority.SAFETY,
                    source=CommandSource.SYSTEM,
                )
            except Exception as exc:
                error_msg = f"{actor}: {command_name} failed: {exc}"
                errors.append(error_msg)
                app.logger.error("Recipe safe-state command failed: %s", error_msg)
        return safe_target, errors

    if _is_ika_motor_binding(binding):
        safe_p = safe_params or {}
        safe_rpm = int(max(0, round(float(safe_p["rpm"])))) if safe_p.get("rpm") is not None else 0
        safe_target.update({"rpm": safe_rpm, "is_on": False})
        try:
            queue_manual_state_update(
                app,
                device,
                desired_is_on=False,
                desired_speed=safe_rpm,
                requested_by=requested_by,
            )
        except Exception as exc:
            error_msg = f"{actor}: queue safe stirrer state failed: {exc}"
            errors.append(error_msg)
            app.logger.error("Recipe safe-state command failed: %s", error_msg)

        for command_text in (f"OUT_SP_4 {safe_rpm}", "STOP_4"):
            try:
                _execute_recipe_device_command(
                    app,
                    evaluation=None,
                    actor=actor,
                    binding=binding,
                    device=device,
                    command_name="manual_text",
                    payload=_manual_text_payload(command_text),
                    requested_by=requested_by,
                    priority=CommandPriority.SAFETY,
                    source=CommandSource.SYSTEM,
                )
            except Exception as exc:
                error_msg = f"{actor}: {command_text} failed: {exc}"
                errors.append(error_msg)
                app.logger.error("Recipe safe-state command failed: %s", error_msg)
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
        error_msg = f"{actor}: queue safe state failed: {exc}"
        errors.append(error_msg)
        app.logger.error("Recipe safe-state command failed: %s", error_msg)
    return safe_target, errors


def _apply_recipe_safe_state(
    app: Flask,
    snapshot: dict[str, Any],
    *,
    requested_by: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    safe_targets: dict[str, dict[str, Any]] = {}
    safe_errors: list[str] = []

    raw_safe_state = snapshot.get("safe_state") if isinstance(snapshot.get("safe_state"), list) else []
    safe_state_lookup: dict[str, dict[str, Any]] = {}
    for entry in raw_safe_state:
        if not isinstance(entry, dict):
            continue
        actor_key = str(entry.get("actor_id") or entry.get("actor") or "").strip().lower()
        if actor_key:
            safe_state_lookup[actor_key] = entry.get("params") or {}

    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        actor_key = str(binding.get("actor") or "").strip().lower()
        safe_params = safe_state_lookup.get(actor_key) or {}
        safe_target, errors = _apply_safe_stop_to_binding(
            app,
            binding,
            requested_by=requested_by,
            safe_params=safe_params,
        )
        if isinstance(safe_target, dict) and safe_target.get("actor"):
            safe_targets[str(safe_target["actor"])] = safe_target
        safe_errors.extend(errors)
    return safe_targets, safe_errors


def stop_recipe_program(app: Flask, *, requested_by: str) -> RecipeProgramState:
    app.logger.info(
        "Recipe stop requested by '%s': cancelling pending commands and applying safe-state.",
        requested_by,
    )
    state = _ensure_program_state()
    run = _ensure_open_program_run(state) if str(state.status or "").strip().lower() == _LEASE_STATUS_RUNNING else _find_open_program_run()
    _publish_program_stop_request(state)
    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        device_id = binding.get("device_id")
        if device_id in (None, ""):
            continue
        cancel_runtime_commands(
            app,
            device_id=int(device_id),
            priority_gt=CommandPriority.SAFETY,
            status=RuntimeStatus.PREEMPTED,
            reason="Recipe stop requested before command execution.",
        )
    safe_targets, safe_errors = _apply_recipe_safe_state(app, snapshot, requested_by=requested_by)

    now = _now_utc()
    error_message = "; ".join(safe_errors) if safe_errors else None
    if error_message:
        app.logger.critical(
            "Recipe safe-state had %d failure(s) during stop (requested_by=%s): %s",
            len(safe_errors),
            requested_by,
            error_message,
        )
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
    *,
    worker_id: str | None = None,
    evaluation: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    if not _program_claim_allows_target_application(state, worker_id):
        return None

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

    ordered_targets = sorted(
        current_targets.items(),
        key=lambda item: (
            int((item[1] or {}).get("_priority") or _PRIORITY_MAX) if isinstance(item[1], dict) else _PRIORITY_MAX,
            int((item[1] or {}).get("_order") or 0) if isinstance(item[1], dict) else 0,
            str(item[0]).lower(),
        ),
    )
    for actor, targets in ordered_targets:
        if not _program_claim_allows_target_application(state, worker_id):
            return None

        binding = binding_lookup.get(actor)
        if binding is None:
            continue

        device = _load_binding_device(binding, actor)
        previous_payload = applied_lookup.get(actor) if isinstance(applied_lookup.get(actor), dict) else {}

        if _is_ika_motor_binding(binding):
            rounded_rpm = max(0, int(round(float((targets or {}).get("rpm") or 0.0))))
            explicit_status = (targets or {}).get(_STATUS_FIELD)
            if explicit_status is False:
                rounded_rpm = 0
                desired_is_on = False
            elif explicit_status is True:
                desired_is_on = True
            else:
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
            min_setpoint, max_setpoint = _huber_setpoint_limits(binding.get("protocol"))
            explicit_status = (targets or {}).get(_STATUS_FIELD)
            previous_control_sensor = previous_payload.get(_CONTROL_SENSOR_FIELD) if isinstance(previous_payload, dict) else None
            control_sensor = _normalize_control_sensor(
                (targets or {}).get(_CONTROL_SENSOR_FIELD),
                fallback=previous_control_sensor or _DEFAULT_CONTROL_SENSOR,
            )
            raw_temp = (targets or {}).get("temp")
            previous_temp = previous_payload.get("temp") if isinstance(previous_payload, dict) else None
            if raw_temp in (None, ""):
                temp_c = round(float(previous_temp or _SAFE_HUBER_SETPOINT_C), 2)
                has_temp_target = False
            else:
                temp_c = round(float(raw_temp), 2)
                has_temp_target = True
            if has_temp_target and (temp_c < min_setpoint or temp_c > max_setpoint):
                raise RuntimeError(
                    f"Recipe target {temp_c:g} degC for actor '{actor}' is outside the "
                    f"Huber safety range {min_setpoint:g}..{max_setpoint:g} degC."
                )
            desired_is_on = False if explicit_status is False else True
            next_payload = {
                "profile_id": "hc_system_temperature",
                "temp": temp_c,
                "is_on": desired_is_on,
                "control_sensor": control_sensor,
            }
            if next_applied_lookup.get(actor) == next_payload:
                continue

            if explicit_status is False:
                try:
                    _execute_recipe_device_command(
                        app,
                        evaluation=evaluation,
                        actor=actor,
                        binding=binding,
                        device=device,
                        command_name="stop",
                        payload={},
                        requested_by="recipe_program",
                    )
                except RecipeProgramCommandInterrupted:
                    return None
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

            sensor_command = "select_external_sensor" if control_sensor == "external" else "select_internal_sensor"
            command_sequence: list[tuple[str, dict[str, Any] | None]] = [
                ("enable_remote", {}),
                (sensor_command, {"skip_remote": True}),
            ]
            if has_temp_target:
                setpoint_payload = {
                    "temp_c": temp_c,
                    "min_setpoint_c": min_setpoint,
                    "max_setpoint_c": max_setpoint,
                }
                command_sequence.append(("set_setpoint", setpoint_payload))
                command_sequence.append(("get_setpoint", {}))
            if not bool(previous_payload.get("is_on")):
                command_sequence.append(("start", {}))
            applied_sequence = _execute_recipe_device_command_sequence(
                app,
                evaluation=evaluation,
                actor=actor,
                binding=binding,
                device=device,
                commands=command_sequence,
                requested_by="recipe_program",
                worker_state=state,
                worker_id=worker_id,
            )
            if not applied_sequence:
                return None
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
                RecipeProgramState.stop_requested.is_(False),
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
    if state is None or not _program_claim_allows_target_application(state, worker_id):
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
        if not _program_claim_allows_target_application(state, worker_id):
            return
        applied_changes = _apply_current_targets(
            app,
            state,
            evaluation.get("current_targets") or {},
            worker_id=worker_id,
            evaluation=evaluation,
        )
        if applied_changes is None:
            db.session.rollback()
            return

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
        # Successful cycle — clear any accumulated transient-error counter so the
        # next step starts with a clean retry budget.
        _transient_error_counts.pop(_PROGRAM_STATE_ID, None)
        _release_program_lease(state)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()

        # Transient InnoDB errors (1020 = record changed, 1205 = lock wait timeout,
        # 1213 = deadlock) are not recipe failures — they are DB-level contention
        # that will resolve on the next reconciler cycle.  Release the lease so
        # another worker can pick up the state, but do NOT mark the recipe as error.
        if isinstance(exc, OperationalError) and _is_transient_mysql_error(exc):
            try:
                db.session.execute(
                    text(
                        "UPDATE recipe_program_state "
                        "SET lease_owner = NULL, lease_expires_at = NULL "
                        "WHERE recipe_program_state_id = :sid"
                    ),
                    {"sid": _PROGRAM_STATE_ID},
                )
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
            app.logger.warning(
                "Recipe program reconciler hit a transient database conflict (MySQL %s); "
                "lease released, will retry on next cycle.",
                _mysql_error_code(exc),
            )
            return

        # Transient device command errors (socket timeout, queue timeout, busy
        # device) are retried up to _MAX_TRANSIENT_ERRORS consecutive times
        # before the recipe is marked as error.  Each successful cycle resets
        # the counter so transient failures on different steps don't accumulate.
        if isinstance(exc, RecipeProgramDeviceCommandError):
            cause = getattr(exc, "__cause__", None)
            if isinstance(cause, DeviceCommandError) and _is_transient_device_error(cause):
                busy = is_device_busy_error(cause)
                max_retries = _MAX_DEVICE_BUSY_ERRORS if busy else _MAX_TRANSIENT_ERRORS
                retry_count = _transient_error_counts.get(_PROGRAM_STATE_ID, 0) + 1
                _transient_error_counts[_PROGRAM_STATE_ID] = retry_count
                if retry_count <= max_retries:
                    try:
                        db.session.execute(
                            text(
                                "UPDATE recipe_program_state "
                                "SET lease_owner = NULL, lease_expires_at = NULL "
                                "WHERE recipe_program_state_id = :sid"
                            ),
                            {"sid": _PROGRAM_STATE_ID},
                        )
                        db.session.commit()
                    except Exception:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
                    kind = "device busy" if busy else "transient device error"
                    app.logger.warning(
                        "Recipe program: %s (attempt %d/%d); "
                        "lease released, will retry on next cycle. Error: %s",
                        kind,
                        retry_count,
                        max_retries,
                        exc,
                    )
                    return
                app.logger.error(
                    "Recipe program: transient device error exceeded retry limit "
                    "(%d/%d attempts); failing recipe. Error: %s",
                    retry_count,
                    max_retries,
                    exc,
                )

        _transient_error_counts.pop(_PROGRAM_STATE_ID, None)
        state = db.session.get(RecipeProgramState, _PROGRAM_STATE_ID)
        if state is None:
            return
        requested_by = str(getattr(state, "requested_by", "") or "recipe_program").strip() or "recipe_program"
        safe_targets: dict[str, dict[str, Any]] = {}
        safe_errors: list[str] = []
        bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            device_id = binding.get("device_id")
            if device_id in (None, ""):
                continue
            cancel_runtime_commands(
                app,
                device_id=int(device_id),
                priority_gt=CommandPriority.SAFETY,
                status=RuntimeStatus.PREEMPTED,
                reason="Recipe runtime fatal error before safe-state execution.",
            )
        try:
            safe_targets, safe_errors = _apply_recipe_safe_state(app, snapshot, requested_by=requested_by)
        except Exception as safe_exc:
            safe_errors.append(str(safe_exc))
            app.logger.critical(
                "Recipe runtime fatal error safe-state orchestration failed (worker_id=%s): %s",
                worker_id,
                safe_exc,
                exc_info=True,
            )
        now = _now_utc()
        state.status = "error"
        state.last_applied_targets_json = safe_targets
        state.last_error = str(exc) if not safe_errors else f"{exc}; safe-state: {'; '.join(safe_errors)}"
        state.finished_at = now
        state.last_progress_at = now
        _release_program_lease(state)
        try:
            run = _ensure_open_program_run(state)
        except Exception:
            run = None
        if run is not None:
            run.status = "error"
            run.finished_at = now
            run.last_progress_at = now
            run.last_error = state.last_error
            try:
                _record_program_event(
                    run,
                    "error",
                    state=state,
                    evaluation=_evaluate_state_snapshot(state, now=now),
                    payload={
                        "error": state.last_error,
                        "applied_targets": _applied_targets_payload(state.last_applied_targets_json),
                    },
                )
            except Exception:
                pass
        db.session.commit()
        if safe_errors:
            app.logger.critical(
                "Recipe safe-state had %d failure(s) during fatal error handling (worker_id=%s): %s",
                len(safe_errors),
                worker_id,
                "; ".join(safe_errors),
            )
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
    app.logger.info(
        "Recipe program reconciler started pid=%s thread_id=%s worker_id=%s",
        os.getpid(),
        thread.ident,
        worker_id,
    )
