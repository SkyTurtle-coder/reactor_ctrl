from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from flask import Flask, has_app_context
from sqlalchemy import and_, case, inspect, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError

from ..extensions import db
from ..models import (
    Device,
    DeviceBindingCurrent,
    DeviceConnection,
    DeviceManualState,
    Measurement,
    MeasurementChannel,
    RecipeProgramState,
)
from .command_dispatcher import dispatch_device_command, is_runtime_interrupted_error
from .command_model import CommandPriority, CommandSource, DeviceCommand
from .device_runtime import DeviceCommandError, describe_device_command_error


_WORKER_EXTENSION_KEY = "device_manual_reconciler_thread"
_DEVICE_DISCOVERY_INTERVAL_SECONDS = 60  # how often to scan for newly active supported devices
_RECIPE_PROGRAM_STATE_ID = 1
_RECIPE_PROGRAM_RUNNING_STATUS = "running"
_RECIPE_PRIORITY_MAX = 10
_TRANSIENT_MYSQL_ERROR_CODES = {1020, 1205, 1213}
_DUPLICATE_KEY_ERROR_CODES = {1062}
_MANUAL_RECIPE_SEQUENCE_LOCK = threading.RLock()
_MANUAL_CLAIM_PORT_ORDER_CACHE: dict[int, bool] = {}
_UNCHANGED = object()

# Channel definitions for IKA telemetry that are persisted as measurements
# on every reconciler poll cycle. channel_code values are part of the
# measurement model and are used by both the Process plot and the Data view.
_IKA_TELEMETRY_CHANNELS: tuple[dict, ...] = (
    {"key": "setpoint_rpm", "channel_code": "ika_setpoint_rpm", "display_name": "Setpoint RPM", "unit": "rpm"},
    {"key": "actual_rpm",   "channel_code": "ika_actual_rpm",   "display_name": "Actual RPM",   "unit": "rpm"},
    {"key": "torque_ncm",   "channel_code": "ika_torque_ncm",   "display_name": "Torque",        "unit": "Ncm"},
)
_HUBER_PROTOCOLS = {"huber_unistat_430", "huber_pilot_one", "huber_cc230"}
# Background polling must finish well within the execution_timeout_s for POLLING
# commands so that user-triggered commands (start, set_setpoint, …) can preempt
# polling within one cooperative-poll interval (250 ms).
#
# The CC230 sometimes ignores primary queries (e.g. SETPOINT?, TE?) forcing the
# driver to wait for the full socket read_timeout before trying the fallback
# command.  A primary + fallback chain therefore takes up to 2 × read_timeout.
# With execution_timeout_s = 10 s for POLLING and 2 commands in the fallback
# chain at 1.5 s each the worst-case driver time is 3 s — well inside the budget.
# Working commands (TEMP?, TI?, BATH?) respond in well under 200 ms at 9600 baud
# so 1.5 s gives 7× headroom even against occasional NPort latency spikes.
_CC230_POLL_RESPONSE_TIMEOUT_MS = 1500
_HUBER_TELEMETRY_CHANNELS: tuple[dict, ...] = (
    {"key": "setpoint_C", "channel_code": "setpoint_C", "display_name": "Setpoint", "unit": "degC"},
    {"key": "actual_temp_C", "channel_code": "actual_temp_C", "display_name": "Actual Temperature", "unit": "degC"},
)
_CC230_TELEMETRY_CHANNELS: tuple[dict, ...] = (
    {"key": "setpoint_C", "channel_code": "setpoint_C", "display_name": "Setpoint", "unit": "degC"},
    {"key": "actual_temp_C", "channel_code": "actual_temp_C", "display_name": "Process Temperature", "unit": "degC"},
    {"key": "bath_temp_C", "channel_code": "bath_temp_C", "display_name": "Bath Temperature", "unit": "degC"},
    {"key": "internal_temp_C", "channel_code": "internal_temp_C", "display_name": "Internal Temperature", "unit": "degC"},
    {"key": "external_temp_C", "channel_code": "external_temp_C", "display_name": "External Temperature", "unit": "degC"},
    {"key": "status", "channel_code": "cc230_status", "display_name": "Status", "unit": "", "value_type": "text"},
    {"key": "error", "channel_code": "cc230_error", "display_name": "Error", "unit": "", "value_type": "text"},
    {"key": "warning", "channel_code": "cc230_warning", "display_name": "Warning", "unit": "", "value_type": "text"},
)
_IKA_TELEMETRY_MEASUREMENT_SOURCE = "poller"


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


def _mysql_integrity_error_code(exc: IntegrityError) -> int | None:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def _is_transient_mysql_error(exc: OperationalError) -> bool:
    return _mysql_error_code(exc) in _TRANSIENT_MYSQL_ERROR_CODES


def _is_duplicate_key_error(exc: IntegrityError) -> bool:
    code = _mysql_integrity_error_code(exc)
    if code in _DUPLICATE_KEY_ERROR_CODES:
        return True
    message = str(getattr(exc, "orig", exc))
    return "UNIQUE constraint failed" in message or "Duplicate entry" in message


def _run_with_transient_db_retry(
    operation,
    *,
    attempts: int = 3,
    retry_duplicate_key: bool = False,
):
    max_attempts = max(1, int(attempts))
    for attempt_index in range(max_attempts):
        try:
            return operation()
        except OperationalError as exc:
            db.session.rollback()
            if not _is_transient_mysql_error(exc) or attempt_index >= max_attempts - 1:
                raise
            time.sleep(0.05 * (attempt_index + 1))
        except IntegrityError as exc:
            db.session.rollback()
            if not retry_duplicate_key or not _is_duplicate_key_error(exc) or attempt_index >= max_attempts - 1:
                raise
            time.sleep(0.05 * (attempt_index + 1))
    raise RuntimeError("Transient database retry exhausted unexpectedly.")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _db_dialect_name() -> str:
    try:
        bind = db.session.get_bind()
    except Exception:
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


@contextmanager
def _manual_recipe_sequence_lock(*, timeout_s: float = 1.0):
    dialect_name = _db_dialect_name()
    timeout_seconds = max(1, int(round(float(timeout_s))))

    if dialect_name in {"mysql", "mariadb"}:
        lock_name = "reactor_ctrl:recipe_manual_sequence"
        with db.engine.connect() as connection:
            result = connection.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout_s)"),
                {"lock_name": lock_name, "timeout_s": timeout_seconds},
            ).scalar()
            if result != 1:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})
                except Exception:
                    pass
        return

    acquired = _MANUAL_RECIPE_SEQUENCE_LOCK.acquire(timeout=timeout_seconds)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        _MANUAL_RECIPE_SEQUENCE_LOCK.release()


@contextmanager
def _no_sequence_lock():
    yield True


def _manual_watch_ttl(app: Flask) -> timedelta:
    seconds = max(5, int(app.config.get("DEVICE_MANUAL_RECONCILER_WATCH_TTL_SECONDS", 30)))
    return timedelta(seconds=seconds)


def _manual_poll_interval(app: Flask) -> timedelta:
    milliseconds = max(1000, int(app.config.get("DEVICE_MANUAL_RECONCILER_POLL_MS", 1000)))
    return timedelta(milliseconds=milliseconds)


