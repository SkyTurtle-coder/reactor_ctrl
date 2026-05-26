"""Device command model.

A ``DeviceCommand`` is a first-class value that carries everything needed to
dispatch a single device operation.  It decouples the *intent* of a command
from its *execution*, enabling:

- structured logging and audit trails
- priority-based scheduling (future)
- queue insertion without changing call sites (future)
- correlation tracking across manual/recipe/API sources

Usage::

    from reactor_app.services.command_model import (
        CommandPriority, CommandSource, DeviceCommand
    )

    cmd = DeviceCommand(
        device_id=device.device_id,
        command_type="set_setpoint",
        payload={"temp_c": 25.0},
        priority=CommandPriority.RECIPE,
        source=CommandSource.RECIPE,
        requested_by="recipe_reconciler",
    )
    result = dispatch_device_command(device, cmd)
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any
from uuid import uuid4


class CommandPriority(IntEnum):
    """Numeric priority for device commands.

    Lower value = higher urgency.  Emergency stop must never be blocked behind
    polling or manual operations.

    These values are intentionally spaced so future levels can be inserted
    without renumbering existing constants.
    """
    EMERGENCY_STOP = 0
    SAFETY = 1
    RECIPE = 3
    MANUAL = 5
    POLLING = 9


class CommandSource:
    """String constants that identify where a command originated.

    Used for audit logging and future queue routing.
    """
    API = "api"
    RECIPE = "recipe"
    MANUAL_RECONCILER = "manual_reconciler"
    POLLER = "poller"
    SYSTEM = "system"


@dataclass
class DeviceCommand:
    """Immutable description of a single device operation to be executed.

    ``command_id`` and ``created_at`` are auto-populated.  Callers only need
    to supply the device-specific fields.

    Attributes:
        device_id:      Target device identifier (matches ``Device.device_id``).
        command_type:   Driver-level command name (e.g. ``"set_setpoint"``).
        payload:        Command-specific parameters passed to the driver.
        priority:       Dispatch priority (see ``CommandPriority``).
        source:         Originating subsystem (see ``CommandSource``).
        requested_by:   Human-readable actor identifier for audit logging.
        timeout_s:      Optional override for the transport receive timeout.
                        ``None`` means use the connection default.
        correlation_id: Optional caller-supplied ID for tracing related
                        commands across devices or subsystems.
        command_id:     Auto-generated UUID string; uniquely identifies this
                        command instance for logging and deduplication.
        created_at:     UTC timestamp when this command object was created.
    """
    device_id: int
    command_type: str
    payload: dict[str, Any]
    priority: int = CommandPriority.MANUAL
    source: str = CommandSource.API
    requested_by: str = "unknown"
    timeout_s: float | None = None
    correlation_id: str | None = None
    command_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    def __post_init__(self) -> None:
        # Deep-copy the payload so mutations to the caller's dict don't affect
        # the command after construction.
        self.payload = deepcopy(self.payload)
