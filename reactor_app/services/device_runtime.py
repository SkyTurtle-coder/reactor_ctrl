from __future__ import annotations

import socket
import threading
import logging
import os
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
from uuid import uuid4

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import OperationalError, PendingRollbackError

from ..extensions import db
from ..models import ControlCommand, ControlCommandEvent, Device, DeviceManualState, Measurement, MeasurementChannel
from .cancellation import CancellationToken, CommandExecutionInterrupted
from .drivers import DeviceCommandRequest, DeviceCommandResult, DriverError, DriverNotFoundError, DriverValidationError, get_driver
from .runtime_status import RuntimeStatus
from .transports import build_transport, TransportTypeNotSupportedError


_MEASUREMENT_PARSERS = {"text", "float", "int", "bool"}
_MEASUREMENT_SOURCES = {"poller", "event", "manual", "import"}
_DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS = 5.0
# Per-priority device command lock-acquisition timeout.
# POLLING drops in 2 s so it never blocks recipe/manual commands for long.
# RECIPE/MANUAL wait up to 20 s to survive a concurrent polling cycle (≤10 s).
# SAFETY/EMERGENCY wait up to 30 s and must not be blocked by anything.
_DEVICE_COMMAND_LOCK_TIMEOUT_BY_PRIORITY: dict[int, float] = {
    0: 30.0,   # EMERGENCY_STOP
    1: 30.0,   # SAFETY
    3: 20.0,   # RECIPE
    5: 20.0,   # MANUAL
    9: 2.0,    # POLLING — drops quickly; manual reconciler reschedules automatically
}
_DEVICE_COMMAND_LOCKS: dict[int, threading.RLock] = {}
_DEVICE_COMMAND_LOCKS_GUARD = threading.Lock()
_PERSISTENT_TRANSPORTS: dict[tuple[Any, ...], Any] = {}
_PERSISTENT_TRANSPORTS_GUARD = threading.Lock()
_UNSET = object()

# ---------------------------------------------------------------------------
# Transient MySQL/InnoDB error codes
# 1020 = ER_CHECKREAD  "Record has changed since last read in table"
# 1205 = ER_LOCK_WAIT_TIMEOUT  "Lock wait timeout exceeded"
# 1213 = ER_LOCK_DEADLOCK  "Deadlock found when trying to get lock"
# ---------------------------------------------------------------------------
_TRANSIENT_DB_ERROR_CODES: frozenset[int] = frozenset({1020, 1205, 1213})
_DB_RETRY_ATTEMPTS = 3
_DB_RETRY_BACKOFF_S = 0.05
_SESSION_TRANSIENT_DB_ERROR_FLAG = "reactor_ctrl_last_transient_db_error"
_PERSISTENCE_ERROR_KIND = "persistence"
_HUBER_PROTOCOL_NAMES: frozenset[str] = frozenset({"huber_unistat_430", "huber_pilot_one", "huber_cc230"})
_SCALE_PROTOCOL_NAMES: frozenset[str] = frozenset({"mettler_toledo_ics435", "ics435_mtsics"})

_T = TypeVar("_T")


def is_transient_db_error(exc: Exception) -> bool:
    """Return True for MySQL 1020/1205/1213 OperationalError variants."""
    if not isinstance(exc, OperationalError):
        return False
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        return False
    try:
        return int(args[0]) in _TRANSIENT_DB_ERROR_CODES
    except (TypeError, ValueError):
        return False


def is_device_busy_error(exc: Exception) -> bool:
    """Return True if *exc* is a 409 DeviceCommandError for a locked device.

    Distinguishes scheduling contention (retryable) from real 409 conflicts
    such as "has no current binding" or "connection disabled".
    """
    if not isinstance(exc, DeviceCommandError):
        return False
    if getattr(exc, "status_code", None) != 409:
        return False
    msg = str(exc).lower()
    return "busy" in msg or "executing another command" in msg


def _run_db_with_retry(
    operation: Callable[[], _T],
    *,
    attempts: int = _DB_RETRY_ATTEMPTS,
    backoff_s: float = _DB_RETRY_BACKOFF_S,
) -> _T:
    """Execute *operation()*, retrying up to *attempts* times on transient DB errors.

    Always rolls back before retrying.  Raises the last exception if all
    retries are exhausted.
    """
    logger = logging.getLogger(__name__)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            if not is_transient_db_error(exc):
                raise
            # Rollback before retrying so the session is clean.
            rollback_session_safely(db.session)
            if attempt < attempts:
                original = getattr(exc, "orig", None)
                args = getattr(original, "args", ())
                code = int(args[0]) if args else "?"
                logger.warning(
                    "Transient DB error (MySQL %s) on attempt %d/%d — retrying in %.0f ms.",
                    code,
                    attempt,
                    attempts,
                    backoff_s * 1000,
                )
                time.sleep(backoff_s)
    raise last_exc  # type: ignore[misc]


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


def _status_value(value: str | None, default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or default


def _command_interrupted_details(exc: CommandExecutionInterrupted) -> dict[str, Any]:
    details = {"runtime_status": exc.status}
    location = str(getattr(exc, "location", "") or "").strip()
    if location:
        details["interrupt_location"] = location
    reason = str(getattr(exc, "reason", "") or "").strip()
    if reason:
        details["interrupt_reason"] = reason
    return details


def _apply_command_runtime_metadata(
    command: ControlCommand,
    *,
    command_source: str | object = _UNSET,
    command_priority: int | None | object = _UNSET,
    correlation_id: str | None | object = _UNSET,
    worker_id: str | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    queue_timeout_s: float | None | object = _UNSET,
    execution_timeout_s: float | None | object = _UNSET,
    total_deadline_at: datetime | None | object = _UNSET,
    cancel_requested_at: datetime | None | object = _UNSET,
) -> bool:
    changed = False

    if command_source is not _UNSET:
        next_value = str(command_source or "").strip().lower() or None
        if command.command_source != next_value:
            command.command_source = next_value
            changed = True
    if command_priority is not _UNSET:
        next_value = None if command_priority is None else int(command_priority)
        if command.command_priority != next_value:
            command.command_priority = next_value
            changed = True
    if correlation_id is not _UNSET:
        next_value = str(correlation_id).strip() or None if correlation_id is not None else None
        if command.correlation_id != next_value:
            command.correlation_id = next_value
            changed = True
    if worker_id is not _UNSET:
        next_value = str(worker_id).strip() or None if worker_id is not None else None
        if command.worker_id != next_value:
            command.worker_id = next_value
            changed = True
    if started_at is not _UNSET:
        next_value = _as_utc_datetime(started_at)
        if command.started_at != next_value:
            command.started_at = next_value
            changed = True
    if queue_timeout_s is not _UNSET:
        next_value = None if queue_timeout_s is None else float(queue_timeout_s)
        if command.queue_timeout_s != next_value:
            command.queue_timeout_s = next_value
            changed = True
    if execution_timeout_s is not _UNSET:
        next_value = None if execution_timeout_s is None else float(execution_timeout_s)
        if command.execution_timeout_s != next_value:
            command.execution_timeout_s = next_value
            changed = True
    if total_deadline_at is not _UNSET:
        next_value = _as_utc_datetime(total_deadline_at)
        if command.total_deadline_at != next_value:
            command.total_deadline_at = next_value
            changed = True
    if cancel_requested_at is not _UNSET:
        next_value = _as_utc_datetime(cancel_requested_at)
        if command.cancel_requested_at != next_value:
            command.cancel_requested_at = next_value
            changed = True
    return changed


def _parse_optional_int(value: Any, *, field_name: str, min_value: int = 1) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DeviceCommandError(f"Field '{field_name}' must be an integer.", status_code=400) from exc

    if parsed < min_value:
        raise DeviceCommandError(f"Field '{field_name}' must be >= {min_value}.", status_code=400)
    return parsed


def _transport_cache_key(connection: Any, payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        getattr(connection, "connection_id", None),
        str(getattr(connection, "transport_type", None) or "tcp_socket").strip().lower(),
        str(getattr(connection, "tcp_host", "") or "").strip(),
        int(getattr(connection, "tcp_port", 0) or 0),
        str(payload.get("connect_timeout_ms", "")),
        str(payload.get("response_timeout_ms", "")),
        str(payload.get("write_timeout_ms", "")),
        str(payload.get("recv_size", "")),
    )


def _build_command_transport(
    driver: Any,
    connection: Any,
    payload: dict[str, Any],
    *,
    cancellation_token: CancellationToken | None,
) -> tuple[Any, tuple[Any, ...] | None]:
    if not bool(getattr(driver, "persistent_transport", False)):
        return build_transport(connection, payload, cancellation_token=cancellation_token), None

    cache_key = _transport_cache_key(connection, payload)
    with _PERSISTENT_TRANSPORTS_GUARD:
        transport = _PERSISTENT_TRANSPORTS.get(cache_key)
        if transport is None:
            transport = build_transport(connection, payload, cancellation_token=cancellation_token)
            _PERSISTENT_TRANSPORTS[cache_key] = transport
        else:
            bind_runtime_control = getattr(transport, "bind_runtime_control", None)
            if callable(bind_runtime_control):
                bind_runtime_control(cancellation_token=cancellation_token)
    return transport, cache_key


def _forget_persistent_transport(cache_key: tuple[Any, ...] | None, transport: Any | None) -> None:
    if cache_key is None:
        return
    with _PERSISTENT_TRANSPORTS_GUARD:
        cached = _PERSISTENT_TRANSPORTS.get(cache_key)
        if cached is transport:
            _PERSISTENT_TRANSPORTS.pop(cache_key, None)
    if transport is not None:
        try:
            transport.close()
        except Exception:
            logging.getLogger(__name__).debug("Persistent transport close failed.", exc_info=True)


@dataclass
class ExecutedDeviceCommand:
    command: ControlCommand
    result: DeviceCommandResult
    measurement: Measurement | None = None


class DeviceCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        command: ControlCommand | None = None,
        details: Any | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.command = command
        self.details = details


class ControlCommandPersistenceError(DeviceCommandError):
    pass


def _db_error_code(exc: Exception | None) -> int | None:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        args = getattr(exc, "args", ())
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def rollback_session_safely(session: Any | None = None) -> bool:
    target_session = session or db.session
    try:
        target_session.rollback()
        return True
    except Exception:
        logging.getLogger(__name__).warning("Database session rollback failed.", exc_info=True)
        return False


def is_transient_db_error(exc: Exception) -> bool:
    """Return True for transient DB conflicts and poisoned follow-up session state."""
    if isinstance(exc, PendingRollbackError):
        return True
    if isinstance(exc, OperationalError):
        return (_db_error_code(exc) or 0) in _TRANSIENT_DB_ERROR_CODES
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, Exception):
        return is_transient_db_error(cause)
    context = getattr(exc, "__context__", None)
    if isinstance(context, Exception):
        return is_transient_db_error(context)
    return False