def _manual_loop_sleep(app: Flask) -> float:
    milliseconds = max(100, int(app.config.get("DEVICE_MANUAL_RECONCILER_LOOP_MS", 500)))
    return milliseconds / 1000.0


def _background_poll_interval(app: Flask) -> timedelta:
    """Interval between telemetry polls when no UI session is active.

    This controls how often IKA device readings are stored as measurements
    even when nobody has the Process page open.  Defaults to 30 s.
    """
    seconds = max(10, int(app.config.get("MEASUREMENT_POLLER_INTERVAL_SECONDS", 30)))
    return timedelta(seconds=seconds)


def _background_huber_poll_enabled(app: Flask) -> bool:
    return bool(app.config.get("MEASUREMENT_POLLER_BACKGROUND_HUBER_ENABLED", False))


def _manual_lease_duration(app: Flask) -> timedelta:
    seconds = max(3, int(app.config.get("DEVICE_MANUAL_RECONCILER_LEASE_SECONDS", 15)))
    return timedelta(seconds=seconds)


def _manual_command_payload(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip().upper()
    return {
        "text": normalized,
        "encoding": "ascii",
        "line_ending": "space_crlf",
        "response_terminator": "crlf" if normalized.startswith("IN_") else "none",
        "expect_response": normalized.startswith("IN_"),
        "strip_response": True,
    }


def _parse_ika_numeric_response(text: str | None) -> float | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    # IKA EUROSTAR responses include a channel suffix after the value
    # (e.g. "IN_SP_4" → "100.0 4", "IN_PV_5" → "2.3 5").
    # Take only the first whitespace-delimited token as the numeric value.
    token = raw.split()[0]
    try:
        return float(token)
    except ValueError:
        return None


def _supports_manual_runtime(device: Device | None) -> bool:
    protocol = str(getattr(device, "protocol", "") or "").strip().lower()
    return protocol == "ika_eurostar_60" or protocol in _HUBER_PROTOCOLS


def _is_ika_device(device: Device | None) -> bool:
    return str(getattr(device, "protocol", "") or "").strip().lower() == "ika_eurostar_60"


def _is_huber_device(device: Device | None) -> bool:
    return str(getattr(device, "protocol", "") or "").strip().lower() in _HUBER_PROTOCOLS


def _is_cc230_device(device: Device | None) -> bool:
    return str(getattr(device, "protocol", "") or "").strip().lower() == "huber_cc230"


def _active_recipe_program_device_ids() -> set[int] | None:
    try:
        state = db.session.get(RecipeProgramState, _RECIPE_PROGRAM_STATE_ID)
    except Exception:
        return None
    if state is None or str(state.status or "").strip().lower() != _RECIPE_PROGRAM_RUNNING_STATUS:
        return None

    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    device_ids: set[int] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        raw_device_id = binding.get("device_id")
        try:
            device_id = int(raw_device_id)
        except (TypeError, ValueError):
            continue
        if device_id > 0:
            device_ids.add(device_id)
    return device_ids


def _active_recipe_device_priority_order(now: datetime) -> dict[int, tuple[int, int]]:
    try:
        state = db.session.get(RecipeProgramState, _RECIPE_PROGRAM_STATE_ID)
    except Exception:
        return {}
    if state is None or str(state.status or "").strip().lower() != _RECIPE_PROGRAM_RUNNING_STATUS:
        return {}

    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    binding_by_actor = {
        str(binding.get("actor") or "").strip(): binding
        for binding in bindings
        if isinstance(binding, dict) and str(binding.get("actor") or "").strip()
    }

    try:
        from . import recipe_program_runtime

        evaluation = recipe_program_runtime._evaluate_state_snapshot(state, now=now)
    except Exception:
        return {}

    current_targets = evaluation.get("current_targets") if isinstance(evaluation, dict) else {}
    if not isinstance(current_targets, dict):
        return {}

    ordered_targets = sorted(
        current_targets.items(),
        key=lambda item: (
            int((item[1] or {}).get("_priority") or _RECIPE_PRIORITY_MAX)
            if isinstance(item[1], dict)
            else _RECIPE_PRIORITY_MAX,
            int((item[1] or {}).get("_order") or 0) if isinstance(item[1], dict) else 0,
            str(item[0]).lower(),
        ),
    )

    order_by_device_id: dict[int, tuple[int, int]] = {}
    for order_index, (actor, targets) in enumerate(ordered_targets):
        binding = binding_by_actor.get(str(actor or "").strip())
        if not isinstance(binding, dict):
            continue
        try:
            device_id = int(binding.get("device_id"))
        except (TypeError, ValueError):
            continue
        if device_id <= 0 or device_id in order_by_device_id:
            continue
        try:
            priority = int((targets or {}).get("_priority") or _RECIPE_PRIORITY_MAX) if isinstance(targets, dict) else _RECIPE_PRIORITY_MAX
        except (TypeError, ValueError):
            priority = _RECIPE_PRIORITY_MAX
        priority = min(_RECIPE_PRIORITY_MAX, max(1, priority))
        order_by_device_id[device_id] = (priority, order_index)
    return order_by_device_id


def _candidate_row_value(row: Any, index: int, default: Any = None) -> Any:
    try:
        return row[index]
    except (IndexError, TypeError, KeyError):
        return default


def _candidate_datetime_sort_value(value: Any) -> float:
    normalized = _as_utc_datetime(value if isinstance(value, datetime) else None)
    return normalized.timestamp() if normalized is not None else float("inf")


def _manual_claim_candidate_sort_key(
    row: Any,
    *,
    active_recipe_priority_order: dict[int, tuple[int, int]],
    active_recipe: bool,
) -> tuple:
    try:
        device_id = int(_candidate_row_value(row, 0, 0) or 0)
    except (TypeError, ValueError):
        device_id = 0
    try:
        desired_version = int(_candidate_row_value(row, 1, 0) or 0)
    except (TypeError, ValueError):
        desired_version = 0
    try:
        applied_version = int(_candidate_row_value(row, 2, 0) or 0)
    except (TypeError, ValueError):
        applied_version = 0
    try:
        port_number = int(_candidate_row_value(row, 5, 9999) or 9999)
    except (TypeError, ValueError):
        port_number = 9999

    desired_pending_order = 0 if desired_version > applied_version else 1
    if active_recipe:
        priority, recipe_order = active_recipe_priority_order.get(device_id, (_RECIPE_PRIORITY_MAX + 1, 9999))
        return (priority, recipe_order, desired_pending_order, port_number, device_id)

    return (
        desired_pending_order,
        _candidate_datetime_sort_value(_candidate_row_value(row, 3)),
        _candidate_datetime_sort_value(_candidate_row_value(row, 4)),
        port_number,
        device_id,
    )


def _manual_claim_port_order_available() -> bool:
    try:
        engine = db.engine
    except Exception:
        return True
    engine_key = id(engine)
    if engine_key in _MANUAL_CLAIM_PORT_ORDER_CACHE:
        return _MANUAL_CLAIM_PORT_ORDER_CACHE[engine_key]
    try:
        table_names = set(inspect(engine).get_table_names())
    except Exception:
        _MANUAL_CLAIM_PORT_ORDER_CACHE[engine_key] = True
        return True
    available = {"device_binding_current", "device_connection"}.issubset(table_names)
    _MANUAL_CLAIM_PORT_ORDER_CACHE[engine_key] = available
    return available


def _active_recipe_binding_for_device(device_id: int) -> tuple[RecipeProgramState, dict[str, Any]] | tuple[None, None]:
    try:
        state = db.session.get(RecipeProgramState, _RECIPE_PROGRAM_STATE_ID)
    except Exception:
        return None, None
    if state is None or str(state.status or "").strip().lower() != _RECIPE_PROGRAM_RUNNING_STATUS:
        return None, None

    snapshot = state.snapshot_json if isinstance(state.snapshot_json, dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        try:
            binding_device_id = int(binding.get("device_id"))
        except (TypeError, ValueError):
            continue
        if binding_device_id == int(device_id):
            return state, binding
    return None, None


def _manual_runtime_error_message(device: Device | None, exc: Exception, binding: dict[str, Any] | None) -> str:
    actor = str((binding or {}).get("actor") or "").strip()
    command = getattr(exc, "command", None)
    detail = str(getattr(command, "error_message", "") or str(exc) or "").strip()
    command_name = str(getattr(command, "command_name", "") or "manual-state command").strip()
    status = str(getattr(command, "status", "") or "").strip()
    command_id = getattr(command, "command_id", None)
    device_id = getattr(device, "device_id", (binding or {}).get("device_id", "unknown"))
    device_name = str(getattr(device, "display_name", "") or (binding or {}).get("device_display_name") or "").strip()
    protocol = str(getattr(device, "protocol", "") or (binding or {}).get("protocol") or "unknown").strip()

    message = (
        "Recipe device command failed during manual device execution: "
        f"actor '{actor or 'unknown'}', command '{command_name}', "
        f"device '{device_name or device_id}' (ID {device_id}, protocol {protocol})."
    )
    if status:
        message = f"{message} Command status: {status}."
    if command_id:
        message = f"{message} Command ID: {command_id}."
    if detail:
        message = f"{message} Device error: {detail}"
    return message


def _fail_active_recipe_program_for_device(app: Flask, device: Device | None, exc: Exception) -> str | None:
    if device is None:
        return None
    program_state, binding = _active_recipe_binding_for_device(int(device.device_id))
    if program_state is None or binding is None:
        return None

    now = _now_utc()
    message = _manual_runtime_error_message(device, exc, binding)
    program_state.status = "error"
    program_state.stop_requested = False
    program_state.finished_at = now
    program_state.last_progress_at = now
    program_state.last_error = message
    program_state.lease_owner = None
    program_state.lease_expires_at = None

    if not has_app_context():
        return message

    try:
        from . import recipe_program_runtime

        run = recipe_program_runtime._ensure_open_program_run(program_state)
        if run is not None:
            run.status = "error"
            run.finished_at = now
            run.last_progress_at = now
            run.last_error = message
            recipe_program_runtime._record_program_event(
                run,
                "error",
                state=program_state,
                evaluation=recipe_program_runtime._evaluate_state_snapshot(program_state, now=now),
                payload={
                    "error": message,
                    "device_id": getattr(device, "device_id", None),
                    "actor": str(binding.get("actor") or "").strip(),
                },
            )
    except Exception:
        app.logger.warning(
            "Manual reconciler could not append recipe program error event for device %s.",
            getattr(device, "device_id", "unknown"),
            exc_info=True,
        )
    return message


def _load_ika_measurement_channels(device_id: int) -> dict[str, MeasurementChannel]:
    channel_codes = [spec["channel_code"] for spec in _IKA_TELEMETRY_CHANNELS]
    return {
        channel.channel_code: channel
        for channel in db.session.query(MeasurementChannel)
        .filter(
            MeasurementChannel.device_id == device_id,
            MeasurementChannel.channel_code.in_(channel_codes),
        )
        .all()
    }


def _load_measurement_channels(device_id: int, specs: tuple[dict, ...]) -> dict[str, MeasurementChannel]:
    channel_codes = [spec["channel_code"] for spec in specs]
    return {
        channel.channel_code: channel
        for channel in db.session.query(MeasurementChannel)
        .filter(
            MeasurementChannel.device_id == device_id,
            MeasurementChannel.channel_code.in_(channel_codes),
        )
        .all()
    }


def _ensure_measurement_channels(device: Device, specs: tuple[dict, ...]) -> dict[str, MeasurementChannel]:
    existing_channels = _load_measurement_channels(device.device_id, specs)
    needs_flush = False

    for spec in specs:
        channel = existing_channels.get(spec["channel_code"])
        if channel is None:
            channel = MeasurementChannel(
                device_id=device.device_id,
                channel_code=spec["channel_code"],
                display_name=spec["display_name"],
                unit=spec["unit"],
                value_type=str(spec.get("value_type") or "float"),
                is_active=True,
            )
            db.session.add(channel)
            existing_channels[channel.channel_code] = channel
            needs_flush = True
            continue

        if channel.display_name != spec["display_name"]:
            channel.display_name = spec["display_name"]
            needs_flush = True
        if channel.unit != spec["unit"]:
            channel.unit = spec["unit"]
            needs_flush = True
        expected_value_type = str(spec.get("value_type") or "float").strip().lower()
        if str(channel.value_type or "").strip().lower() != expected_value_type:
            channel.value_type = expected_value_type
            needs_flush = True
        if not bool(channel.is_active):
            channel.is_active = True
            needs_flush = True

    if needs_flush:
        db.session.flush()

    return existing_channels


def _ensure_ika_measurement_channels(device: Device) -> dict[str, MeasurementChannel]:
    return _ensure_measurement_channels(device, _IKA_TELEMETRY_CHANNELS)


def _ensure_huber_measurement_channels(device: Device) -> dict[str, MeasurementChannel]:
    return _ensure_measurement_channels(
        device,
        _CC230_TELEMETRY_CHANNELS if _is_cc230_device(device) else _HUBER_TELEMETRY_CHANNELS,
    )


def _ensure_manual_state(device: Device) -> DeviceManualState:
    state = db.session.get(DeviceManualState, device.device_id)
    if state is not None:
        return state

    try:
        state = DeviceManualState(
            device_id=device.device_id,
            queue_status="idle",
            desired_version=0,
            applied_version=0,
        )
        db.session.add(state)
        db.session.flush()
    except IntegrityError:
        # Another worker inserted the row first — re-read and return it.
        db.session.rollback()
        state = db.session.get(DeviceManualState, device.device_id)
        if state is None:
            raise RuntimeError(f"DeviceManualState for device {device.device_id} could not be created or found.")
    return state


def _ensure_manual_state_row(device_id: int) -> None:
    state = db.session.get(DeviceManualState, device_id)
    if state is not None:
        return
    try:
        db.session.add(
            DeviceManualState(
                device_id=device_id,
                queue_status="idle",
                desired_version=0,
                applied_version=0,
            )
        )
        db.session.flush()
    except IntegrityError:
        # Another Gunicorn worker or reconciler inserted the row first — safe to ignore.
        db.session.rollback()


def _telemetry_to_snapshot(state: DeviceManualState) -> dict[str, Any]:
    return {
        "is_on": bool(state.reported_is_on) if state.reported_is_on is not None else None,
        "setpoint_rpm": state.reported_setpoint_rpm,
        "actual_rpm": state.actual_rpm,
        "torque_ncm": state.torque_ncm,
        "active_control_sensor": state.active_control_sensor or "unknown",
        "updated_at": _datetime_isoformat(state.last_reported_at),
    }


def manual_state_to_dict(state: DeviceManualState | None) -> dict[str, Any] | None:
    if state is None:
        return None

    return {
        "device_id": state.device_id,
        "queue_status": str(state.queue_status or "idle"),
        "desired_version": int(state.desired_version or 0),
        "applied_version": int(state.applied_version or 0),
        "desired_state": {
            "is_on": bool(state.desired_is_on) if state.desired_is_on is not None else None,
            "speed": state.desired_speed,
            "requested_by": state.requested_by,
            "updated_at": _datetime_isoformat(state.last_desired_at),
        },
        "reported_state": _telemetry_to_snapshot(state),
        "last_error": state.last_error,
        "next_poll_at": _datetime_isoformat(state.next_poll_at),
        "watch_expires_at": _datetime_isoformat(state.watch_expires_at),
    }


def ensure_manual_state_snapshot(
    app: Flask,
    device: Device,
    *,
    requested_by: str,
    watch: bool,
    refresh: bool,
) -> DeviceManualState:
    device_id = int(device.device_id)

    def operation() -> DeviceManualState:
        _ensure_manual_state_row(device_id)
        now = _now_utc()
        values: dict[Any, Any] = {}
        if watch:
            values[DeviceManualState.watch_expires_at] = now + _manual_watch_ttl(app)
        if refresh:
            values[DeviceManualState.next_poll_at] = now
            values[DeviceManualState.queue_status] = case(
                (DeviceManualState.queue_status != "running", "queued"),
                else_=DeviceManualState.queue_status,
            )
        else:
            values[DeviceManualState.next_poll_at] = case(
                (DeviceManualState.last_reported_at.is_(None), now),
                else_=DeviceManualState.next_poll_at,
            )
            values[DeviceManualState.queue_status] = case(
                (
                    and_(
                        DeviceManualState.last_reported_at.is_(None),
                        DeviceManualState.queue_status != "running",
                    ),
                    "queued",
                ),
                else_=DeviceManualState.queue_status,
            )
        if values:
            db.session.query(DeviceManualState).filter_by(device_id=device_id).update(values, synchronize_session=False)
        db.session.flush()
        db.session.expire_all()
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            raise RuntimeError(f"DeviceManualState for device {device_id} disappeared during snapshot update.")
        return state

    return _run_with_transient_db_retry(operation, retry_duplicate_key=True)


def queue_manual_state_update(
    app: Flask,
    device: Device,
    *,
    desired_is_on: bool,
    desired_speed: int,
    requested_by: str,
) -> DeviceManualState:
    device_id = int(device.device_id)

    def operation() -> DeviceManualState:
        _ensure_manual_state_row(device_id)
        now = _now_utc()
        updated = (
            db.session.query(DeviceManualState)
            .filter_by(device_id=device_id)
            .update(
                {
                    DeviceManualState.desired_is_on: bool(desired_is_on),
                    DeviceManualState.desired_speed: int(desired_speed),
                    DeviceManualState.desired_version: DeviceManualState.desired_version + 1,
                    DeviceManualState.requested_by: requested_by,
                    DeviceManualState.last_desired_at: now,
                    DeviceManualState.watch_expires_at: now + _manual_watch_ttl(app),
                    DeviceManualState.next_poll_at: now,
                    DeviceManualState.queue_status: case(
                        (DeviceManualState.queue_status != "running", "queued"),
                        else_=DeviceManualState.queue_status,
                    ),
                    DeviceManualState.last_error: None,
                },
                synchronize_session=False,
            )
        )
        if not updated:
            raise RuntimeError(f"DeviceManualState for device {device_id} could not be queued.")
        db.session.flush()
        db.session.expire_all()
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            raise RuntimeError(f"DeviceManualState for device {device_id} disappeared during queue update.")
        return state

    return _run_with_transient_db_retry(operation, retry_duplicate_key=True)


def wait_for_manual_state_refresh(
    app: Flask,
    device_id: int,
    *,
    previous_reported_at: datetime | None,
    timeout_ms: int,
) -> DeviceManualState | None:
    timeout_seconds = max(0.0, timeout_ms / 1000.0)
    deadline = time.monotonic() + timeout_seconds
    previous_timestamp = _as_utc_datetime(previous_reported_at)

    while True:
        db.session.remove()
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            return None

        current_reported_at = _as_utc_datetime(state.last_reported_at)
        queue_status = str(state.queue_status or "idle").strip().lower()

        if previous_timestamp is None:
            if current_reported_at is not None or queue_status not in {"queued", "running"}:
                return state
        elif current_reported_at is not None and current_reported_at > previous_timestamp:
            return state
        elif queue_status == "error":
            return state

        if time.monotonic() >= deadline:
            return state

        time.sleep(0.05)


def _run_logged_manual_command(
    device: Device,
    command_text: str,
    *,
    priority: int,
    source: str,
) -> str | None:
    try:
        execution = dispatch_device_command(
            device,
            DeviceCommand(
                device_id=device.device_id,
                command_type="manual_text",
                payload=_manual_command_payload(command_text),
                priority=priority,
                source=source,
                requested_by="manual_reconciler",
            ),
        )
    except DeviceCommandError as exc:
        if exc.command is not None:
            db.session.commit()
        raise

    db.session.commit()
    return execution.result.response_text


def _run_logged_driver_command(
    device: Device,
    command_name: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int,
    source: str,
) -> Any:
    try:
        execution = dispatch_device_command(
            device,
            DeviceCommand(
                device_id=device.device_id,
                command_type=command_name,
                payload=payload or {},
                priority=priority,
                source=source,
                requested_by="manual_reconciler",
            ),
        )
    except DeviceCommandError as exc:
        if exc.command is not None:
            try:
                db.session.commit()
            except Exception:
                # A MySQL deadlock can silently roll back the outer transaction,
                # leaving the session with references to rows that no longer exist.
                # Rolling back here ensures a clean session for the next operation.
                db.session.rollback()
        raise

    db.session.commit()
    return execution.result.metadata.get("value")


def _read_ika_status(device: Device) -> dict[str, float | None]:
    setpoint_response = _run_logged_manual_command(
        device,
        "IN_SP_4",
        priority=CommandPriority.POLLING,
        source=CommandSource.POLLER,
    )
    actual_response = _run_logged_manual_command(
        device,
        "IN_PV_4",
        priority=CommandPriority.POLLING,
        source=CommandSource.POLLER,
    )
    torque_response = _run_logged_manual_command(
        device,
        "IN_PV_5",
        priority=CommandPriority.POLLING,
        source=CommandSource.POLLER,
    )

    setpoint = _parse_ika_numeric_response(setpoint_response)
    actual = _parse_ika_numeric_response(actual_response)
    torque = _parse_ika_numeric_response(torque_response)

    # If every channel returned None (empty or non-numeric), the device is not
    # communicating properly.  Treat this as an explicit failure so that the
    # reconciler stores a visible error instead of silently treating the command
    # as successfully applied.
    if setpoint is None and actual is None and torque is None:
        raise RuntimeError(
            "Stirrer returned no valid data on any channel "
            f"(IN_SP_4={setpoint_response!r}, IN_PV_4={actual_response!r}, "
            f"IN_PV_5={torque_response!r}). "
            "The device may still be booting after a power cycle, or the "
            "connection is broken. Will retry automatically."
        )

    return {
        "setpoint_rpm": setpoint,
        "actual_rpm": actual,
        "torque_ncm": torque,
    }


def _read_huber_status(device: Device) -> dict[str, Any]:
    if _is_cc230_device(device):
        # Keep CC230 live polling bounded to one scheduled command per cycle.
        # The driver still performs short fallback reads internally, but we avoid
        # five separate queue/lock/DB round-trips for a single telemetry refresh.
        poll_payload = {"response_timeout_ms": _CC230_POLL_RESPONSE_TIMEOUT_MS}
    else:
        poll_payload = {}

    telemetry = _run_logged_driver_command(
        device,
        "read_live_telemetry",
        poll_payload,
        priority=CommandPriority.POLLING,
        source=CommandSource.POLLER,
    )
    if not isinstance(telemetry, dict):
        raise RuntimeError("Huber live telemetry did not return a structured payload.")
    return telemetry


def _manual_state_next_poll_at(app: Flask, *, watch_active: bool, bg_interval: timedelta, measured_at: datetime) -> datetime:
    if watch_active:
        return measured_at + _manual_poll_interval(app)
    return measured_at + bg_interval


def _reported_ika_values(telemetry: dict[str, float | None]) -> dict[str, Any]:
    setpoint = telemetry.get("setpoint_rpm")
    actual = telemetry.get("actual_rpm")
    torque = telemetry.get("torque_ncm")
    reported_setpoint = None if setpoint is None else max(0, int(round(setpoint)))
    return {
        "reported_setpoint_rpm": reported_setpoint,
        "actual_rpm": actual,
        "torque_ncm": torque,
        "reported_is_on": bool(actual is not None and actual > 0.5),
    }


def _update_manual_state_row(
    device_id: int,
    *,
    values_factory,
    memory_update,
) -> DeviceManualState:
    session = db.session
    if not hasattr(session, "query"):
        state = session.get(DeviceManualState, device_id)
        if state is None:
            raise RuntimeError(f"DeviceManualState for device {device_id} disappeared during manual-state update.")
        memory_update(state)
        session.commit()
        return state

    def operation() -> DeviceManualState:
        updated = (
            db.session.query(DeviceManualState)
            .filter_by(device_id=device_id)
            .update(values_factory(), synchronize_session=False)
        )
        if not updated:
            raise RuntimeError(f"DeviceManualState for device {device_id} could not be updated.")
        db.session.commit()
        if hasattr(db.session, "expire_all"):
            db.session.expire_all()
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            raise RuntimeError(f"DeviceManualState for device {device_id} disappeared after manual-state update.")
        return state

    return _run_with_transient_db_retry(operation)


def _commit_ika_manual_state_success(
    app: Flask,
    *,
    device_id: int,
    telemetry: dict[str, float | None],
    measured_at: datetime,
    desired_pending: bool,
    processed_version: int,
    watch_active: bool,
    bg_interval: timedelta,
) -> DeviceManualState:
    reported = _reported_ika_values(telemetry)
    next_poll_at = _manual_state_next_poll_at(
        app,
        watch_active=watch_active,
        bg_interval=bg_interval,
        measured_at=measured_at,
    )
    initial_desired_speed = int(reported["reported_setpoint_rpm"] or 0)

    def values_factory() -> dict[Any, Any]:
        initial_condition = and_(
            DeviceManualState.desired_version == 0,
            DeviceManualState.desired_is_on.is_(None),
        )
        values: dict[Any, Any] = {
            DeviceManualState.reported_setpoint_rpm: reported["reported_setpoint_rpm"],
            DeviceManualState.actual_rpm: reported["actual_rpm"],
            DeviceManualState.torque_ncm: reported["torque_ncm"],
            DeviceManualState.reported_is_on: reported["reported_is_on"],
            DeviceManualState.last_reported_at: measured_at,
            DeviceManualState.desired_is_on: case(
                (initial_condition, reported["reported_is_on"]),
                else_=DeviceManualState.desired_is_on,
            ),
            DeviceManualState.desired_speed: case(
                (initial_condition, initial_desired_speed),
                else_=DeviceManualState.desired_speed,
            ),
            DeviceManualState.last_error: None,
            DeviceManualState.next_poll_at: next_poll_at,
            DeviceManualState.queue_status: "idle",
            DeviceManualState.lease_owner: None,
            DeviceManualState.lease_expires_at: None,
        }
        if desired_pending:
            values[DeviceManualState.applied_version] = int(processed_version)
        return values

    def memory_update(state: DeviceManualState) -> None:
        state.reported_setpoint_rpm = reported["reported_setpoint_rpm"]
        state.actual_rpm = reported["actual_rpm"]
        state.torque_ncm = reported["torque_ncm"]
        state.reported_is_on = reported["reported_is_on"]
        state.last_reported_at = measured_at
        if state.desired_version == 0 and state.desired_is_on is None:
            state.desired_is_on = bool(state.reported_is_on)
            state.desired_speed = initial_desired_speed
        if desired_pending:
            state.applied_version = int(processed_version)
        state.last_error = None
        state.next_poll_at = next_poll_at
        _release_manual_state_lease(state, status="idle")

    return _update_manual_state_row(device_id, values_factory=values_factory, memory_update=memory_update)


def _commit_huber_manual_state_success(
    app: Flask,
    *,
    device_id: int,
    measured_at: datetime,
    watch_active: bool,
    bg_interval: timedelta,
) -> DeviceManualState:
    next_poll_at = _manual_state_next_poll_at(
        app,
        watch_active=watch_active,
        bg_interval=bg_interval,
        measured_at=measured_at,
    )

    def values_factory() -> dict[Any, Any]:
        return {
            DeviceManualState.last_reported_at: measured_at,
            DeviceManualState.applied_version: case(
                (DeviceManualState.desired_version == 0, 0),
                else_=DeviceManualState.applied_version,
            ),
            DeviceManualState.last_error: None,
            DeviceManualState.next_poll_at: next_poll_at,
            DeviceManualState.queue_status: "idle",
            DeviceManualState.lease_owner: None,
            DeviceManualState.lease_expires_at: None,
        }

    def memory_update(state: DeviceManualState) -> None:
        state.last_reported_at = measured_at
        if state.desired_version == 0:
            state.applied_version = 0
        state.last_error = None
        state.next_poll_at = next_poll_at
        _release_manual_state_lease(state, status="idle")

    return _update_manual_state_row(device_id, values_factory=values_factory, memory_update=memory_update)


def _commit_manual_state_release(
    device_id: int,
    *,
    status: str,
    next_poll_at: datetime | None | object = _UNCHANGED,
    last_error: str | None | object = _UNCHANGED,
    applied_version: int | object = _UNCHANGED,
) -> DeviceManualState:
    def values_factory() -> dict[Any, Any]:
        values: dict[Any, Any] = {
            DeviceManualState.queue_status: status,
            DeviceManualState.lease_owner: None,
            DeviceManualState.lease_expires_at: None,
        }
        if next_poll_at is not _UNCHANGED:
            values[DeviceManualState.next_poll_at] = next_poll_at
        if last_error is not _UNCHANGED:
            values[DeviceManualState.last_error] = last_error
        if applied_version is not _UNCHANGED:
            values[DeviceManualState.applied_version] = int(applied_version)
        return values

    def memory_update(state: DeviceManualState) -> None:
        _release_manual_state_lease(state, status=status)
        if next_poll_at is not _UNCHANGED:
            state.next_poll_at = next_poll_at
        if last_error is not _UNCHANGED:
            state.last_error = last_error
        if applied_version is not _UNCHANGED:
            state.applied_version = int(applied_version)

    return _update_manual_state_row(device_id, values_factory=values_factory, memory_update=memory_update)


def _persist_telemetry_as_measurements(
    device: Device,
    telemetry: dict[str, Any],
    measured_at: datetime,
    *,
    specs: tuple[dict, ...],
    channels: dict[str, MeasurementChannel],
) -> None:
    """Write telemetry values to the measurement table.

    Called after every successful telemetry poll so that the complete
    history is available for the process-view plot and the Data export.
    """
    for spec in specs:
        value = telemetry.get(spec["key"])
        if value is None:
            continue

        channel = channels.get(spec["channel_code"])
        if channel is None:
            continue

        value_type = str(spec.get("value_type") or channel.value_type or "float").strip().lower()
        if value_type == "text":
            numeric_value = None
            text_value = str(value)[:255]
        else:
            numeric_value = float(value)
            text_value = None

        db.session.add(
            Measurement(
                device_id=device.device_id,
                channel_id=channel.channel_id,
                channel_code=channel.channel_code,
                measured_at=measured_at,
                numeric_value=numeric_value,
                text_value=text_value,
                unit=channel.unit,
                # Must stay inside the measurement schema's allowed source enum.
                source=_IKA_TELEMETRY_MEASUREMENT_SOURCE,
            )
        )

    db.session.flush()


def _persist_ika_telemetry_as_measurements(
    device: Device,
    telemetry: dict[str, float | None],
    measured_at: datetime,
) -> None:
    channels = _ensure_ika_measurement_channels(device)
    _persist_telemetry_as_measurements(
        device,
        telemetry,
        measured_at,
        specs=_IKA_TELEMETRY_CHANNELS,
        channels=channels,
    )


def _persist_huber_telemetry_as_measurements(
    device: Device,
    telemetry: dict[str, Any],
    measured_at: datetime,
) -> None:
    channels = _ensure_huber_measurement_channels(device)
    specs = _CC230_TELEMETRY_CHANNELS if _is_cc230_device(device) else _HUBER_TELEMETRY_CHANNELS
    _persist_telemetry_as_measurements(
        device,
        telemetry,
        measured_at,
        specs=specs,
        channels=channels,
    )


def _persist_ika_telemetry_as_measurements_best_effort(
    app: Flask,
    device: Device,
    telemetry: dict[str, float | None],
    measured_at: datetime,
) -> None:
    session = db.session
    if not all(hasattr(session, attr) for attr in ("query", "add", "flush", "commit", "rollback")):
        # Simplified unit-test doubles may not implement the full SQLAlchemy
        # session API. Skip history persistence there so the control-state path
        # can still be exercised independently.
        return

    try:
        def operation() -> None:
            _persist_ika_telemetry_as_measurements(device, telemetry, measured_at)
            db.session.commit()

        _run_with_transient_db_retry(operation, retry_duplicate_key=True)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning(
            "Measurement persistence failed for device %s; keeping live manual-state update and retrying history on the next poll.",
            device.device_id,
            exc_info=True,
        )


def _persist_huber_telemetry_as_measurements_best_effort(
    app: Flask,
    device: Device,
    telemetry: dict[str, float | None],
    measured_at: datetime,
) -> None:
    session = db.session
    if not all(hasattr(session, attr) for attr in ("query", "add", "flush", "commit", "rollback")):
        return

    try:
        def operation() -> None:
            _persist_huber_telemetry_as_measurements(device, telemetry, measured_at)
            db.session.commit()

        _run_with_transient_db_retry(operation, retry_duplicate_key=True)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning(
            "Measurement persistence failed for Huber device %s; keeping live state update and retrying history on the next poll.",
            device.device_id,
            exc_info=True,
        )


def _apply_desired_ika_state(device: Device, state: DeviceManualState) -> None:
    desired_is_on = bool(state.desired_is_on)
    desired_speed = max(0, int(state.desired_speed or 0))

    if desired_is_on:
        _run_logged_manual_command(
            device,
            "START_4",
            priority=CommandPriority.MANUAL,
            source=CommandSource.MANUAL_RECONCILER,
        )
        # Give the device time to process the start command before sending
        # the setpoint.  0.5 s is more robust than 0.18 s, especially after
        # a power cycle when firmware may not be fully ready.
        time.sleep(0.5)
        _run_logged_manual_command(
            device,
            f"OUT_SP_4 {desired_speed}",
            priority=CommandPriority.MANUAL,
            source=CommandSource.MANUAL_RECONCILER,
        )
        time.sleep(0.5)

        # Verify the setpoint was accepted: a None response means the device
        # is not communicating (e.g. still booting).  Raise so the reconciler
        # stores a visible error and retries on the next cycle.
        sp_response = _run_logged_manual_command(
            device,
            "IN_SP_4",
            priority=CommandPriority.MANUAL,
            source=CommandSource.MANUAL_RECONCILER,
        )
        sp_value = _parse_ika_numeric_response(sp_response)
        if sp_value is None:
            raise RuntimeError(
                f"Stirrer did not confirm setpoint after START command "
                f"(IN_SP_4 returned {sp_response!r}). "
                "The device may still be booting. Will retry automatically."
            )
        # Detect device-level clamping: the IKA panel has a physical speed limit
        # (Menu → Speed Limit) that silently caps OUT_SP_4 regardless of what we
        # send.  A mismatch of more than 5 rpm means the physical limit is too low.
        if desired_speed > 0 and sp_value < desired_speed - 5:
            raise RuntimeError(
                f"Device accepted {int(round(sp_value))} rpm instead of the "
                f"requested {desired_speed} rpm. The physical speed limit on "
                f"the IKA panel is set too low. Please raise it to at least "
                f"{desired_speed} rpm via the device menu (Speed Limit)."
            )
        return

    _run_logged_manual_command(
        device,
        "STOP_4",
        priority=CommandPriority.MANUAL,
        source=CommandSource.MANUAL_RECONCILER,
    )
    # Give device time to process the stop command before subsequent reads.
    time.sleep(0.5)


def _ensure_manual_states_for_active_devices(app: Flask) -> None:
    """Create DeviceManualState rows for active supported devices that have no state row yet.

    This seeds background telemetry polling for devices that have never been
    accessed through the Process UI, so measurements are stored continuously
    regardless of whether any user has the page open.

    Each device is committed individually so that an IntegrityError from a
    concurrent Gunicorn worker inserting the same row does not roll back all
    other devices.
    """
    active_recipe_device_ids = _active_recipe_program_device_ids()
    active_devices_query = db.session.query(Device).filter(
        Device.protocol.in_(("ika_eurostar_60", *_HUBER_PROTOCOLS)),
        Device.is_active.is_(True),
    )
    if active_recipe_device_ids is not None:
        if not active_recipe_device_ids:
            return
        active_devices_query = active_devices_query.filter(Device.device_id.in_(active_recipe_device_ids))
    active_devices = active_devices_query.all()
    seeded_states = 0
    seeded_channels = 0
    for device in active_devices:
        try:
            added_state = False
            existing_state = db.session.get(DeviceManualState, device.device_id)
            if existing_state is None:
                db.session.add(
                    DeviceManualState(
                        device_id=device.device_id,
                        queue_status="idle",
                        desired_version=0,
                        applied_version=0,
                    )
                )
                added_state = True
            if _is_huber_device(device):
                specs = _CC230_TELEMETRY_CHANNELS if _is_cc230_device(device) else _HUBER_TELEMETRY_CHANNELS
                ensure_channels = _ensure_huber_measurement_channels
            else:
                specs = _IKA_TELEMETRY_CHANNELS
                ensure_channels = _ensure_ika_measurement_channels
            existing_channels = _load_measurement_channels(device.device_id, specs)
            ensure_channels(device)
            added_channels = max(0, len(specs) - len(existing_channels))
            db.session.commit()  # Commit individually to survive multi-worker races
            if added_state:
                seeded_states += 1
            seeded_channels += added_channels
        except IntegrityError:
            # Another Gunicorn worker inserted the row first — safe to ignore.
            db.session.rollback()
        except Exception:
            db.session.rollback()
            app.logger.warning(
                "Measurement poller: failed to seed DeviceManualState for device %s.",
                device.device_id,
                exc_info=True,
            )
    if seeded_states or seeded_channels:
        app.logger.info(
            "Measurement poller: seeded %d manual-state row(s) and %d measurement channel(s) for active supported device(s).",
            seeded_states,
            seeded_channels,
        )


def _ensure_manual_states_for_active_ika_devices(app: Flask) -> None:
    _ensure_manual_states_for_active_devices(app)


def _release_manual_state_lease(state: DeviceManualState, *, status: str) -> None:
    state.queue_status = status
    state.lease_owner = None
    state.lease_expires_at = None


def _process_manual_state(app: Flask, *, device_id: int, worker_id: str) -> None:
    state = db.session.get(DeviceManualState, device_id)
    if state is None or state.lease_owner != worker_id:
        return

    now = _now_utc()
    device = db.session.get(Device, device_id)
    watch_expires_at = _as_utc_datetime(state.watch_expires_at)
    next_poll_at = _as_utc_datetime(state.next_poll_at)
    watch_active = bool(watch_expires_at and watch_expires_at > now)
    desired_pending = int(state.desired_version or 0) > int(state.applied_version or 0)
    # UI-driven poll: only when a browser has the Process page open.
    ui_poll_due = watch_active and (next_poll_at is None or next_poll_at <= now)
    # Background poll: fires even with no UI session so measurements are stored
    # continuously.  Uses a longer interval than the live UI poll cadence.
    bg_interval = _background_poll_interval(app)
    last_reported = _as_utc_datetime(state.last_reported_at)
    bg_poll_due = last_reported is None or last_reported + bg_interval <= now
    poll_due = ui_poll_due or bg_poll_due

    if device is None or not _supports_manual_runtime(device):
        _commit_manual_state_release(
            device_id,
            status="error",
            last_error="Manual runtime is not supported for this device.",
        )
        return
    is_huber = _is_huber_device(device)

    if not desired_pending and not poll_due:
        _commit_manual_state_release(device_id, status="idle")
        return

    processed_version = int(state.desired_version or 0)
    desired_snapshot = SimpleNamespace(
        desired_is_on=state.desired_is_on,
        desired_speed=state.desired_speed,
    )
    active_recipe_device_ids = _active_recipe_program_device_ids()
    sequence_lock_required = active_recipe_device_ids is not None and int(device_id) in active_recipe_device_ids
    db.session.commit()

    try:
        lock_context = _manual_recipe_sequence_lock() if sequence_lock_required else _no_sequence_lock()
        with lock_context as sequence_lock_acquired:
            if not sequence_lock_acquired:
                _commit_manual_state_release(
                    device_id,
                    status="queued",
                    next_poll_at=_now_utc() + timedelta(milliseconds=250),
                )
                return

            if is_huber:
                if desired_pending:
                    raise RuntimeError("Queued manual-state writes are not supported for Huber devices.")
                telemetry = _read_huber_status(device)
                measured_at = _now_utc()
                _commit_huber_manual_state_success(
                    app,
                    device_id=device_id,
                    measured_at=measured_at,
                    watch_active=watch_active,
                    bg_interval=bg_interval,
                )
                _persist_huber_telemetry_as_measurements_best_effort(app, device, telemetry, measured_at)
                return

            if desired_pending:
                _apply_desired_ika_state(device, desired_snapshot)

            telemetry = _read_ika_status(device)
            measured_at = _now_utc()
            reported = _reported_ika_values(telemetry)
            if desired_pending:
                # Final sanity check: if the desired state was ON but the setpoint
                # came back as None in the post-apply telemetry, the device silently
                # dropped our command (e.g. a second boot glitch).  Do NOT mark as
                # applied so the reconciler retries on the next cycle.
                if bool(desired_snapshot.desired_is_on) and reported["reported_setpoint_rpm"] is None:
                    raise RuntimeError(
                        "Stirrer accepted the START command but setpoint reads as "
                        "None in the subsequent telemetry poll. The device may have "
                        "reset. Will retry automatically."
                    )
            _commit_ika_manual_state_success(
                app,
                device_id=device_id,
                telemetry=telemetry,
                measured_at=measured_at,
                desired_pending=desired_pending,
                processed_version=processed_version,
                watch_active=watch_active,
                bg_interval=bg_interval,
            )
            _persist_ika_telemetry_as_measurements_best_effort(app, device, telemetry, measured_at)
    except Exception as exc:
        db.session.rollback()
        if isinstance(exc, DeviceCommandError) and is_runtime_interrupted_error(exc):
            _commit_manual_state_release(
                device_id,
                status="queued",
                next_poll_at=_now_utc() + timedelta(milliseconds=250),
            )
            return
        if isinstance(exc, OperationalError) and _is_transient_mysql_error(exc):
            try:
                _commit_manual_state_release(
                    device_id,
                    status="queued",
                    next_poll_at=_now_utc() + timedelta(milliseconds=250),
                )
            except Exception:
                app.logger.warning(
                    "Manual reconciler could not reschedule device %s after transient database conflict.",
                    device_id,
                    exc_info=True,
                )
            app.logger.warning(
                "Manual reconciler hit a transient database conflict for device %s; rescheduled without failing the recipe.",
                device_id,
            )
            return
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            return
        recipe_program_error = _fail_active_recipe_program_for_device(app, device, exc)
        fallback_error = describe_device_command_error(exc) if isinstance(exc, DeviceCommandError) else str(exc)
        # Use the background interval for retry when no UI session is watching so
        # an unreachable device is not hammered on every reconciler tick.
        retry_interval = _manual_poll_interval(app) if watch_active else _background_poll_interval(app)
        _commit_manual_state_release(
            device_id,
            status="error",
            next_poll_at=_now_utc() + retry_interval,
            last_error=recipe_program_error or fallback_error,
            applied_version=processed_version if recipe_program_error and desired_pending else _UNCHANGED,
        )
        app.logger.warning("Manual reconciler failed for device %s: %s", device_id, exc)


def _claim_next_device_id(app: Flask, worker_id: str) -> int | None:
    now = _now_utc()
    # Background telemetry cutoff: poll even without an active UI session.
    bg_cutoff = now - _background_poll_interval(app)
    active_recipe_device_ids = _active_recipe_program_device_ids()
    active_recipe_priority_order = _active_recipe_device_priority_order(now)
    include_port_order = _manual_claim_port_order_available()
    selected_columns = [
        DeviceManualState.device_id,
        DeviceManualState.desired_version,
        DeviceManualState.applied_version,
        DeviceManualState.next_poll_at,
        DeviceManualState.last_desired_at,
    ]
    if include_port_order:
        selected_columns.append(DeviceConnection.port_number)

    candidate_query = (
        db.session.query(*selected_columns)
        .join(Device, Device.device_id == DeviceManualState.device_id)
        .filter(
            Device.is_active.is_(True),
            Device.protocol.in_(("ika_eurostar_60", *_HUBER_PROTOCOLS)),
            or_(DeviceManualState.lease_expires_at.is_(None), DeviceManualState.lease_expires_at < now),
            or_(
                # Explicit command pending
                DeviceManualState.desired_version > DeviceManualState.applied_version,
                # UI-driven live poll
                and_(
                    DeviceManualState.watch_expires_at.is_not(None),
                    DeviceManualState.watch_expires_at > now,
                    or_(DeviceManualState.next_poll_at.is_(None), DeviceManualState.next_poll_at <= now),
                ),
                # Background telemetry poll: device hasn't been read recently AND
                # its scheduled retry time (if any) has passed.  The retry time is
                # set to bg_interval after both successes and failures, so this
                # prevents hammering an unreachable device.
                and_(
                    or_(
                        Device.protocol == "ika_eurostar_60",
                        Device.protocol.in_(list(_HUBER_PROTOCOLS)),
                        and_(
                            DeviceManualState.watch_expires_at.is_not(None),
                            DeviceManualState.watch_expires_at > now,
                        ),
                        Device.device_id.in_(active_recipe_device_ids or []),
                    ),
                    or_(
                        DeviceManualState.last_reported_at.is_(None),
                        DeviceManualState.last_reported_at <= bg_cutoff,
                    ),
                    or_(
                        DeviceManualState.next_poll_at.is_(None),
                        DeviceManualState.next_poll_at <= now,
                    ),
                ),
            ),
        )
    )
    if include_port_order:
        candidate_query = (
            candidate_query
            .outerjoin(DeviceBindingCurrent, DeviceBindingCurrent.device_id == DeviceManualState.device_id)
            .outerjoin(DeviceConnection, DeviceConnection.connection_id == DeviceBindingCurrent.connection_id)
        )
    if active_recipe_device_ids is not None:
        if not active_recipe_device_ids:
            return None
        # Devices in an active recipe always take priority.  Non-recipe devices
        # with an active UI watch are also allowed so their live telemetry
        # continues to be stored even while the recipe program is running.
        candidate_query = candidate_query.filter(
            or_(
                DeviceManualState.device_id.in_(active_recipe_device_ids),
                and_(
                    DeviceManualState.watch_expires_at.is_not(None),
                    DeviceManualState.watch_expires_at > now,
                ),
            )
        )
    order_by_columns = [
        case((DeviceManualState.desired_version > DeviceManualState.applied_version, 0), else_=1),
        DeviceManualState.next_poll_at.asc(),
        DeviceManualState.last_desired_at.asc(),
    ]
    if include_port_order:
        order_by_columns.append(DeviceConnection.port_number.asc())
    order_by_columns.append(DeviceManualState.device_id.asc())

    candidates = (
        candidate_query.order_by(*order_by_columns)
        .limit(16)
        .all()
    )
    candidates = sorted(
        candidates,
        key=lambda row: _manual_claim_candidate_sort_key(
            row,
            active_recipe_priority_order=active_recipe_priority_order,
            active_recipe=active_recipe_device_ids is not None,
        ),
    )

    lease_until = now + _manual_lease_duration(app)
    for row in candidates:  # noqa: variable reuse
        device_id = int(row[0])
        try:
            claimed = (
                db.session.query(DeviceManualState)
                .filter(
                    DeviceManualState.device_id == device_id,
                    or_(DeviceManualState.lease_expires_at.is_(None), DeviceManualState.lease_expires_at < now),
                )
                .update(
                    {
                        DeviceManualState.lease_owner: worker_id,
                        DeviceManualState.lease_expires_at: lease_until,
                        DeviceManualState.queue_status: "running",
                    },
                    synchronize_session=False,
                )
            )
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if _is_transient_mysql_error(exc):
                time.sleep(0.05)
                continue
            raise
        if claimed:
            return int(device_id)

    return None


def _reconciler_loop(app: Flask, worker_id: str) -> None:
    loop_sleep = _manual_loop_sleep(app)
    last_discovery_at: float = 0.0

    while True:
        try:
            with app.app_context():
                # Periodically create DeviceManualState rows for newly registered
                # or previously-unseen active IKA devices so they get picked up
                # by background telemetry polling even before any UI session.
                now_ts = time.monotonic()
                if now_ts - last_discovery_at >= _DEVICE_DISCOVERY_INTERVAL_SECONDS:
                    try:
                        _ensure_manual_states_for_active_devices(app)
                    except Exception:
                        app.logger.exception("Device discovery failed; will retry on next cycle.")
                    finally:
                        # Always advance the timer so a broken DB doesn't busy-loop.
                        last_discovery_at = now_ts

                device_id = _claim_next_device_id(app, worker_id)
                if device_id is None:
                    db.session.remove()
                    time.sleep(loop_sleep)
                    continue
                _process_manual_state(app, device_id=device_id, worker_id=worker_id)
                db.session.remove()
        except Exception:
            with app.app_context():
                db.session.rollback()
                db.session.remove()
                app.logger.exception("Device manual reconciler loop crashed.")
            time.sleep(max(loop_sleep, 1.0))


def start_device_manual_reconciler(app: Flask) -> None:
    if not app.config.get("DEVICE_MANUAL_RECONCILER_ENABLED", True):
        return
    if app.config.get("SQLALCHEMY_DATABASE_URI") == "sqlite:///:memory:":
        return
    if app.extensions.get(_WORKER_EXTENSION_KEY):
        return

    worker_id = uuid4().hex
    thread = threading.Thread(
        target=_reconciler_loop,
        name=f"device-manual-reconciler-{worker_id[:8]}",
        args=(app, worker_id),
        daemon=True,
    )
    thread.start()
    app.extensions[_WORKER_EXTENSION_KEY] = thread
    app.logger.info(
        "Device manual reconciler started pid=%s thread_id=%s worker_id=%s",
        os.getpid(),
        thread.ident,
        worker_id,
    )
