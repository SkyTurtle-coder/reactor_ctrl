"""Canonical runtime status constants.

All subsystems (manual reconciler, recipe reconciler, device command runtime)
use the same underlying status strings.  Centralising them here removes magic
string literals from multiple files and makes future refactoring safe.

Usage::

    from reactor_app.services.runtime_status import ProgramStatus, RuntimeStatus

    if state.status in ProgramStatus.TERMINAL:
        ...

    command.status = RuntimeStatus.ACKED

Design notes
------------
- String values are intentionally lowercase to match the database columns.
- ``TERMINAL`` and similar frozensets allow ``in`` membership tests without
  hard-coding set literals at every call site.
- No IntEnum is used here because the statuses are stored as VARCHAR in the
  database; converting between int and str would add unnecessary complexity.
"""
from __future__ import annotations


class RuntimeStatus:
    """Status strings shared across ControlCommand, DeviceManualState, and
    background reconcilers.

    Lifecycle overview::

        QUEUED → SENT → ACKED           (happy path)
                      ↘ FAILED / TIMEOUT (error paths)
        IDLE ↔ QUEUED → RUNNING → IDLE  (manual reconciler cycle)
    """

    # Pre-execution
    IDLE = "idle"
    PENDING = "pending"
    QUEUED = "queued"

    # In-flight
    RUNNING = "running"
    SENT = "sent"

    # Completed successfully
    ACKED = "acked"
    COMPLETED = "completed"

    # Stop-requested (transient, set before sending safe-state commands)
    STOP_REQUESTED = "stop_requested"

    # Terminal
    STOPPED = "stopped"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    PREEMPTED = "preempted"
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"
    RECOVERING = "recovering"

    # Convenience sets
    TERMINAL: frozenset[str] = frozenset(
        {
            "completed",
            "stopped",
            "failed",
            "error",
            "timeout",
            "cancelled",
            "skipped",
            "preempted",
            "interrupted",
            "expired",
        }
    )
    ERROR_STATES: frozenset[str] = frozenset({"failed", "error", "timeout", "interrupted", "expired"})
    ACTIVE_STATES: frozenset[str] = frozenset({"pending", "queued", "running", "sent", "recovering"})
    INTERRUPTED_STATES: frozenset[str] = frozenset({"cancelled", "skipped", "preempted"})
    RECOVERY_STATES: frozenset[str] = frozenset({"recovering"})


class ProgramStatus:
    """Status values specific to ``RecipeProgramState``.

    The recipe runtime uses a subset of ``RuntimeStatus`` values; this class
    makes the applicable subset explicit and avoids accidental use of
    command-level statuses (e.g. ``SENT``, ``ACKED``) on program-level objects.

    Lifecycle::

        IDLE → RUNNING → COMPLETED   (normal completion)
                       → STOPPED     (user-requested stop)
                       → ERROR       (device or runtime failure)
    """

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"

    TERMINAL: frozenset[str] = frozenset({"completed", "stopped", "error"})
    ACTIVE: frozenset[str] = frozenset({"running"})