def retry_db_operation(
    operation: Callable[[int], _T],
    *,
    attempts: int = _DB_RETRY_ATTEMPTS,
    backoff_s: float = _DB_RETRY_BACKOFF_S,
    label: str,
    command_id: int | None = None,
    extra: dict[str, Any] | None = None,
    session: Any | None = None,
) -> _T:
    logger = logging.getLogger(__name__)
    target_session = session or db.session
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            target_session.info.pop(_SESSION_TRANSIENT_DB_ERROR_FLAG, None)
        except Exception:
            pass
        try:
            return operation(attempt)
        except Exception as exc:
            last_exc = exc
            transient = is_transient_db_error(exc)
            if transient:
                try:
                    target_session.info[_SESSION_TRANSIENT_DB_ERROR_FLAG] = True
                except Exception:
                    pass
            rollback_session_safely(target_session)
            if not transient or attempt >= attempts:
                raise
            logger.warning(
                "Transient DB error during %s; retrying attempt %d/%d pid=%s thread_id=%s command_id=%s context=%s",
                label,
                attempt,
                attempts,
                os.getpid(),
                threading.get_ident(),
                command_id,
                extra or {},
                exc_info=True,
            )
            time.sleep(max(0.0, float(backoff_s)))
    raise last_exc  # type: ignore[misc]


_CONTROL_COMMAND_SYNC_FIELDS: tuple[str, ...] = (
    "command_id",
    "device_id",
    "request_uuid",
    "requested_by",
    "command_name",
    "command_payload",
    "command_source",
    "command_priority",
    "correlation_id",
    "worker_id",
    "status",
    "requested_at",
    "scheduled_for",
    "started_at",
    "sent_at",
    "ack_at",
    "finished_at",
    "queue_timeout_s",
    "execution_timeout_s",
    "total_deadline_at",
    "cancel_requested_at",
    "retry_count",
    "error_message",
)

_CONTROL_COMMAND_TRANSITION_ORDER: dict[str, int] = {
    RuntimeStatus.IDLE: 0,
    RuntimeStatus.PENDING: 10,
    RuntimeStatus.QUEUED: 10,
    RuntimeStatus.RECOVERING: 15,
    RuntimeStatus.STOP_REQUESTED: 18,
    RuntimeStatus.RUNNING: 20,
    RuntimeStatus.SENT: 30,
    RuntimeStatus.ACKED: 40,
    RuntimeStatus.COMPLETED: 100,
    RuntimeStatus.STOPPED: 100,
    RuntimeStatus.FAILED: 100,
    RuntimeStatus.ERROR: 100,
    RuntimeStatus.TIMEOUT: 100,
    RuntimeStatus.CANCELLED: 100,
    RuntimeStatus.SKIPPED: 100,
    RuntimeStatus.PREEMPTED: 100,
    RuntimeStatus.INTERRUPTED: 100,
    RuntimeStatus.EXPIRED: 100,
}


def _command_error_kind(exc: DeviceCommandError) -> str | None:
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    value = str(details.get("error_kind") or "").strip().lower()
    return value or None


def _copy_control_command_state(source: ControlCommand | None, target: ControlCommand | None) -> None:
    if source is None or target is None or source is target:
        return
    for field_name in _CONTROL_COMMAND_SYNC_FIELDS:
        try:
            setattr(target, field_name, getattr(source, field_name))
        except Exception:
            continue


def _persistence_error_details(
    command: ControlCommand | None,
    *,
    phase: str,
    exc: Exception,
    device_success: bool,
) -> dict[str, Any]:
    runtime_status = _status_value(getattr(command, "status", None), RuntimeStatus.FAILED)
    return {
        "error_kind": _PERSISTENCE_ERROR_KIND,
        "phase": str(phase or "").strip().lower() or "unknown",
        "runtime_status": runtime_status,
        "device_success": bool(device_success),
        "device_failed": not bool(device_success),
        "persistence_success": False,
        "persistence_failed": True,
        "persistence_retryable": is_transient_db_error(exc),
        "command_id": getattr(command, "command_id", None),
        "request_uuid": getattr(command, "request_uuid", None),
        "db_error_code": _db_error_code(exc),
    }


def _build_control_command_persistence_error(
    message: str,
    *,
    command: ControlCommand | None,
    phase: str,
    exc: Exception,
    device_success: bool,
) -> ControlCommandPersistenceError:
    return ControlCommandPersistenceError(
        message,
        status_code=500,
        command=command,
        details=_persistence_error_details(
            command,
            phase=phase,
            exc=exc,
            device_success=device_success,
        ),
    )


def _transition_sort_key(status: str | None) -> int:
    return _CONTROL_COMMAND_TRANSITION_ORDER.get(
        _status_value(status, RuntimeStatus.PENDING),
        0,
    )


def _blocked_transition_reason(current_status: str, target_status: str) -> str | None:
    if current_status == target_status:
        return None
    if current_status in RuntimeStatus.TERMINAL:
        return f"terminal status '{current_status}' is immutable"
    if target_status == RuntimeStatus.RECOVERING and current_status in RuntimeStatus.ACTIVE_STATES:
        return None
    if current_status == RuntimeStatus.RECOVERING and (
        target_status in RuntimeStatus.TERMINAL
        or target_status in {RuntimeStatus.PENDING, RuntimeStatus.RUNNING, RuntimeStatus.SENT, RuntimeStatus.ACKED}
    ):
        return None
    if _transition_sort_key(target_status) < _transition_sort_key(current_status):
        return f"out-of-order transition '{current_status}' -> '{target_status}'"
    return None


def describe_device_command_error(exc: DeviceCommandError) -> str:
    command = getattr(exc, "command", None)
    command_name = str(getattr(command, "command_name", "") or "").strip()
    command_id = getattr(command, "command_id", None)
    device_id = getattr(command, "device_id", None)
    detail = str(getattr(command, "error_message", "") or "").strip()
    base_message = str(exc).strip() or "Device command failed."

    parts: list[str] = []
    if command_name:
        parts.append(f"command '{command_name}'")
    if command_id:
        parts.append(f"command_id={command_id}")
    if device_id:
        parts.append(f"device_id={device_id}")

    prefix = "Persistence error" if _command_error_kind(exc) == _PERSISTENCE_ERROR_KIND else "Device command failed"
    if parts:
        prefix = f"{prefix} ({', '.join(parts)})"
    if detail and detail != base_message:
        return f"{prefix}: {detail}"
    return f"{prefix}: {base_message}"


def _db_dialect_name() -> str:
    try:
        bind = db.session.get_bind()
    except Exception:
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


def _local_device_command_lock(device_id: int) -> threading.RLock:
    normalized_device_id = int(device_id)
    with _DEVICE_COMMAND_LOCKS_GUARD:
        lock = _DEVICE_COMMAND_LOCKS.get(normalized_device_id)
        if lock is None:
            lock = threading.RLock()
            _DEVICE_COMMAND_LOCKS[normalized_device_id] = lock
        return lock


