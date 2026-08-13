"""Global safety-stop interlock helpers.

This module is intentionally small and dependency-light so the deepest
command-dispatch path can import it without pulling in recipe runtime logic.
"""
from __future__ import annotations

from typing import Any

from ..extensions import db
from ..models import RecipeProgramState
from .command_model import CommandPriority, DeviceCommand
from .runtime_status import ProgramStatus

PROGRAM_STATE_ID = 1
SAFETY_STOP_STATUSES: frozenset[str] = frozenset(
    {
        ProgramStatus.SAFETY_STOP,
        "safty_stop",
        "safety-stop",
        "safty-stop",
    }
)


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def is_safety_stop_state(state: Any | None) -> bool:
    """Return true when the global program state is in a safety-stop window."""
    if state is None:
        return False
    if bool(getattr(state, "stop_requested", False)):
        return True
    return _normalize_status(getattr(state, "status", None)) in SAFETY_STOP_STATUSES


def safety_stop_allows_command(command: DeviceCommand) -> bool:
    """Only emergency/safety-priority commands may run during safety stop."""
    try:
        priority = int(command.priority)
    except (TypeError, ValueError):
        return False
    return priority <= int(CommandPriority.SAFETY)


def current_safety_stop_state() -> dict[str, Any]:
    """Read the singleton program state and return normalized interlock data.

    If the DB/session is not available (for example in isolated unit tests),
    the interlock is treated as inactive. In production the guard is driven by
    the actual singleton program-state row.
    """
    try:
        state = db.session.get(RecipeProgramState, PROGRAM_STATE_ID)
    except Exception:
        return {"active": False, "status": None, "stop_requested": False}

    status = _normalize_status(getattr(state, "status", None))
    stop_requested = bool(getattr(state, "stop_requested", False)) if state is not None else False
    return {
        "active": is_safety_stop_state(state),
        "status": status or None,
        "stop_requested": stop_requested,
    }


def safety_interlock_block_details(command: DeviceCommand) -> dict[str, Any] | None:
    """Return error details when *command* must be blocked by safety-stop."""
    state = current_safety_stop_state()
    if not state.get("active") or safety_stop_allows_command(command):
        return None
    return {
        "runtime_status": ProgramStatus.SAFETY_STOP,
        "program_status": state.get("status"),
        "stop_requested": bool(state.get("stop_requested")),
        "command_id": command.command_id,
        "device_id": command.device_id,
        "command_type": command.command_type,
        "command_priority": int(command.priority),
        "command_source": command.source,
    }


def unsafe_manual_target_blocked(
    *,
    desired_is_on: bool,
    desired_speed: int | float | None,
) -> dict[str, Any] | None:
    """Block manual desired-state updates that would energize an actuator."""
    state = current_safety_stop_state()
    if not state.get("active"):
        return None

    try:
        speed = float(desired_speed or 0)
    except (TypeError, ValueError):
        speed = 0.0
    if not bool(desired_is_on) and speed <= 0:
        return None

    return {
        "runtime_status": ProgramStatus.SAFETY_STOP,
        "program_status": state.get("status"),
        "stop_requested": bool(state.get("stop_requested")),
        "desired_is_on": bool(desired_is_on),
        "desired_speed": speed,
    }
