"""Central command dispatcher.

All device commands should flow through ``dispatch_device_command()`` instead
of calling ``execute_device_command()`` directly. The dispatcher now inserts
commands into an in-process priority queue that is serviced by runtime worker
threads when a production Flask/SQLAlchemy app is available.

Fallback behaviour remains synchronous for unit tests and in-memory SQLite
setups so Phase 3 call sites keep working without a full worker bootstrap.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import has_app_context

from ..extensions import db
from ..models import ControlCommand, Device, Measurement
from .command_model import CommandPriority, DeviceCommand
from .device_runtime import DeviceCommandError, ExecutedDeviceCommand, execute_device_command
from .runtime_scheduler import (
    RuntimeCommandInterruptedError,
    RuntimeCommandScheduler,
    ScheduledRuntimeCommand,
)
from .runtime_status import RuntimeStatus

logger = logging.getLogger(__name__)

_RUNTIME_SCHEDULER_EXTENSION_KEY = "runtime_command_scheduler"
_DEFAULT_RUNTIME_WORKER_COUNT = 2

_KNOWN_SOURCES = frozenset(
    {
        "api",
        "recipe",
        "manual_reconciler",
        "poller",
        "system",
    }
)


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


def _scheduler_supported(app: Any | None) -> bool:
    if app is None:
        return False
    if not bool(app.config.get("RUNTIME_COMMAND_SCHEDULER_ENABLED", True)):
        return False
    if app.config.get("SQLALCHEMY_DATABASE_URI") == "sqlite:///:memory:":
        return False
    return app.extensions.get("sqlalchemy") is not None


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
    )
    scheduler.start()
    app_obj.extensions[_RUNTIME_SCHEDULER_EXTENSION_KEY] = scheduler
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
    return scheduler.cancel_pending(
        device_id=device_id,
        source=source,
        priority_gt=priority_gt,
        status=status,
        reason=reason,
    )


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


def _effective_payload(command: DeviceCommand) -> dict[str, Any]:
    effective_payload: dict[str, Any] = dict(command.payload)
    if command.timeout_s is not None:
        timeout_ms = int(command.timeout_s * 1000)
        effective_payload.setdefault("response_timeout_ms", timeout_ms)
        effective_payload.setdefault("connect_timeout_ms", timeout_ms)
        effective_payload.setdefault("write_timeout_ms", timeout_ms)
    return effective_payload


def _execute_direct(
    device: Any,
    command: DeviceCommand,
    *,
    acquire_lock: bool,
) -> ExecutedDeviceCommand:
    return execute_device_command(
        device,
        command_name=command.command_type,
        payload=_effective_payload(command),
        requested_by=command.requested_by,
        acquire_lock=acquire_lock,
    )


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
) -> ExecutedDeviceCommand:
    with app.app_context():
        try:
            device = db.session.get(Device, int(command.device_id))
            if device is None:
                raise DeviceCommandError(
                    f"Device {command.device_id} could not be loaded for command execution.",
                    status_code=404,
                )
            execution = _execute_direct(device, command, acquire_lock=acquire_lock)
            _prepare_cross_thread_handoff(
                command=execution.command,
                measurement=execution.measurement,
            )
            return execution
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
    return DeviceCommandError(
        str(exc),
        status_code=409,
        details={
            "runtime_status": exc.status,
            "command_id": exc.command.command_id,
            "device_id": exc.command.device_id,
            "command_type": exc.command.command_type,
            "source": exc.command.source,
        },
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

    logger.debug(
        "dispatch command_id=%s device_id=%s type=%s priority=%s source=%s requested_by=%s",
        command.command_id,
        command.device_id,
        command.command_type,
        _priority_label(command.priority),
        command.source,
        command.requested_by,
    )

    if command.priority <= CommandPriority.SAFETY:
        logger.info(
            "HIGH-PRIORITY command dispatched: command_id=%s device_id=%s "
            "type=%s priority=%s source=%s",
            command.command_id,
            command.device_id,
            command.command_type,
            _priority_label(command.priority),
            command.source,
        )

    app_obj = _resolve_flask_app(app)
    scheduler = get_runtime_command_scheduler(app_obj, start=True)
    if scheduler is None:
        return _execute_direct(device, command, acquire_lock=acquire_lock)

    work_item = ScheduledRuntimeCommand(
        command=command,
        acquire_lock=acquire_lock,
        execute=lambda: _execute_with_worker_app(
            app_obj,
            command,
            acquire_lock=acquire_lock,
        ),
    )
    try:
        return scheduler.submit(work_item, wait=True)
    except RuntimeCommandInterruptedError as exc:
        raise _queued_interrupt_to_device_error(exc) from exc