@contextmanager
def _device_command_lock(device_id: int, *, timeout_s: float = _DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS):
    normalized_device_id = int(device_id)
    timeout_seconds = max(1, int(round(float(timeout_s))))
    dialect_name = _db_dialect_name()

    if dialect_name in {"mysql", "mariadb"}:
        lock_name = f"reactor_ctrl:device_command:{normalized_device_id}"
        with db.engine.connect() as connection:
            result = connection.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout_s)"),
                {"lock_name": lock_name, "timeout_s": timeout_seconds},
            ).scalar()
            if result != 1:
                raise DeviceCommandError(
                    f"Device {normalized_device_id} is busy executing another command.",
                    status_code=409,
                )
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Failed to release device command lock for device %s.",
                        normalized_device_id,
                        exc_info=True,
                    )
        return

    lock = _local_device_command_lock(normalized_device_id)
    acquired = lock.acquire(timeout=timeout_seconds)
    if not acquired:
        raise DeviceCommandError(
            f"Device {normalized_device_id} is busy executing another command.",
            status_code=409,
        )
    try:
        yield
    finally:
        lock.release()


@contextmanager
def device_command_sequence_lock(device_id: int, *, timeout_s: float = _DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS):
    with _device_command_lock(device_id, timeout_s=timeout_s):
        yield


def _add_command_event(command: ControlCommand, event_type: str, event_payload: dict[str, Any] | None = None) -> None:
    """Flush the ControlCommand and append a ControlCommandEvent.

    The flush may raise a transient DB error (1020/1205/1213) if another
    worker concurrently updated the same command row.  Callers that also call
    ``_commit_command_phase`` should use ``_add_and_commit_command_phase``
    instead, which retries the full add+commit sequence atomically.
    """
    logger = logging.getLogger(__name__)
    try:
        command_state = sa_inspect(command)
        if command_state.detached:
            raise RuntimeError("Cannot add command event: ControlCommand is detached from the active session.")
        if command_state.transient:
            db.session.add(command)

        db.session.flush([command])
        command_id = command.command_id
        if command_id is None:
            raise RuntimeError("Cannot add command event: ControlCommand has no command_id after flush.")

        event = ControlCommandEvent(
            command=command,
            command_id=command_id,
            event_type=event_type,
            event_payload=event_payload,
        )
        db.session.add(event)
        db.session.flush([event])
    except Exception as exc:
        request_uuid = getattr(command, "request_uuid", None)
        log_fn = logger.warning if is_transient_db_error(exc) else logger.exception
        log_fn(
            "Failed to add ControlCommandEvent(command_id=%s, request_uuid=%s): %s",
            getattr(command, "command_id", None),
            request_uuid,
            exc,
        )
        rollback_session_safely(db.session)
        raise


def _commit_command_phase(command: ControlCommand, phase: str) -> None:
    """Commit the current session transaction for *phase*.

    This is intentionally a simple commit with no internal retry because
    retrying just the commit after a rollback would commit an empty
    transaction.  Use ``_add_and_commit_command_phase`` for the full
    event-add + commit pair with retry semantics.
    """
    logger = logging.getLogger(__name__)
    command_id = getattr(command, "command_id", None)
    request_uuid = getattr(command, "request_uuid", None)
    try:
        db.session.commit()
    except Exception as exc:
        # Always ensure session is clean after any failure.
        try:
            db.session.rollback()
        except Exception:
            pass
        if is_transient_db_error(exc):
            logger.warning(
                "Transient DB error persisting command phase %s (command_id=%s, request_uuid=%s) — "
                "treating as persistence failure.",
                phase,
                command_id,
                request_uuid,
            )
        else:
            logger.exception(
                "Failed to commit device command phase %s (command_id=%s, request_uuid=%s).",
                phase,
                command_id,
                request_uuid,
            )
        raise DeviceCommandError(
            f"Device command log persistence failed during {phase}.",
            status_code=500,
            command=command,
        ) from exc


def _add_and_commit_command_phase(
    command: ControlCommand,
    phase: str,
    event_type: str,
    event_payload: dict[str, Any] | None = None,
) -> None:
    """Add a ControlCommandEvent for *phase* and commit, retrying the full
    sequence up to ``_DB_RETRY_ATTEMPTS`` times on transient MySQL errors.

    A retry rolls back the session first so any stale flush state is cleared
    before the next attempt.
    """
    logger = logging.getLogger(__name__)
    command_id = getattr(command, "command_id", None)
    request_uuid = getattr(command, "request_uuid", None)
    last_exc: Exception | None = None
    for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
        try:
            _add_command_event(command, event_type, event_payload)
            _commit_command_phase(command, phase)
            return
        except DeviceCommandError as exc:
            last_exc = exc
            cause = exc.__cause__
            if not isinstance(cause, Exception) or not is_transient_db_error(cause):
                raise
            # Rollback is already done inside _commit_command_phase on error.
            # If the error came from _add_command_event, we need to rollback too.
            rollback_session_safely(db.session)
            if attempt < _DB_RETRY_ATTEMPTS:
                original = getattr(getattr(cause, "orig", None), "args", ())
                code = int(original[0]) if original else "?"
                logger.warning(
                    "Transient DB error (MySQL %s) in phase %s (command_id=%s) "
                    "on attempt %d/%d — retrying in %.0f ms.",
                    code,
                    phase,
                    command_id,
                    attempt,
                    _DB_RETRY_ATTEMPTS,
                    _DB_RETRY_BACKOFF_S * 1000,
                )
                time.sleep(_DB_RETRY_BACKOFF_S)
        except Exception as exc:
            # Non-DeviceCommandError (e.g. OperationalError from _add_command_event flush)
            last_exc = exc
            if not is_transient_db_error(exc):
                try:
                    db.session.rollback()
                except Exception:
                    pass
                logger.exception(
                    "Failed to add/commit command phase %s (command_id=%s, request_uuid=%s).",
                    phase,
                    command_id,
                    request_uuid,
                )
                raise DeviceCommandError(
                    f"Device command log persistence failed during {phase}.",
                    status_code=500,
                    command=command,
                ) from exc
            rollback_session_safely(db.session)
            if attempt < _DB_RETRY_ATTEMPTS:
                original = getattr(getattr(exc, "orig", None), "args", ())
                code = int(original[0]) if original else "?"
                logger.warning(
                    "Transient DB error (MySQL %s) in phase %s (command_id=%s) "
                    "on attempt %d/%d — retrying in %.0f ms.",
                    code,
                    phase,
                    command_id,
                    attempt,
                    _DB_RETRY_ATTEMPTS,
                    _DB_RETRY_BACKOFF_S * 1000,
                )
                time.sleep(_DB_RETRY_BACKOFF_S)

    # All retries exhausted.
    logger.warning(
        "Transient DB error in phase %s (command_id=%s, request_uuid=%s) "
        "after %d retries — giving up.",
        phase,
        command_id,
        request_uuid,
        _DB_RETRY_ATTEMPTS,
    )
    if isinstance(last_exc, DeviceCommandError):
        raise last_exc  # type: ignore[misc]
    raise DeviceCommandError(
        f"Device command log persistence failed during {phase}.",
        status_code=500,
        command=command,
    ) from last_exc


def _commit_command_phase(command: ControlCommand, phase: str, *, device_success: bool = False) -> None:
    logger = logging.getLogger(__name__)
    command_id = getattr(command, "command_id", None)
    request_uuid = getattr(command, "request_uuid", None)
    try:
        db.session.commit()
    except Exception as exc:
        rollback_session_safely(db.session)
        if is_transient_db_error(exc):
            logger.warning(
                "Transient DB error persisting command phase %s (command_id=%s, request_uuid=%s); treating as persistence failure.",
                phase,
                command_id,
                request_uuid,
            )
        else:
            logger.exception(
                "Failed to commit device command phase %s (command_id=%s, request_uuid=%s).",
                phase,
                command_id,
                request_uuid,
            )
        raise _build_control_command_persistence_error(
            f"Persistence error while updating control_command during {phase}.",
            command=command,
            phase=phase,
            exc=exc,
            device_success=device_success,
        ) from exc


def _add_and_commit_command_phase(
    command: ControlCommand,
    phase: str,
    event_type: str,
    event_payload: dict[str, Any] | None = None,
    *,
    device_success: bool = False,
) -> None:
    logger = logging.getLogger(__name__)
    command_id = getattr(command, "command_id", None)
    request_uuid = getattr(command, "request_uuid", None)
    last_exc: Exception | None = None
    for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
        try:
            _add_command_event(command, event_type, event_payload)
            _commit_command_phase(command, phase, device_success=device_success)
            return
        except ControlCommandPersistenceError as exc:
            last_exc = exc
            cause = exc.__cause__
            if not isinstance(cause, Exception) or not is_transient_db_error(cause):
                raise
            rollback_session_safely(db.session)
            if attempt < _DB_RETRY_ATTEMPTS:
                logger.warning(
                    "Transient DB error (MySQL %s) in phase %s (command_id=%s) on attempt %d/%d; retrying in %.0f ms.",
                    _db_error_code(cause) or "?",
                    phase,
                    command_id,
                    attempt,
                    _DB_RETRY_ATTEMPTS,
                    _DB_RETRY_BACKOFF_S * 1000,
                )
                time.sleep(_DB_RETRY_BACKOFF_S)
        except Exception as exc:
            last_exc = exc
            if not is_transient_db_error(exc):
                rollback_session_safely(db.session)
                logger.exception(
                    "Failed to add/commit command phase %s (command_id=%s, request_uuid=%s).",
                    phase,
                    command_id,
                    request_uuid,
                )
                raise _build_control_command_persistence_error(
                    f"Persistence error while updating control_command during {phase}.",
                    command=command,
                    phase=phase,
                    exc=exc,
                    device_success=device_success,
                ) from exc
            rollback_session_safely(db.session)
            if attempt < _DB_RETRY_ATTEMPTS:
                logger.warning(
                    "Transient DB error (MySQL %s) in phase %s (command_id=%s) on attempt %d/%d; retrying in %.0f ms.",
                    _db_error_code(exc) or "?",
                    phase,
                    command_id,
                    attempt,
                    _DB_RETRY_ATTEMPTS,
                    _DB_RETRY_BACKOFF_S * 1000,
                )
                time.sleep(_DB_RETRY_BACKOFF_S)

    logger.warning(
        "Transient DB error in phase %s (command_id=%s, request_uuid=%s) after %d retries; giving up.",
        phase,
        command_id,
        request_uuid,
        _DB_RETRY_ATTEMPTS,
    )
    if isinstance(last_exc, ControlCommandPersistenceError):
        raise last_exc
    raise _build_control_command_persistence_error(
        f"Persistence error while updating control_command during {phase}.",
        command=command,
        phase=phase,
        exc=last_exc or RuntimeError("unknown persistence error"),
        device_success=device_success,
    ) from last_exc


