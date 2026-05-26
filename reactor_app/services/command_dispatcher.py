"""Central command dispatcher.

All device commands should flow through ``dispatch_device_command()`` instead
of calling ``execute_device_command()`` directly. The dispatcher now inserts
commands into an in-process priority queue that is serviced by runtime worker
threads when a production Flask/SQLAlchemy app is available.

Fallback behaviour remains synchronous for unit tests and in-memory SQLite
setups so Phase 3 call sites keep working without a full worker bootstrap.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from flask import has_app_context

from ..extensions import db
from ..models import ControlCommand, Device, Measurement
from .cancellation import CancellationToken, CommandExecutionInterrupted
from .command_model import CommandPriority, CommandSource, DeviceCommand
from .device_runtime import (
    DeviceCommandError,
    ExecutedDeviceCommand,
    _command_interrupted_details,
    create_control_command_record,
    execute_device_command,
    transition_control_command_record,
)
from .runtime_scheduler import (
    RuntimeCommandInterruptedError,
    RuntimeCommandQueue,
    RuntimeCommandScheduler,
    ScheduledRuntimeCommand,
)
from .runtime_status import RuntimeStatus

logger = logging.getLogger(__name__)

_RUNTIME_SCHEDULER_EXTENSION_KEY = "runtime_command_scheduler"
_DEFAULT_RUNTIME_WORKER_COUNT = 2
_DEFAULT_TIMEOUT_POLICY: dict[int, dict[str, float]] = {
    int(CommandPriority.EMERGENCY_STOP): {
        "queue_timeout_s": 10.0,
        "execution_timeout_s": 6.0,
        "total_timeout_s": 15.0,
    },
    int(CommandPriority.SAFETY): {
        "queue_timeout_s": 10.0,
        "execution_timeout_s": 6.0,
        "total_timeout_s": 15.0,
    },
    int(CommandPriority.RECIPE): {
        "queue_timeout_s": 5.0,
        "execution_timeout_s": 6.0,
        "total_timeout_s": 12.0,
    },
    int(CommandPriority.MANUAL): {
        "queue_timeout_s": 5.0,
        "execution_timeout_s": 6.0,
        "total_timeout_s": 12.0,
    },
    int(CommandPriority.POLLING): {
        "queue_timeout_s": 1.0,
        "execution_timeout_s": 3.0,
        "total_timeout_s": 4.0,
    },
}
_RECOVERY_MANUAL_MAX_AGE_SECONDS = 15.0
_RECOVERY_SAFETY_MAX_AGE_SECONDS = 30.0

_KNOWN_SOURCES = frozenset(
    {
        "api",
        "recipe",
        "manual_reconciler",
        "poller",
        "system",
    }
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dt_iso(value: datetime | None) -> str | None:
    normalized = _as_utc_datetime(value)
    return normalized.isoformat() if normalized is not None else None


def _priority_label(priority: int) -> str:
    try:
        return CommandPriority(priority).name
    except ValueError:
        return str(priority)


def _resolve_flask_app(app: Any | None) -> Any | None:
    if app is not None:
        resolver = getattr(app, "_get_current_object", None)
        return resolver() if callable(resolver) else app

    if not has_app_context():
        return None

    from flask import current_app

    return current_app._get_current_object()


def _positive_int_config(app: Any, key: str, default: int) -> int:
    try:
        value = int(app.config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _float_from_config(value: Any, default: float | None) -> float | None:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _command_timeout_policy(app: Any | None, priority: int) -> dict[str, float]:
    resolved_priority = int(priority)
    if resolved_priority <= int(CommandPriority.EMERGENCY_STOP):
        policy_key = int(CommandPriority.EMERGENCY_STOP)
    elif resolved_priority <= int(CommandPriority.SAFETY):
        policy_key = int(CommandPriority.SAFETY)
    elif resolved_priority <= int(CommandPriority.RECIPE):
        policy_key = int(CommandPriority.RECIPE)
    elif resolved_priority <= int(CommandPriority.MANUAL):
        policy_key = int(CommandPriority.MANUAL)
    else:
        policy_key = int(CommandPriority.POLLING)

    policy = dict(_DEFAULT_TIMEOUT_POLICY[policy_key])
    if app is None:
        return policy

    configured = app.config.get("RUNTIME_COMMAND_TIMEOUT_POLICY")
    if not isinstance(configured, dict):
        return policy

    override = configured.get(CommandPriority(policy_key).name.lower())
    if not isinstance(override, dict):
        return policy

    for key in ("queue_timeout_s", "execution_timeout_s", "total_timeout_s"):
        if key in override:
            policy[key] = _float_from_config(override.get(key), policy[key]) or 0.0
    return policy


def _scheduler_supported(app: Any | None) -> bool:
    if app is None:
        return False
    if not bool(app.config.get("RUNTIME_COMMAND_SCHEDULER_ENABLED", True)):
        return False
    if app.config.get("SQLALCHEMY_DATABASE_URI") == "sqlite:///:memory:":
        return False
    return app.extensions.get("sqlalchemy") is not None


def _command_source_from_row(row: ControlCommand) -> str:
    source = str(getattr(row, "command_source", "") or "").strip().lower()
    if source in _KNOWN_SOURCES:
        return source

    requested_by = str(getattr(row, "requested_by", "") or "").strip().lower()
    if requested_by == "manual_reconciler":
        return CommandSource.MANUAL_RECONCILER
    if requested_by == "recipe_program":
        return CommandSource.RECIPE
    return CommandSource.API


def _command_priority_from_row(row: ControlCommand) -> int:
    raw_priority = getattr(row, "command_priority", None)
    try:
        return int(raw_priority)
    except (TypeError, ValueError):
        pass

    command_name = str(getattr(row, "command_name", "") or "").strip().lower()
    if command_name == "emergency_stop":
        return int(CommandPriority.EMERGENCY_STOP)

    source = _command_source_from_row(row)
    if source == CommandSource.POLLER:
        return int(CommandPriority.POLLING)
    if source == CommandSource.RECIPE:
        return int(CommandPriority.RECIPE)
    if source == CommandSource.SYSTEM:
        return int(CommandPriority.SAFETY)
    return int(CommandPriority.MANUAL)


def _command_from_row(row: ControlCommand) -> DeviceCommand:
    payload = row.command_payload if isinstance(row.command_payload, dict) else {}
    return DeviceCommand(
        device_id=int(row.device_id),
        command_type=str(row.command_name or "").strip(),
        payload=payload,
        priority=_command_priority_from_row(row),
        source=_command_source_from_row(row),
        requested_by=str(row.requested_by or "system").strip() or "system",
        queue_timeout_s=getattr(row, "queue_timeout_s", None),
        execution_timeout_s=getattr(row, "execution_timeout_s", None),
        total_deadline_at=_as_utc_datetime(getattr(row, "total_deadline_at", None)),
        correlation_id=str(row.correlation_id).strip() or None if getattr(row, "correlation_id", None) is not None else None,
        command_id=str(row.request_uuid or ""),
        created_at=_as_utc_datetime(getattr(row, "requested_at", None)) or _now_utc(),
    )


def _make_work_item(
    app: Any,
    command: DeviceCommand,
    *,
    acquire_lock: bool,
    control_command_id: int | None = None,
) -> ScheduledRuntimeCommand:
    item = ScheduledRuntimeCommand(
        command=command,
        acquire_lock=acquire_lock,
        control_command_id=control_command_id,
        execute=lambda: None,
    )
    item.execute = lambda item=item: _execute_with_worker_app(
        app,
        command,
        acquire_lock=item.acquire_lock,
        control_command_id=item.control_command_id,
        worker_id=item.worker_id,
        started_at=item.started_at,
        cancellation_token=item.cancellation_token,
    )
    return item


def _recovery_max_age_s(app: Any, row: ControlCommand) -> float:
    source = _command_source_from_row(row)
    priority = _command_priority_from_row(row)
    if priority <= int(CommandPriority.SAFETY) or source == CommandSource.SYSTEM:
        return _float_from_config(
            app.config.get("RUNTIME_COMMAND_RECOVERY_SAFETY_MAX_AGE_SECONDS"),
            _RECOVERY_SAFETY_MAX_AGE_SECONDS,
        ) or _RECOVERY_SAFETY_MAX_AGE_SECONDS
    return _float_from_config(
        app.config.get("RUNTIME_COMMAND_RECOVERY_MANUAL_MAX_AGE_SECONDS"),
        _RECOVERY_MANUAL_MAX_AGE_SECONDS,
    ) or _RECOVERY_MANUAL_MAX_AGE_SECONDS


def _recover_runtime_commands(app: Any, scheduler: RuntimeCommandScheduler) -> None:
    with app.app_context():
        try:
            active_statuses = tuple(sorted(RuntimeStatus.ACTIVE_STATES | {RuntimeStatus.ACKED}))
            rows = (
                ControlCommand.query.filter(ControlCommand.status.in_(active_statuses))
                .order_by(ControlCommand.requested_at.asc(), ControlCommand.command_id.asc())
                .all()
            )
            now = _now_utc()
            for row in rows:
                _recover_runtime_command_row(app, scheduler, row, now=now)
        except Exception:
            logger.debug("Runtime recovery skipped because the command tables are not yet available.", exc_info=True)
        finally:
            try:
                db.session.remove()
            except Exception:
                pass


def _recover_runtime_command_row(
    app: Any,
    scheduler: RuntimeCommandScheduler,
    row: ControlCommand,
    *,
    now: datetime,
) -> None:
    status = str(row.status or "").strip().lower()
    source = _command_source_from_row(row)
    priority = _command_priority_from_row(row)
    requested_at = _as_utc_datetime(getattr(row, "requested_at", None)) or now
    started_at = _as_utc_datetime(getattr(row, "started_at", None))
    total_deadline_at = _as_utc_datetime(getattr(row, "total_deadline_at", None))
    queue_timeout_s = getattr(row, "queue_timeout_s", None)
    execution_timeout_s = getattr(row, "execution_timeout_s", None)
    age_s = max(0.0, (now - requested_at).total_seconds())

    transition_control_command_record(
        row,
        RuntimeStatus.RECOVERING,
        event_payload={
            "previous_status": status,
            "worker_id": row.worker_id,
            "recovered_at": now.isoformat(),
        },
    )

    if status in {RuntimeStatus.RUNNING, RuntimeStatus.SENT, RuntimeStatus.QUEUED}:
        execution_deadline_exceeded = False
        if started_at is not None and execution_timeout_s not in (None, ""):
            execution_deadline_exceeded = started_at + timedelta(seconds=float(execution_timeout_s)) <= now
        total_deadline_exceeded = total_deadline_at is not None and total_deadline_at <= now
        next_status = RuntimeStatus.TIMEOUT if execution_deadline_exceeded or total_deadline_exceeded else RuntimeStatus.INTERRUPTED
        transition_control_command_record(
            row,
            next_status,
            event_payload={
                "message": "Recovered running command had no active worker after restart.",
                "recovered_at": now.isoformat(),
                "previous_status": status,
                "worker_id": row.worker_id,
            },
            finished_at=now,
            error_message="Worker process was not available during runtime recovery.",
        )
        return

    if status == RuntimeStatus.ACKED:
        transition_control_command_record(
            row,
            RuntimeStatus.COMPLETED,
            event_payload={
                "message": "Recovered acknowledged command was finalized as completed.",
                "recovered_at": now.isoformat(),
            },
            finished_at=_as_utc_datetime(row.finished_at) or now,
            error_message=None,
        )
        return

    if source == CommandSource.POLLER:
        transition_control_command_record(
            row,
            RuntimeStatus.SKIPPED,
            event_payload={
                "message": "Recovered polling command was skipped after restart.",
                "recovered_at": now.isoformat(),
            },
            finished_at=now,
        )
        return

    if total_deadline_at is not None and total_deadline_at <= now:
        transition_control_command_record(
            row,
            RuntimeStatus.EXPIRED,
            event_payload={
                "message": "Recovered command exceeded its total deadline before it could be replayed.",
                "recovered_at": now.isoformat(),
            },
            finished_at=now,
            error_message="Recovered command exceeded its total deadline.",
        )
        return

    if queue_timeout_s not in (None, "") and requested_at + timedelta(seconds=float(queue_timeout_s)) <= now:
        transition_control_command_record(
            row,
            RuntimeStatus.TIMEOUT,
            event_payload={
                "message": "Recovered command exceeded its queue timeout before it could be replayed.",
                "recovered_at": now.isoformat(),
            },
            finished_at=now,
            error_message="Recovered command exceeded its queue timeout.",
        )
        return

    if source == CommandSource.RECIPE:
        transition_control_command_record(
            row,
            RuntimeStatus.CANCELLED,
            event_payload={
                "message": "Recovered recipe command was discarded so the recipe runtime can recompute state.",
                "recovered_at": now.isoformat(),
            },
            finished_at=now,
        )
        return

    if age_s > _recovery_max_age_s(app, row):
        transition_control_command_record(
            row,
            RuntimeStatus.CANCELLED,
            event_payload={
                "message": "Recovered command was cancelled because it became stale during restart.",
                "recovered_at": now.isoformat(),
                "age_seconds": round(age_s, 3),
            },
            finished_at=now,
        )
        return

    command = _resolved_command(_command_from_row(row), app=app)
    work_item = _make_work_item(
        app,
        command,
        acquire_lock=True,
        control_command_id=int(row.command_id),
    )
    scheduler.queue.enqueue(work_item)


def get_runtime_command_scheduler(app: Any | None, *, start: bool = True) -> RuntimeCommandScheduler | None:
    app_obj = _resolve_flask_app(app)
    if not _scheduler_supported(app_obj):
        return None

    scheduler = app_obj.extensions.get(_RUNTIME_SCHEDULER_EXTENSION_KEY)
    if isinstance(scheduler, RuntimeCommandScheduler):
        if start and not scheduler.is_running():
            scheduler.start()
        return scheduler

    if not start:
        return None

    worker_count = _positive_int_config(
        app_obj,
        "RUNTIME_COMMAND_WORKER_COUNT",
        _DEFAULT_RUNTIME_WORKER_COUNT,
    )
    scheduler = RuntimeCommandScheduler(
        worker_count=worker_count,
        logger=logging.getLogger(f"{__name__}.runtime"),
        worker_name_prefix="runtime-command-worker",
        queue=RuntimeCommandQueue(
            logger=logging.getLogger(f"{__name__}.runtime.queue"),
            on_status_change=_runtime_status_callback(app_obj),
            on_cancel_requested=_runtime_cancel_callback(app_obj),
        ),
    )
    app_obj.extensions[_RUNTIME_SCHEDULER_EXTENSION_KEY] = scheduler
    _recover_runtime_commands(app_obj, scheduler)
    if start:
        scheduler.start()
    return scheduler


def start_runtime_command_scheduler(app: Any) -> RuntimeCommandScheduler | None:
    return get_runtime_command_scheduler(app, start=True)


def stop_runtime_command_scheduler(
    app: Any,
    *,
    cancel_pending: bool = True,
    timeout_s: float | None = 5.0,
) -> None:
    app_obj = _resolve_flask_app(app)
    if app_obj is None:
        return

    scheduler = app_obj.extensions.pop(_RUNTIME_SCHEDULER_EXTENSION_KEY, None)
    if isinstance(scheduler, RuntimeCommandScheduler):
        scheduler.stop(cancel_pending=cancel_pending, timeout_s=timeout_s)


def cancel_runtime_commands(
    app: Any,
    *,
    device_id: int | None = None,
    source: str | None = None,
    priority_gt: int | None = None,
    status: str = RuntimeStatus.CANCELLED,
    reason: str | None = None,
):
    scheduler = get_runtime_command_scheduler(app, start=False)
    if scheduler is None:
        return []
    cancelled = scheduler.cancel_pending(
        device_id=device_id,
        source=source,
        priority_gt=priority_gt,
        status=status,
        reason=reason,
    )
    running = scheduler.request_cancellation(
        device_id=device_id,
        source=source,
        priority_gt=priority_gt,
        reason=reason,
    )
    return [*cancelled, *running]


def clear_runtime_device_queue(app: Any, device_id: int):
    scheduler = get_runtime_command_scheduler(app, start=False)
    if scheduler is None:
        return []
    return scheduler.clear_device_queue(device_id)


def runtime_error_status(exc: DeviceCommandError) -> str | None:
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    value = str(details.get("runtime_status") or "").strip().lower()
    return value or None


def is_runtime_interrupted_error(exc: DeviceCommandError) -> bool:
    status = runtime_error_status(exc)
    return status in RuntimeStatus.INTERRUPTED_STATES


def _validate_command_source(command: DeviceCommand) -> None:
    if str(command.source or "").strip().lower() not in _KNOWN_SOURCES:
        raise DeviceCommandError(
            f"Unknown command source '{command.source}'.",
            status_code=400,
            details={"command_source": command.source},
        )


def _resolved_command(command: DeviceCommand, *, app: Any | None) -> DeviceCommand:
    resolved = deepcopy(command)
    if app is None:
        if resolved.total_deadline_at is not None:
            resolved.total_deadline_at = _as_utc_datetime(resolved.total_deadline_at)
        return resolved
    policy = _command_timeout_policy(app, resolved.priority)

    if resolved.queue_timeout_s is None:
        resolved.queue_timeout_s = policy["queue_timeout_s"]
    if resolved.execution_timeout_s is None:
        resolved.execution_timeout_s = policy["execution_timeout_s"]
    if resolved.total_deadline_at is None:
        resolved.total_deadline_at = resolved.created_at + timedelta(seconds=policy["total_timeout_s"])
    elif resolved.total_deadline_at.tzinfo is None or resolved.total_deadline_at.tzinfo.utcoffset(resolved.total_deadline_at) is None:
        resolved.total_deadline_at = resolved.total_deadline_at.replace(tzinfo=timezone.utc)
    else:
        resolved.total_deadline_at = resolved.total_deadline_at.astimezone(timezone.utc)
    return resolved


def _command_total_remaining_s(command: DeviceCommand, *, now: datetime | None = None) -> float | None:
    total_deadline_at = _as_utc_datetime(command.total_deadline_at)
    if total_deadline_at is None:
        return None
    current_time = _as_utc_datetime(now) or _now_utc()
    return max(0.0, (total_deadline_at - current_time).total_seconds())


def _command_execution_remaining_s(
    command: DeviceCommand,
    *,
    started_at: datetime | None,
    now: datetime | None = None,
) -> float | None:
    if command.execution_timeout_s in (None, ""):
        return _command_total_remaining_s(command, now=now)
    current_time = _as_utc_datetime(now) or _now_utc()
    if started_at is None:
        remaining = float(command.execution_timeout_s)
    else:
        started = _as_utc_datetime(started_at) or current_time
        remaining = max(0.0, float(command.execution_timeout_s) - (current_time - started).total_seconds())
    total_remaining = _command_total_remaining_s(command, now=current_time)
    if total_remaining is None:
        return remaining
    return max(0.0, min(remaining, total_remaining))


def _effective_execution_deadline(
    command: DeviceCommand,
    *,
    started_at: datetime | None,
) -> tuple[datetime, str, str] | None:
    candidates: list[tuple[datetime, str, str]] = []
    total_deadline_at = _as_utc_datetime(command.total_deadline_at)
    if total_deadline_at is not None:
        candidates.append(
            (
                total_deadline_at,
                RuntimeStatus.EXPIRED,
                "Command exceeded its total deadline during execution.",
            )
        )
    if started_at is not None and command.execution_timeout_s not in (None, ""):
        candidates.append(
            (
                started_at + timedelta(seconds=max(0.0, float(command.execution_timeout_s))),
                RuntimeStatus.TIMEOUT,
                "Command exceeded its execution timeout during execution.",
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])


def _configure_execution_cancellation_token(
    command: DeviceCommand,
    *,
    started_at: datetime | None,
    cancellation_token: CancellationToken | None,
) -> CancellationToken | None:
    if cancellation_token is None:
        return None
    effective_deadline = _effective_execution_deadline(command, started_at=started_at)
    if effective_deadline is None:
        cancellation_token.clear_deadline()
        return cancellation_token
    deadline_at, deadline_status, deadline_message = effective_deadline
    cancellation_token.set_deadline(
        deadline_at,
        status=deadline_status,
        reason=deadline_message,
        source="command_dispatcher",
    )
    return cancellation_token


def _cap_payload_timeout_ms(payload: dict[str, Any], key: str, max_timeout_ms: int) -> None:
    try:
        current = int(payload.get(key)) if payload.get(key) not in (None, "") else None
    except (TypeError, ValueError):
        current = None
    if current is None:
        payload[key] = max_timeout_ms
        return
    payload[key] = max(1, min(current, max_timeout_ms))


def _effective_payload(
    command: DeviceCommand,
    *,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    effective_payload: dict[str, Any] = dict(command.payload)
    deadline_timeouts_s: list[float] = []
    if command.timeout_s is not None:
        deadline_timeouts_s.append(max(0.0, float(command.timeout_s)))
    execution_remaining_s = _command_execution_remaining_s(command, started_at=started_at)
    if execution_remaining_s is not None:
        deadline_timeouts_s.append(execution_remaining_s)
    total_remaining_s = _command_total_remaining_s(command)
    if total_remaining_s is not None:
        deadline_timeouts_s.append(total_remaining_s)

    if deadline_timeouts_s:
        timeout_ms = max(1, int(min(deadline_timeouts_s) * 1000))
        for key in ("response_timeout_ms", "connect_timeout_ms", "write_timeout_ms"):
            _cap_payload_timeout_ms(effective_payload, key, timeout_ms)
    return effective_payload


def _control_command_row(command_id: int | None = None, *, request_uuid: str | None = None) -> ControlCommand | None:
    if command_id is not None:
        return db.session.get(ControlCommand, int(command_id))
    if request_uuid in (None, ""):
        return None
    return ControlCommand.query.filter_by(request_uuid=str(request_uuid)).one_or_none()


def _persist_pending_command(app: Any, command: DeviceCommand) -> ControlCommand:
    with app.app_context():
        try:
            existing = _control_command_row(request_uuid=command.command_id)
            if existing is not None:
                return existing
            row = create_control_command_record(
                device_id=command.device_id,
                request_uuid=command.command_id,
                requested_by=command.requested_by,
                command_name=command.command_type,
                command_payload=command.payload,
                status=RuntimeStatus.PENDING,
                requested_at=command.created_at,
                scheduled_for=command.created_at,
                command_source=command.source,
                command_priority=int(command.priority),
                correlation_id=command.correlation_id,
                queue_timeout_s=command.queue_timeout_s,
                execution_timeout_s=command.execution_timeout_s,
                total_deadline_at=command.total_deadline_at,
                event_payload={
                    "requested_by": command.requested_by,
                    "command_source": command.source,
                    "command_priority": int(command.priority),
                    "queue_timeout_s": command.queue_timeout_s,
                    "execution_timeout_s": command.execution_timeout_s,
                    "total_deadline_at": _dt_iso(command.total_deadline_at),
                },
            )
            db.session.refresh(row)
            return row
        finally:
            try:
                db.session.remove()
            except Exception:
                pass


def _execute_direct(
    device: Any,
    command: DeviceCommand,
    *,
    acquire_lock: bool,
    command_record: ControlCommand | None = None,
    worker_id: str | None = None,
    started_at: datetime | None = None,
    cancellation_token: CancellationToken | None = None,
) -> ExecutedDeviceCommand:
    configured_token = _configure_execution_cancellation_token(
        command,
        started_at=started_at,
        cancellation_token=cancellation_token,
    )
    return execute_device_command(
        device,
        command_name=command.command_type,
        payload=_effective_payload(command, started_at=started_at),
        requested_by=command.requested_by,
        acquire_lock=acquire_lock,
        command_record=command_record,
        request_uuid=command.command_id,
        command_source=command.source,
        command_priority=int(command.priority),
        correlation_id=command.correlation_id,
        worker_id=worker_id,
        requested_at=command.created_at,
        scheduled_for=command.created_at,
        started_at=started_at,
        queue_timeout_s=command.queue_timeout_s,
        execution_timeout_s=command.execution_timeout_s,
        total_deadline_at=command.total_deadline_at,
        cancellation_token=configured_token,
    )


def _persist_runtime_status(
    app: Any,
    item: ScheduledRuntimeCommand,
    status: str,
    payload: dict[str, Any],
) -> None:
    with app.app_context():
        try:
            row = _control_command_row(item.control_command_id, request_uuid=item.command.command_id)
            if row is None:
                return
            current_status = str(row.status or "").strip().lower()
            if current_status == status and status in (RuntimeStatus.TERMINAL | {RuntimeStatus.PENDING}):
                return
            transition_control_command_record(
                row,
                status,
                event_payload=payload or None,
                scheduled_for=item.enqueued_at,
                started_at=item.started_at or row.started_at,
                worker_id=item.worker_id or row.worker_id,
                command_source=item.command.source,
                command_priority=int(item.command.priority),
                correlation_id=item.command.correlation_id,
                queue_timeout_s=item.command.queue_timeout_s,
                execution_timeout_s=item.command.execution_timeout_s,
                total_deadline_at=item.command.total_deadline_at,
                finished_at=item.finished_at if status in RuntimeStatus.TERMINAL else row.finished_at,
                error_message=(payload or {}).get("message") if status in RuntimeStatus.ERROR_STATES or status in RuntimeStatus.INTERRUPTED_STATES else None,
            )
        finally:
            try:
                db.session.remove()
            except Exception:
                pass


def _persist_runtime_cancel_requested(
    app: Any,
    item: ScheduledRuntimeCommand,
    payload: dict[str, Any],
) -> None:
    with app.app_context():
        try:
            row = _control_command_row(item.control_command_id, request_uuid=item.command.command_id)
            if row is None:
                return
            transition_control_command_record(
                row,
                str(row.status or RuntimeStatus.RUNNING),
                event_type="cancel_requested",
                event_payload=payload or None,
                cancel_requested_at=item.cancel_requested_at,
                worker_id=item.worker_id or row.worker_id,
                command_source=item.command.source,
                command_priority=int(item.command.priority),
                correlation_id=item.command.correlation_id,
                queue_timeout_s=item.command.queue_timeout_s,
                execution_timeout_s=item.command.execution_timeout_s,
                total_deadline_at=item.command.total_deadline_at,
            )
        finally:
            try:
                db.session.remove()
            except Exception:
                pass


def _runtime_status_callback(app: Any) -> Callable[[ScheduledRuntimeCommand, str, dict[str, Any]], None]:
    return lambda item, status, payload: _persist_runtime_status(app, item, status, payload)


def _runtime_cancel_callback(app: Any) -> Callable[[ScheduledRuntimeCommand, dict[str, Any]], None]:
    return lambda item, payload: _persist_runtime_cancel_requested(app, item, payload)


def _prepare_cross_thread_handoff(
    *,
    command: ControlCommand | None,
    measurement: Measurement | None,
) -> None:
    if command is not None:
        try:
            db.session.refresh(command)
        except Exception:
            pass
        try:
            events = list(command.events)
        except Exception:
            events = []
        for event in events:
            try:
                db.session.refresh(event)
            except Exception:
                pass
    if measurement is not None:
        try:
            db.session.refresh(measurement)
        except Exception:
            pass
    try:
        db.session.expunge_all()
    except Exception:
        pass


def _execute_with_worker_app(
    app: Any,
    command: DeviceCommand,
    *,
    acquire_lock: bool,
    control_command_id: int | None = None,
    worker_id: str | None = None,
    started_at: datetime | None = None,
    cancellation_token: Any | None = None,
) -> ExecutedDeviceCommand:
    with app.app_context():
        try:
            configured_token = _configure_execution_cancellation_token(
                command,
                started_at=started_at,
                cancellation_token=cancellation_token,
            )
            if configured_token is not None:
                configured_token.throw_if_interrupted(location="command_dispatcher.worker_preflight")

            execution_remaining_s = _command_execution_remaining_s(command, started_at=started_at)
            if execution_remaining_s is not None and execution_remaining_s <= 0:
                raise RuntimeCommandInterruptedError(
                    "Command exceeded its execution timeout before execution started.",
                    command=command,
                    status=RuntimeStatus.TIMEOUT,
                    location="command_dispatcher.worker_preflight",
                )

            total_remaining_s = _command_total_remaining_s(command)
            if total_remaining_s is not None and total_remaining_s <= 0:
                raise RuntimeCommandInterruptedError(
                    "Command exceeded its total deadline before execution started.",
                    command=command,
                    status=RuntimeStatus.EXPIRED,
                    location="command_dispatcher.worker_preflight",
                )

            device = db.session.get(Device, int(command.device_id))
            if device is None:
                raise DeviceCommandError(
                    f"Device {command.device_id} could not be loaded for command execution.",
                    status_code=404,
                )
            command_record = _control_command_row(control_command_id, request_uuid=command.command_id)
            execution = _execute_direct(
                device,
                command,
                acquire_lock=acquire_lock,
                command_record=command_record,
                worker_id=worker_id,
                started_at=started_at,
                cancellation_token=configured_token,
            )
            _prepare_cross_thread_handoff(
                command=execution.command,
                measurement=execution.measurement,
            )
            return execution
        except CommandExecutionInterrupted as exc:
            raise RuntimeCommandInterruptedError(
                str(exc),
                command=command,
                status=exc.status,
                location=exc.location,
            ) from exc
        except DeviceCommandError as exc:
            _prepare_cross_thread_handoff(
                command=getattr(exc, "command", None),
                measurement=None,
            )
            raise
        finally:
            try:
                db.session.remove()
            except Exception:
                pass


def _queued_interrupt_to_device_error(exc: RuntimeCommandInterruptedError) -> DeviceCommandError:
    details = {
        "runtime_status": exc.status,
        "command_id": exc.command.command_id,
        "device_id": exc.command.device_id,
        "command_type": exc.command.command_type,
        "source": exc.command.source,
    }
    location = str(getattr(exc, "location", "") or "").strip()
    if location:
        details["interrupt_location"] = location
    status_code = 504 if exc.status in {RuntimeStatus.TIMEOUT, RuntimeStatus.EXPIRED} else 409
    return DeviceCommandError(
        str(exc),
        status_code=status_code,
        details=details,
    )


def dispatch_device_command(
    device: Any,
    command: DeviceCommand,
    *,
    acquire_lock: bool = True,
    app: Any | None = None,
) -> ExecutedDeviceCommand:
    """Dispatch *command* to *device* and return the execution result."""
    _validate_command_source(command)
    app_obj = _resolve_flask_app(app)
    resolved_command = _resolved_command(command, app=app_obj)

    logger.debug(
        "dispatch command_id=%s device_id=%s type=%s priority=%s source=%s requested_by=%s",
        resolved_command.command_id,
        resolved_command.device_id,
        resolved_command.command_type,
        _priority_label(resolved_command.priority),
        resolved_command.source,
        resolved_command.requested_by,
    )

    if resolved_command.priority <= CommandPriority.SAFETY:
        logger.info(
            "HIGH-PRIORITY command dispatched: command_id=%s device_id=%s "
            "type=%s priority=%s source=%s",
            resolved_command.command_id,
            resolved_command.device_id,
            resolved_command.command_type,
            _priority_label(resolved_command.priority),
            resolved_command.source,
        )

    scheduler = get_runtime_command_scheduler(app_obj, start=True)
    if scheduler is None:
        direct_token = CancellationToken()
        try:
            return _execute_direct(
                device,
                resolved_command,
                acquire_lock=acquire_lock,
                cancellation_token=direct_token,
            )
        except CommandExecutionInterrupted as exc:
            raise DeviceCommandError(
                str(exc),
                status_code=504 if exc.status in {RuntimeStatus.TIMEOUT, RuntimeStatus.EXPIRED} else 409,
                details={
                    **_command_interrupted_details(exc),
                    "command_id": resolved_command.command_id,
                    "device_id": resolved_command.device_id,
                    "command_type": resolved_command.command_type,
                    "source": resolved_command.source,
                },
            ) from exc

    persisted = _persist_pending_command(app_obj, resolved_command)
    work_item = _make_work_item(
        app_obj,
        resolved_command,
        acquire_lock=acquire_lock,
        control_command_id=int(persisted.command_id),
    )
    try:
        return scheduler.submit(work_item, wait=True)
    except RuntimeCommandInterruptedError as exc:
        raise _queued_interrupt_to_device_error(exc) from exc