def _apply_control_command_transition(
    command: ControlCommand,
    status: str,
    *,
    error_message: str | None | object = _UNSET,
    scheduled_for: datetime | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    sent_at: datetime | None | object = _UNSET,
    ack_at: datetime | None | object = _UNSET,
    finished_at: datetime | None | object = _UNSET,
    command_source: str | object = _UNSET,
    command_priority: int | None | object = _UNSET,
    correlation_id: str | None | object = _UNSET,
    worker_id: str | None | object = _UNSET,
    queue_timeout_s: float | None | object = _UNSET,
    execution_timeout_s: float | None | object = _UNSET,
    total_deadline_at: datetime | None | object = _UNSET,
    cancel_requested_at: datetime | None | object = _UNSET,
) -> bool:
    next_status = _status_value(status, str(command.status or RuntimeStatus.PENDING))
    changed = False

    if str(command.status or "").strip().lower() != next_status:
        command.status = next_status
        changed = True

    if scheduled_for is not _UNSET:
        next_scheduled_for = _as_utc_datetime(scheduled_for)
        if command.scheduled_for != next_scheduled_for:
            command.scheduled_for = next_scheduled_for
            changed = True
    if sent_at is not _UNSET:
        next_sent_at = _as_utc_datetime(sent_at)
        if command.sent_at != next_sent_at:
            command.sent_at = next_sent_at
            changed = True
    if ack_at is not _UNSET:
        next_ack_at = _as_utc_datetime(ack_at)
        if command.ack_at != next_ack_at:
            command.ack_at = next_ack_at
            changed = True
    if finished_at is not _UNSET:
        next_finished_at = _as_utc_datetime(finished_at)
        if command.finished_at != next_finished_at:
            command.finished_at = next_finished_at
            changed = True
    if error_message is not _UNSET:
        next_error = str(error_message).strip() if error_message not in (None, "") else None
        if command.error_message != next_error:
            command.error_message = next_error
            changed = True

    if _apply_command_runtime_metadata(
        command,
        command_source=command_source,
        command_priority=command_priority,
        correlation_id=correlation_id,
        worker_id=worker_id,
        started_at=started_at,
        queue_timeout_s=queue_timeout_s,
        execution_timeout_s=execution_timeout_s,
        total_deadline_at=total_deadline_at,
        cancel_requested_at=cancel_requested_at,
    ):
        changed = True
    return changed


def safe_update_control_command_status(
    command_id: int,
    status: str,
    *,
    command: ControlCommand | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    error_message: str | None | object = _UNSET,
    scheduled_for: datetime | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    sent_at: datetime | None | object = _UNSET,
    ack_at: datetime | None | object = _UNSET,
    finished_at: datetime | None | object = _UNSET,
    command_source: str | object = _UNSET,
    command_priority: int | None | object = _UNSET,
    correlation_id: str | None | object = _UNSET,
    worker_id: str | None | object = _UNSET,
    queue_timeout_s: float | None | object = _UNSET,
    execution_timeout_s: float | None | object = _UNSET,
    total_deadline_at: datetime | None | object = _UNSET,
    cancel_requested_at: datetime | None | object = _UNSET,
    device_success: bool = False,
) -> ControlCommand:
    logger = logging.getLogger(__name__)
    next_status = _status_value(status, str(getattr(command, "status", RuntimeStatus.PENDING)))
    explicit_event_type = event_type is not None
    next_event_type = str(event_type or next_status).strip().lower() or next_status
    transition_context = {
        "worker_id": None if worker_id is _UNSET else worker_id,
        "command_source": None if command_source is _UNSET else command_source,
        "command_priority": None if command_priority is _UNSET else command_priority,
        "correlation_id": None if correlation_id is _UNSET else correlation_id,
    }

    def _operation(attempt: int) -> ControlCommand:
        session_get = getattr(db.session, "get", None)
        if callable(session_get):
            row = session_get(ControlCommand, int(command_id))
        elif command is not None and getattr(command, "command_id", None) == int(command_id):
            row = command
        else:
            row = None
        if row is None:
            raise RuntimeError(f"ControlCommand {command_id} could not be loaded for transition.")

        current_status = _status_value(getattr(row, "status", None), RuntimeStatus.PENDING)
        blocked_reason = _blocked_transition_reason(current_status, next_status)
        if blocked_reason is not None:
            logger.warning(
                "Ignoring control_command transition pid=%s thread_id=%s command_id=%s request_uuid=%s old_status=%s new_status=%s worker_id=%s source=%s priority=%s correlation_id=%s attempt=%s reason=%s",
                os.getpid(),
                threading.get_ident(),
                command_id,
                getattr(row, "request_uuid", None),
                current_status,
                next_status,
                getattr(row, "worker_id", None),
                getattr(row, "command_source", None),
                getattr(row, "command_priority", None),
                getattr(row, "correlation_id", None),
                attempt,
                blocked_reason,
            )
            _copy_control_command_state(row, command)
            return row

        changed = _apply_control_command_transition(
            row,
            next_status,
            error_message=error_message,
            scheduled_for=scheduled_for,
            started_at=started_at,
            sent_at=sent_at,
            ack_at=ack_at,
            finished_at=finished_at,
            command_source=command_source,
            command_priority=command_priority,
            correlation_id=correlation_id,
            worker_id=worker_id,
            queue_timeout_s=queue_timeout_s,
            execution_timeout_s=execution_timeout_s,
            total_deadline_at=total_deadline_at,
            cancel_requested_at=cancel_requested_at,
        )

        should_add_event = bool(changed or event_payload is not None or next_event_type != next_status or explicit_event_type)
        if should_add_event:
            _add_command_event(row, next_event_type, event_payload)
        try:
            db.session.commit()
        except Exception as exc:
            raise _build_control_command_persistence_error(
                f"Persistence error while updating control_command during {next_event_type}.",
                command=row,
                phase=next_event_type,
                exc=exc,
                device_success=device_success,
            ) from exc
        try:
            db.session.refresh(row)
        except Exception:
            pass
        _copy_control_command_state(row, command)
        logger.debug(
            "Persisted control_command transition pid=%s thread_id=%s command_id=%s request_uuid=%s old_status=%s new_status=%s worker_id=%s source=%s priority=%s correlation_id=%s attempt=%s",
            os.getpid(),
            threading.get_ident(),
            command_id,
            getattr(row, "request_uuid", None),
            current_status,
            next_status,
            getattr(row, "worker_id", None),
            getattr(row, "command_source", None),
            getattr(row, "command_priority", None),
            getattr(row, "correlation_id", None),
            attempt,
        )
        return row

    try:
        return retry_db_operation(
            _operation,
            label=f"control_command transition {next_event_type}",
            command_id=int(command_id),
            extra=transition_context,
            session=db.session,
        )
    except ControlCommandPersistenceError:
        raise
    except Exception as exc:
        raise _build_control_command_persistence_error(
            f"Persistence error while updating control_command during {next_event_type}.",
            command=command,
            phase=next_event_type,
            exc=exc,
            device_success=device_success,
        ) from exc


def create_control_command_record(
    *,
    device_id: int,
    request_uuid: str,
    requested_by: str,
    command_name: str,
    command_payload: dict[str, Any],
    status: str,
    requested_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    command_source: str | None = None,
    command_priority: int | None = None,
    correlation_id: str | None = None,
    worker_id: str | None = None,
    started_at: datetime | None = None,
    queue_timeout_s: float | None = None,
    execution_timeout_s: float | None = None,
    total_deadline_at: datetime | None = None,
    event_payload: dict[str, Any] | None = None,
) -> ControlCommand:
    command = ControlCommand(
        device_id=int(device_id),
        request_uuid=str(request_uuid),
        requested_by=requested_by,
        command_name=command_name,
        command_payload=command_payload,
        status=_status_value(status, RuntimeStatus.PENDING),
        requested_at=_as_utc_datetime(requested_at) or _now_utc(),
        scheduled_for=_as_utc_datetime(scheduled_for),
    )
    _apply_command_runtime_metadata(
        command,
        command_source=command_source,
        command_priority=command_priority,
        correlation_id=correlation_id,
        worker_id=worker_id,
        started_at=started_at,
        queue_timeout_s=queue_timeout_s,
        execution_timeout_s=execution_timeout_s,
        total_deadline_at=total_deadline_at,
    )
    try:
        db.session.add(command)
        db.session.flush([command])
    except Exception as exc:
        rollback_session_safely(db.session)
        raise _build_control_command_persistence_error(
            "Persistence error while creating control_command.",
            command=command,
            phase=command.status,
            exc=exc,
            device_success=False,
        ) from exc
    safe_update_control_command_status(
        int(command.command_id),
        command.status,
        command=command,
        event_type=command.status,
        event_payload=event_payload,
        scheduled_for=command.scheduled_for,
        started_at=command.started_at,
        command_source=command.command_source,
        command_priority=command.command_priority,
        correlation_id=command.correlation_id,
        worker_id=command.worker_id,
        queue_timeout_s=command.queue_timeout_s,
        execution_timeout_s=command.execution_timeout_s,
        total_deadline_at=command.total_deadline_at,
    )
    return command


def transition_control_command_record(
    command: ControlCommand,
    status: str,
    *,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    error_message: str | None | object = _UNSET,
    scheduled_for: datetime | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    sent_at: datetime | None | object = _UNSET,
    ack_at: datetime | None | object = _UNSET,
    finished_at: datetime | None | object = _UNSET,
    command_source: str | object = _UNSET,
    command_priority: int | None | object = _UNSET,
    correlation_id: str | None | object = _UNSET,
    worker_id: str | None | object = _UNSET,
    queue_timeout_s: float | None | object = _UNSET,
    execution_timeout_s: float | None | object = _UNSET,
    total_deadline_at: datetime | None | object = _UNSET,
    cancel_requested_at: datetime | None | object = _UNSET,
    commit: bool = True,
    device_success: bool = False,
) -> ControlCommand:
    next_status = _status_value(status, str(command.status or RuntimeStatus.PENDING))
    explicit_event_type = event_type is not None
    next_event_type = str(event_type or next_status).strip().lower() or next_status

    if commit and getattr(command, "command_id", None) is not None:
        return safe_update_control_command_status(
            int(command.command_id),
            next_status,
            command=command,
            event_type=next_event_type,
            event_payload=event_payload,
            error_message=error_message,
            scheduled_for=scheduled_for,
            started_at=started_at,
            sent_at=sent_at,
            ack_at=ack_at,
            finished_at=finished_at,
            command_source=command_source,
            command_priority=command_priority,
            correlation_id=correlation_id,
            worker_id=worker_id,
            queue_timeout_s=queue_timeout_s,
            execution_timeout_s=execution_timeout_s,
            total_deadline_at=total_deadline_at,
            cancel_requested_at=cancel_requested_at,
            device_success=device_success,
        )

    changed = _apply_control_command_transition(
        command,
        next_status,
        error_message=error_message,
        scheduled_for=scheduled_for,
        started_at=started_at,
        sent_at=sent_at,
        ack_at=ack_at,
        finished_at=finished_at,
        command_source=command_source,
        command_priority=command_priority,
        correlation_id=correlation_id,
        worker_id=worker_id,
        queue_timeout_s=queue_timeout_s,
        execution_timeout_s=execution_timeout_s,
        total_deadline_at=total_deadline_at,
        cancel_requested_at=cancel_requested_at,
    )

    should_add_event = bool(changed or event_payload is not None or next_event_type != next_status or explicit_event_type)
    if should_add_event:
        _add_command_event(command, next_event_type, event_payload)
    return command


def _safe_expire(item: Any, attribute_names: list[str]) -> None:
    try:
        db.session.expire(item, attribute_names)
    except Exception:
        pass


def _mark_connection_success(connection_id: int, *, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_connection "
            "SET last_seen_at=:ts, last_error=NULL, updated_at=:ts "
            "WHERE connection_id=:cid"
        ),
        {"ts": timestamp, "cid": connection_id},
    )


def _mark_connection_failure(connection_id: int, *, message: str, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_connection "
            "SET last_error=:msg, updated_at=:ts "
            "WHERE connection_id=:cid"
        ),
        {"msg": message, "ts": timestamp, "cid": connection_id},
    )


def _mark_binding_online(device_id: int, *, connection_id: int, timestamp: datetime) -> None:
    db.session.execute(
        text(
            "UPDATE device_binding_current "
            "SET last_seen_at=:ts, is_online=1 "
            "WHERE device_id=:did AND connection_id=:cid"
        ),
        {"ts": timestamp, "did": device_id, "cid": connection_id},
    )


def _mark_binding_offline(device_id: int, *, connection_id: int) -> None:
    db.session.execute(
        text(
            "UPDATE device_binding_current "
            "SET is_online=0 "
            "WHERE device_id=:did AND connection_id=:cid"
        ),
        {"did": device_id, "cid": connection_id},
    )


def _sensor_value_from_command(command_name: str, result: DeviceCommandResult) -> str | None:
    normalized = str(command_name or "").strip().lower()
    if normalized in {"select_internal_sensor", "set_internal_sensor"}:
        return "internal"
    if normalized in {"select_external_sensor", "set_external_sensor"}:
        return "external"
    value = result.metadata.get("active_control_sensor") if isinstance(result.metadata, dict) else None
    normalized_value = str(value or "").strip().lower()
    return normalized_value if normalized_value in {"internal", "external"} else None


def _record_active_control_sensor(device_id: int, sensor: str) -> None:
    normalized_sensor = str(sensor or "").strip().lower()
    if normalized_sensor not in {"internal", "external"}:
        return
    try:
        if _db_dialect_name() in {"mysql", "mariadb"}:
            db.session.execute(
                text(
                    "INSERT INTO device_manual_state "
                    "(device_id, desired_version, applied_version, queue_status, active_control_sensor) "
                    "VALUES (:did, 0, 0, 'idle', :sensor) "
                    "ON DUPLICATE KEY UPDATE active_control_sensor=VALUES(active_control_sensor)"
                ),
                {"did": int(device_id), "sensor": normalized_sensor},
            )
        else:
            state = db.session.get(DeviceManualState, int(device_id))
            if state is None:
                state = DeviceManualState(
                    device_id=int(device_id),
                    desired_version=0,
                    applied_version=0,
                    queue_status="idle",
                )
                db.session.add(state)
            state.active_control_sensor = normalized_sensor
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        logging.getLogger(__name__).warning(
            "Failed to store active control sensor '%s' for device %s.",
            normalized_sensor,
            device_id,
            exc_info=True,
        )



def _fail_command(
    command: ControlCommand,
    *,
    status: str,
    message: str,
    connection_id: int,
    binding_device_id: int | None,
    binding_connection_id: int | None,
) -> None:
    finished_at = _now_utc()

    # These runtime telemetry fields can be touched by the recipe and manual
    # reconcilers at the same time. Use a savepoint so a concurrent 1020 error
    # cannot destroy the command record; last writer wins is acceptable for
    # connection health metadata.
    try:
        with db.session.begin_nested():
            with db.session.no_autoflush:
                _mark_connection_failure(connection_id, message=message, timestamp=finished_at)
                if binding_device_id is not None and binding_connection_id is not None:
                    _mark_binding_offline(binding_device_id, connection_id=binding_connection_id)
    except Exception:
        pass

    transition_control_command_record(
        command,
        status,
        event_payload={"message": message, "finished_at": finished_at.isoformat()},
        error_message=message,
        finished_at=finished_at,
    )


def _fail_command_without_connection_health(command: ControlCommand, *, status: str, message: str) -> None:
    finished_at = _now_utc()
    transition_control_command_record(
        command,
        status,
        event_payload={"message": message, "finished_at": finished_at.isoformat()},
        error_message=message,
        finished_at=finished_at,
    )


def _raise_if_interrupted(
    cancellation_token: CancellationToken | None,
    *,
    location: str,
) -> None:
    if cancellation_token is None:
        return
    cancellation_token.throw_if_interrupted(location=location)


def _with_command_outcome_metadata(
    result: DeviceCommandResult,
    *,
    device_success: bool,
    persistence_success: bool,
    persistence_retryable: bool = False,
    runtime_status: str | None = None,
    persistence_error_phase: str | None = None,
) -> DeviceCommandResult:
    metadata = dict(result.metadata) if isinstance(result.metadata, dict) else {}
    metadata["device_success"] = bool(device_success)
    metadata["device_failed"] = not bool(device_success)
    metadata["persistence_success"] = bool(persistence_success)
    metadata["persistence_failed"] = not bool(persistence_success)
    metadata["persistence_retryable"] = bool(persistence_retryable)
    if runtime_status:
        metadata["runtime_status"] = runtime_status
    if persistence_error_phase:
        metadata["persistence_error_phase"] = persistence_error_phase
    return replace(result, metadata=metadata)


def _parse_measurement_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise DriverValidationError("Field 'payload.measurement.measured_at' must be an ISO datetime string.") from exc
    raise DriverValidationError("Field 'payload.measurement.measured_at' must be an ISO datetime string.")


def _parse_measurement_quality_score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DriverValidationError("Field 'payload.measurement.quality_score' must be numeric.") from exc


def _parse_measurement_source(value: Any) -> str:
    source = str(value or "poller").strip().lower()
    if source not in _MEASUREMENT_SOURCES:
        allowed = ", ".join(sorted(_MEASUREMENT_SOURCES))
        raise DriverValidationError(f"Field 'payload.measurement.source' must be one of: {allowed}.")
    return source


def _parse_measurement_parser(value: Any) -> str:
    parser_name = str(value or "text").strip().lower()
    if parser_name not in _MEASUREMENT_PARSERS:
        allowed = ", ".join(sorted(_MEASUREMENT_PARSERS))
        raise DriverValidationError(f"Field 'payload.measurement.parser' must be one of: {allowed}.")
    return parser_name


def _parse_bool_value(raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    truthy = {"true", "1", "yes", "y", "on", "running"}
    falsy = {"false", "0", "no", "n", "off", "stopped"}
    if normalized in truthy:
        return True
    if normalized in falsy:
        return False
    raise DriverValidationError(
        "Field 'payload.measurement.parser=bool' requires a response value like true/false, 1/0, running/stopped."
    )


def _extract_response_value(response_text: str, *, key: str | None) -> str:
    cleaned_response = response_text.strip()
    if not key:
        return cleaned_response

    target_key = key.strip().upper()
    for fragment in cleaned_response.split(";"):
        if "=" not in fragment:
            continue
        current_key, current_value = fragment.split("=", 1)
        if current_key.strip().upper() == target_key:
            return current_value.strip()

    raise DriverValidationError(
        f"Response does not contain key '{key}' required by 'payload.measurement.key'."
    )


def _measurement_value_type(parser_name: str) -> str:
    if parser_name == "text":
        return "text"
    if parser_name == "int":
        return "int"
    if parser_name == "bool":
        return "bool"
    return "float"


def _upsert_measurement_channel(
    *,
    device: Device,
    channel_code: str,
    display_name: str,
    unit: str,
    value_type: str,
) -> MeasurementChannel:
    channel = MeasurementChannel.query.filter_by(device_id=device.device_id, channel_code=channel_code).one_or_none()
    if channel is None:
        channel = MeasurementChannel(
            device_id=device.device_id,
            channel_code=channel_code,
            display_name=display_name,
            unit=unit,
            value_type=value_type,
            is_active=True,
        )
        db.session.add(channel)
        db.session.flush()
    else:
        channel.display_name = display_name
        channel.unit = unit
        channel.value_type = value_type
        channel.is_active = True
    return channel


def _create_measurement_record(
    *,
    device: Device,
    command: ControlCommand,
    channel: MeasurementChannel,
    measured_at: datetime,
    numeric_value: float | None,
    text_value: str | None,
    unit: str | None,
    quality_score: float | None,
    source: str,
    value_type: str,
    raw_payload: dict[str, Any],
) -> Measurement:
    measurement = Measurement(
        device_id=device.device_id,
        channel_id=channel.channel_id,
        channel_code=channel.channel_code,
        measured_at=measured_at,
        numeric_value=numeric_value,
        text_value=text_value,
        unit=unit or None,
        quality_score=quality_score,
        raw_payload=raw_payload,
        source=source,
    )
    db.session.add(measurement)
    db.session.flush()

    _add_command_event(
        command,
        "measurement_saved",
        {
            "measurement_id": measurement.measurement_id,
            "channel_code": measurement.channel_code,
            "value_type": value_type,
            "numeric_value": measurement.numeric_value,
            "text_value": measurement.text_value,
            "unit": measurement.unit,
            "measured_at": measured_at.isoformat(),
        },
    )
    return measurement


def _result_measurement_spec(
    *,
    device: Device,
    command: ControlCommand,
    command_name: str,
    payload: dict[str, Any],
    result: DeviceCommandResult,
) -> dict[str, Any] | None:
    normalized_protocol = str(getattr(device, "protocol", "") or "").strip().lower()
    normalized_command = str(command_name or "").strip().lower()
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if normalized_protocol in _SCALE_PROTOCOL_NAMES and normalized_command in {
        "read_weight",
        "get_weight",
        "weight",
        "read_live_telemetry",
        "read_stable_weight",
        "get_stable_weight",
    }:
        weight = metadata.get("weight")
        if not isinstance(weight, dict):
            return None
        raw_value = weight.get("value_decimal", weight.get("value"))
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            return None
        stable = bool(weight.get("stable"))
        unit = str(weight.get("unit") or "").strip()
        command_source = str(getattr(command, "command_source", "") or "").strip().lower()
        return {
            "channel_code": "weight",
            "display_name": "Weight",
            "unit": unit,
            "value_type": "float",
            "numeric_value": numeric_value,
            "text_value": None,
            "quality_score": 1.0 if stable else 0.5,
            "source": "poller" if command_source == "poller" else "event",
            "raw_payload": {
                "command_id": command.command_id,
                "command_name": command.command_name,
                "command_source": command_source,
                "request_payload": payload,
                "response_text": result.response_text,
                "response_hex": result.response_hex,
                "driver_metadata": metadata,
                "measurement": {
                    "origin": "driver_result",
                    "field": "weight",
                    "stable": stable,
                    "quality_status": weight.get("quality_status"),
                    "raw_response": weight.get("raw_response"),
                    "device_serial": weight.get("device_serial"),
                },
            },
        }
    if normalized_protocol not in _HUBER_PROTOCOL_NAMES:
        return None
    if normalized_command not in {"set_setpoint", "set_temperature", "write_setpoint"}:
        return None

    raw_value = metadata.get("verified_setpoint")
    if raw_value in (None, ""):
        raw_value = metadata.get("value")
    if raw_value in (None, ""):
        raw_value = payload.get("temp_c", payload.get("temperature_c"))
    try:
        numeric_value = float(raw_value)
    except (TypeError, ValueError):
        return None

    measurement_field = "verified_setpoint" if metadata.get("verified_setpoint") not in (None, "") else "value"
    return {
        "channel_code": "setpoint_C",
        "display_name": "Setpoint",
        "unit": "degC",
        "value_type": "float",
        "numeric_value": numeric_value,
        "text_value": None,
        "source": "event",
        "raw_payload": {
            "command_id": command.command_id,
            "command_name": command.command_name,
            "request_payload": payload,
            "response_text": result.response_text,
            "response_hex": result.response_hex,
            "driver_metadata": metadata,
            "measurement": {
                "origin": "driver_result",
                "field": measurement_field,
            },
        },
    }


def _persist_result_measurement(
    *,
    device: Device,
    command: ControlCommand,
    command_name: str,
    payload: dict[str, Any],
    result: DeviceCommandResult,
    finished_at: datetime,
) -> Measurement | None:
    spec = _result_measurement_spec(
        device=device,
        command=command,
        command_name=command_name,
        payload=payload,
        result=result,
    )
    if spec is None:
        return None

    channel = _upsert_measurement_channel(
        device=device,
        channel_code=str(spec["channel_code"]),
        display_name=str(spec["display_name"]),
        unit=str(spec["unit"]),
        value_type=str(spec["value_type"]),
    )
    return _create_measurement_record(
        device=device,
        command=command,
        channel=channel,
        measured_at=finished_at,
        numeric_value=float(spec["numeric_value"]) if spec["numeric_value"] is not None else None,
        text_value=None if spec["text_value"] is None else str(spec["text_value"]),
        unit=str(spec["unit"]),
        quality_score=float(spec["quality_score"]) if spec.get("quality_score") is not None else None,
        source=str(spec["source"]),
        value_type=str(spec["value_type"]),
        raw_payload=dict(spec["raw_payload"]),
    )


def _persist_measurement(
    *,
    device: Device,
    command: ControlCommand,
    payload: dict[str, Any],
    result: DeviceCommandResult,
    finished_at: datetime,
) -> Measurement | None:
    try:
        measurement_config = payload.get("measurement")
        if measurement_config is None:
            return None
        if not isinstance(measurement_config, dict):
            raise DeviceCommandError(
                "Field 'payload.measurement' must be a JSON object.",
                status_code=400,
                command=command,
            )

        channel_code = str(measurement_config.get("channel_code", "")).strip()
        if not channel_code:
            raise DeviceCommandError(
                "Field 'payload.measurement.channel_code' is required.",
                status_code=400,
                command=command,
            )

        parser_name = _parse_measurement_parser(measurement_config.get("parser"))
        source = _parse_measurement_source(measurement_config.get("source"))
        response_text = result.response_text
        if response_text is None:
            raise DeviceCommandError(
                "Measurement persistence requires a text response from the device.",
                status_code=422,
                command=command,
            )

        key = measurement_config.get("key")
        key_text = str(key).strip() if key is not None else None
        raw_value = _extract_response_value(response_text, key=key_text or None)

        numeric_value: float | None = None
        text_value: str | None = None
        if parser_name == "text":
            text_value = raw_value
        elif parser_name == "float":
            try:
                numeric_value = float(raw_value)
            except ValueError as exc:
                raise DeviceCommandError(
                    f"Could not parse measurement value '{raw_value}' as float.",
                    status_code=422,
                    command=command,
                ) from exc
        elif parser_name == "int":
            try:
                numeric_value = float(int(raw_value))
            except ValueError as exc:
                raise DeviceCommandError(
                    f"Could not parse measurement value '{raw_value}' as int.",
                    status_code=422,
                    command=command,
                ) from exc
        else:
            numeric_value = 1.0 if _parse_bool_value(raw_value) else 0.0

        value_type = _measurement_value_type(parser_name)
        display_name = str(measurement_config.get("display_name") or channel_code).strip() or channel_code
        unit = str(measurement_config.get("unit") or "").strip()
        measured_at = _parse_measurement_datetime(measurement_config.get("measured_at")) or finished_at
        quality_score = _parse_measurement_quality_score(measurement_config.get("quality_score"))

        channel = _upsert_measurement_channel(
            device=device,
            channel_code=channel_code,
            display_name=display_name,
            unit=unit,
            value_type=value_type,
        )
        measurement = _create_measurement_record(
            device=device,
            command=command,
            channel=channel,
            measured_at=measured_at,
            numeric_value=numeric_value,
            text_value=text_value,
            unit=unit or None,
            quality_score=quality_score,
            source=source,
            value_type=value_type,
            raw_payload={
                "command_id": command.command_id,
                "command_name": command.command_name,
                "request_payload": payload,
                "response_text": result.response_text,
                "response_hex": result.response_hex,
                "driver_metadata": result.metadata,
                "measurement": {
                    "parser": parser_name,
                    "key": key_text,
                    "raw_value": raw_value,
                },
            },
        )
        return measurement
    except DriverValidationError as exc:
        message = str(exc)
        command.error_message = message
        _add_command_event(command, "measurement_failed", {"message": message})
        raise DeviceCommandError(
            "Measurement persistence failed.",
            status_code=422,
            command=command,
            details={"measurement_error": message},
        ) from exc
    except DeviceCommandError as exc:
        command.error_message = str(exc)
        _add_command_event(command, "measurement_failed", {"message": str(exc)})
        raise


def execute_device_command(
    device: Device,
    *,
    command_name: str,
    payload: dict[str, Any],
    requested_by: str,
    acquire_lock: bool = True,
    command_record: ControlCommand | None = None,
    request_uuid: str | None = None,
    command_source: str | None = None,
    command_priority: int | None = None,
    correlation_id: str | None = None,
    worker_id: str | None = None,
    requested_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    started_at: datetime | None = None,
    queue_timeout_s: float | None = None,
    execution_timeout_s: float | None = None,
    total_deadline_at: datetime | None = None,
    cancellation_token: CancellationToken | None = None,
) -> ExecutedDeviceCommand:
    binding = device.current_binding
    if binding is None:
        raise DeviceCommandError(f"Device {device.device_id} has no current binding.", status_code=409)

    connection = binding.connection
    if connection is None:
        raise DeviceCommandError(f"Device {device.device_id} is bound to an invalid connection.", status_code=409)
    if not connection.is_enabled:
        raise DeviceCommandError(f"Connection {connection.connection_id} is disabled.", status_code=409)

    try:
        driver = get_driver(device.protocol)
    except DriverNotFoundError as exc:
        raise DeviceCommandError(str(exc), status_code=400) from exc

    if not driver.uses_transport and str(connection.transport_type or "tcp_socket").strip().lower() not in {"tcp_socket"}:
        raise DeviceCommandError(
            f"Transport type '{connection.transport_type}' is not supported for command execution.",
            status_code=400,
        )
    connection_id = int(connection.connection_id)
    binding_device_id = int(binding.device_id) if binding.device_id is not None else None
    binding_connection_id = int(binding.connection_id) if binding.connection_id is not None else None

    command = command_record
    normalized_request_uuid = str(request_uuid or uuid4())
    normalized_requested_at = _as_utc_datetime(requested_at)
    normalized_scheduled_for = _as_utc_datetime(scheduled_for)
    normalized_started_at = _as_utc_datetime(started_at) or _now_utc()
    normalized_total_deadline_at = _as_utc_datetime(total_deadline_at)
    runtime_event_payload = {
        "requested_by": requested_by,
        "command_source": str(command_source or "").strip().lower() or None,
        "command_priority": None if command_priority is None else int(command_priority),
        "correlation_id": str(correlation_id).strip() or None if correlation_id is not None else None,
        "queue_timeout_s": None if queue_timeout_s is None else float(queue_timeout_s),
        "execution_timeout_s": None if execution_timeout_s is None else float(execution_timeout_s),
        "total_deadline_at": _datetime_isoformat(normalized_total_deadline_at),
    }
    if command is None:
        command = create_control_command_record(
            device_id=device.device_id,
            request_uuid=normalized_request_uuid,
            requested_by=requested_by,
            command_name=command_name,
            command_payload=payload,
            status=RuntimeStatus.RUNNING,
            requested_at=normalized_requested_at or normalized_started_at,
            scheduled_for=normalized_scheduled_for,
            command_source=command_source,
            command_priority=command_priority,
            correlation_id=correlation_id,
            worker_id=worker_id,
            started_at=normalized_started_at,
            queue_timeout_s=queue_timeout_s,
            execution_timeout_s=execution_timeout_s,
            total_deadline_at=normalized_total_deadline_at,
            event_payload=runtime_event_payload,
        )
    elif str(command.status or "").strip().lower() not in {RuntimeStatus.RUNNING, RuntimeStatus.SENT}:
        transition_control_command_record(
            command,
            RuntimeStatus.RUNNING,
            event_payload=runtime_event_payload,
            started_at=normalized_started_at,
            worker_id=worker_id,
            command_source=command_source,
            command_priority=command_priority,
            correlation_id=correlation_id,
            queue_timeout_s=queue_timeout_s,
            execution_timeout_s=execution_timeout_s,
            total_deadline_at=normalized_total_deadline_at,
        )

    # For CC230 set_setpoint: inject the remembered write variant so the driver
    # tries the most-recently successful mode first instead of always starting from A.
    effective_payload = payload
    if (
        str(command_name or "").strip().lower() in {"set_setpoint", "set_temperature", "write_setpoint"}
        and str(getattr(device, "protocol", "") or "").strip().lower() == "huber_cc230"
        and "cc230_write_mode" not in payload
    ):
        stored_mode = getattr(connection, "cc230_setpoint_write_mode", None)
        if stored_mode is not None:
            effective_payload = {**payload, "cc230_write_mode": int(stored_mode)}

    transport_obj = None
    persistent_transport_key: tuple[Any, ...] | None = None
    if driver.uses_transport:
        try:
            transport_obj, persistent_transport_key = _build_command_transport(
                driver,
                connection,
                effective_payload,
                cancellation_token=cancellation_token,
            )
        except TransportTypeNotSupportedError as exc:
            raise DeviceCommandError(str(exc), status_code=400) from exc

    request = DeviceCommandRequest(
        command_name=command_name,
        payload=effective_payload,
        cancellation_token=cancellation_token,
    )
    sent_at = _now_utc()

    try:
        _raise_if_interrupted(cancellation_token, location="device_runtime.pre_lock")
        priority_lock_timeout = _DEVICE_COMMAND_LOCK_TIMEOUT_BY_PRIORITY.get(
            int(command_priority) if command_priority is not None else -1,
            _DEVICE_COMMAND_LOCK_TIMEOUT_SECONDS,
        )
        lock_context = _device_command_lock(device.device_id, timeout_s=priority_lock_timeout) if acquire_lock else nullcontext()
        with lock_context:
            _raise_if_interrupted(cancellation_token, location="device_runtime.pre_send")
            if driver.uses_transport:
                assert transport_obj is not None
                if persistent_transport_key is not None:
                    transport_obj.connect()
                    _raise_if_interrupted(cancellation_token, location="device_runtime.pre_driver_execute")
                    transition_control_command_record(
                        command,
                        RuntimeStatus.SENT,
                        sent_at=sent_at,
                        error_message=None,
                        event_payload={"sent_at": sent_at.isoformat()},
                    )
                    result = driver.execute(transport=transport_obj, request=request)
                else:
                    with transport_obj:
                        _raise_if_interrupted(cancellation_token, location="device_runtime.pre_driver_execute")
                        transition_control_command_record(
                            command,
                            RuntimeStatus.SENT,
                            sent_at=sent_at,
                            error_message=None,
                            event_payload={"sent_at": sent_at.isoformat()},
                        )
                        result = driver.execute(transport=transport_obj, request=request)
            else:
                _raise_if_interrupted(cancellation_token, location="device_runtime.pre_driver_execute")
                transition_control_command_record(
                    command,
                    RuntimeStatus.SENT,
                    sent_at=sent_at,
                    error_message=None,
                    event_payload={"sent_at": sent_at.isoformat()},
                )
                result = driver.execute(transport=None, request=request)
            _raise_if_interrupted(cancellation_token, location="device_runtime.post_driver_execute")
    except ControlCommandPersistenceError:
        rollback_session_safely(db.session)
        raise
    except CommandExecutionInterrupted as exc:
        _forget_persistent_transport(persistent_transport_key, transport_obj)
        _fail_command_without_connection_health(command, status=exc.status, message=str(exc))
        raise
    except DeviceCommandError as exc:
        _fail_command_without_connection_health(command, status=RuntimeStatus.FAILED, message=str(exc))
        details = dict(exc.details) if isinstance(exc.details, dict) else {}
        details.setdefault("runtime_status", command.status)
        raise DeviceCommandError(str(exc), status_code=exc.status_code, command=command, details=details) from exc
    except socket.timeout as exc:
        _forget_persistent_transport(persistent_transport_key, transport_obj)
        _fail_command(
            command,
            status=RuntimeStatus.TIMEOUT,
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError(
            "Timed out while waiting for a device response.",
            status_code=504,
            command=command,
            details={"runtime_status": RuntimeStatus.TIMEOUT},
        ) from exc
    except DriverValidationError as exc:
        _fail_command(
            command,
            status=RuntimeStatus.FAILED,
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError(str(exc), status_code=400, command=command) from exc
    except OSError as exc:
        _forget_persistent_transport(persistent_transport_key, transport_obj)
        _fail_command(
            command,
            status=RuntimeStatus.FAILED,
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError("Device command execution failed.", status_code=502, command=command) from exc
    except DriverError as exc:
        _fail_command(
            command,
            status=RuntimeStatus.FAILED,
            message=str(exc),
            connection_id=connection_id,
            binding_device_id=binding_device_id,
            binding_connection_id=binding_connection_id,
        )
        raise DeviceCommandError("Device command execution failed.", status_code=502, command=command) from exc

    finished_at = _now_utc()
    logger = logging.getLogger(__name__)

    # See _fail_command for why these telemetry fields are written outside ORM
    # dirty tracking. The binding update is guarded by connection_id so a stale
    # in-flight command cannot mark a newly rebound device online.
    try:
        with db.session.begin_nested():
            with db.session.no_autoflush:
                _mark_connection_success(connection_id, timestamp=finished_at)
                if binding_device_id is not None and binding_connection_id is not None:
                    _mark_binding_online(binding_device_id, connection_id=binding_connection_id, timestamp=finished_at)
    except Exception:
        pass
    _safe_expire(connection, ["last_seen_at", "last_error", "updated_at"])
    _safe_expire(binding, ["last_seen_at", "is_online"])

    # Post-execution persistence: device comms already succeeded.
    # Transient DB conflicts (MySQL 1020/1205/1213) must NOT raise DeviceCommandError
    # because the command actually completed on the device.  _add_and_commit_command_phase
    # retries up to _DB_RETRY_ATTEMPTS times internally; if all retries are exhausted
    # and the error is still transient, log a WARNING and continue.
    try:
        _add_and_commit_command_phase(
            command,
            "response",
            "response",
            {
                "finished_at": finished_at.isoformat(),
                "response_text": result.response_text,
                "response_hex": result.response_hex,
                "metadata": result.metadata,
            },
            device_success=True,
        )
    except ControlCommandPersistenceError as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception) and is_transient_db_error(cause):
            logger.warning(
                "Post-execution DB persistence failed for response phase "
                "(command_id=%s) after %d retries — device comms succeeded, continuing.",
                getattr(command, "command_id", None),
                _DB_RETRY_ATTEMPTS,
                exc_info=True,
            )
            try:
                db.session.rollback()
            except Exception:
                pass
        else:
            raise

    active_control_sensor = _sensor_value_from_command(command_name, result)
    if active_control_sensor is not None:
        _record_active_control_sensor(device.device_id, active_control_sensor)

    measurement: Measurement | None = None
    try:
        _raise_if_interrupted(cancellation_token, location="device_runtime.pre_measurement")
        persisted_measurement = _persist_result_measurement(
            device=device,
            command=command,
            command_name=command_name,
            payload=payload,
            result=result,
            finished_at=finished_at,
        )
        if persisted_measurement is not None:
            measurement = persisted_measurement
        configured_measurement = _persist_measurement(
            device=device,
            command=command,
            payload=payload,
            result=result,
            finished_at=finished_at,
        )
        if configured_measurement is not None:
            measurement = configured_measurement if measurement is None else measurement
        if measurement is not None:
            # _persist_measurement already flushed; commit via add-and-commit so
            # the "measurement_saved" event and commit are retried atomically.
            _commit_command_phase(command, "measurement", device_success=True)
        _raise_if_interrupted(cancellation_token, location="device_runtime.post_measurement")
    except ControlCommandPersistenceError as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception) and is_transient_db_error(cause):
            logger.warning(
                "Post-execution DB persistence failed for measurement phase "
                "(command_id=%s); device comms succeeded, continuing without measurement.",
                getattr(command, "command_id", None),
                exc_info=True,
            )
            rollback_session_safely(db.session)
            measurement = None
            result = _with_command_outcome_metadata(
                result,
                device_success=True,
                persistence_success=False,
                persistence_retryable=True,
                runtime_status=str(getattr(command, "status", "") or RuntimeStatus.SENT),
                persistence_error_phase="measurement",
            )
        else:
            raise
    except CommandExecutionInterrupted as exc:
        _fail_command_without_connection_health(command, status=exc.status, message=str(exc))
        raise
    except DeviceCommandError as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception) and is_transient_db_error(cause):
            # Measurement persistence hit a transient DB error; device comms succeeded.
            logger.warning(
                "Post-execution DB persistence failed for measurement phase "
                "(command_id=%s) — device comms succeeded, continuing without measurement.",
                getattr(command, "command_id", None),
                exc_info=True,
            )
            try:
                db.session.rollback()
            except Exception:
                pass
            measurement = None
        else:
            _fail_command_without_connection_health(command, status=RuntimeStatus.FAILED, message=str(exc))
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details.setdefault("runtime_status", command.status)
            raise DeviceCommandError(str(exc), status_code=exc.status_code, command=command, details=details) from exc
    except Exception as exc:
        if is_transient_db_error(exc):
            logger.warning(
                "Post-execution DB persistence failed for measurement phase "
                "(command_id=%s) during measurement flush/upsert; device comms succeeded, continuing.",
                getattr(command, "command_id", None),
                exc_info=True,
            )
            rollback_session_safely(db.session)
            measurement = None
            result = _with_command_outcome_metadata(
                result,
                device_success=True,
                persistence_success=False,
                persistence_retryable=True,
                runtime_status=str(getattr(command, "status", "") or RuntimeStatus.SENT),
                persistence_error_phase="measurement",
            )
        else:
            raise

    # For CC230 set_setpoint: persist the write mode that worked so the next call
    # can try it first.  Non-fatal: a failure here must not break the command response.
    if (
        str(command_name or "").strip().lower() in {"set_setpoint", "set_temperature", "write_setpoint"}
        and str(getattr(device, "protocol", "") or "").strip().lower() == "huber_cc230"
    ):
        write_mode_used = result.metadata.get("write_mode_used")
        if write_mode_used is not None:
            stored_mode = getattr(connection, "cc230_setpoint_write_mode", None)
            if stored_mode != int(write_mode_used):
                try:
                    db.session.execute(
                        text(
                            "UPDATE device_connection "
                            "SET cc230_setpoint_write_mode=:mode "
                            "WHERE connection_id=:cid"
                        ),
                        {"mode": int(write_mode_used), "cid": connection_id},
                    )
                    db.session.commit()
                    _safe_expire(connection, ["cc230_setpoint_write_mode"])
                except Exception:
                    logger.warning(
                        "CC230: failed to persist setpoint write mode for connection %s.",
                        connection_id,
                        exc_info=True,
                    )

    try:
        transition_control_command_record(
            command,
            RuntimeStatus.COMPLETED,
            ack_at=finished_at if result.acknowledged else None,
            finished_at=finished_at,
            error_message=None,
            event_payload={
                "finished_at": finished_at.isoformat(),
                "acknowledged": bool(result.acknowledged),
            },
            device_success=True,
        )
    except ControlCommandPersistenceError as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception) and is_transient_db_error(cause):
            # Transient DB error persisting COMPLETED status — device comms already
            # succeeded.  Log a warning but do NOT propagate as a device error.
            logger.warning(
                "Post-execution DB persistence failed for completed phase "
                "(command_id=%s) after %d retries — "
                "device comms succeeded, returning result without raising.",
                getattr(command, "command_id", None),
                _DB_RETRY_ATTEMPTS,
                exc_info=True,
            )
            rollback_session_safely(db.session)
            result = _with_command_outcome_metadata(
                result,
                device_success=True,
                persistence_success=False,
                persistence_retryable=True,
                runtime_status=str(getattr(command, "status", "") or RuntimeStatus.SENT),
                persistence_error_phase="completed",
            )
        else:
            raise

    result = _with_command_outcome_metadata(
        result,
        device_success=True,
        persistence_success=bool(result.metadata.get("persistence_success", True)) if isinstance(result.metadata, dict) else True,
        persistence_retryable=bool(result.metadata.get("persistence_retryable", False)) if isinstance(result.metadata, dict) else False,
        runtime_status=str(getattr(command, "status", "") or RuntimeStatus.COMPLETED),
        persistence_error_phase=(
            str(result.metadata.get("persistence_error_phase")).strip()
            if isinstance(result.metadata, dict) and result.metadata.get("persistence_error_phase")
            else None
        ),
    )
    return ExecutedDeviceCommand(command=command, result=result, measurement=measurement)
