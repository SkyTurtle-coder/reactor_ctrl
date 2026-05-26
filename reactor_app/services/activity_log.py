from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import joinedload

from ..models import (
    ControlCommand,
    ControlCommandEvent,
    DeviceBindingCurrent,
    DeviceConnection,
    DeviceManualState,
    RecipeProgramEvent,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def activity_log_cutoff(days: int) -> datetime:
    return _now_utc() - timedelta(days=max(1, int(days)))


@dataclass(frozen=True)
class ActivityLogItem:
    event_key: str
    timestamp: datetime
    severity: str
    category: str
    title: str
    message: str
    actor: str = ""
    source: str = ""
    status: str = ""
    details: str = ""


def severity_badge_class(severity: str | None) -> str:
    normalized = str(severity or "").strip().lower()
    if normalized in {"success", "ok"}:
        return "badge-success"
    if normalized in {"warning", "warn"}:
        return "badge-warning"
    if normalized in {"error", "critical", "danger"}:
        return "badge-danger"
    if normalized == "info":
        return "badge-info"
    return "badge-muted"


def activity_log_item_to_dict(item: ActivityLogItem) -> dict[str, Any]:
    return {
        "event_key": item.event_key,
        "timestamp": item.timestamp.isoformat() if item.timestamp else None,
        "severity": item.severity,
        "severity_badge_class": severity_badge_class(item.severity),
        "category": item.category,
        "title": item.title,
        "message": item.message,
        "actor": item.actor,
        "source": item.source,
        "status": item.status,
        "details": item.details,
    }


def _payload_text(payload: Any, key: str, default: str = "") -> str:
    if not isinstance(payload, dict):
        return default
    value = payload.get(key)
    if value in (None, ""):
        return default
    return str(value)


def _command_text(command: ControlCommand | None) -> str:
    if command is None:
        return "unknown command"

    payload = command.command_payload if isinstance(command.command_payload, dict) else {}
    text = str(payload.get("text") or payload.get("command_text") or "").strip()
    if text:
        return text
    return str(command.command_name or "command")


def _command_actor(command: ControlCommand | None) -> str:
    if command is None:
        return ""
    device = command.device
    if device is not None and getattr(device, "display_name", None):
        return str(device.display_name)
    return f"Device {command.device_id}"


def _command_event_severity(event_type: str, command: ControlCommand | None) -> str:
    normalized_event = str(event_type or "").strip().lower()
    normalized_status = str(getattr(command, "status", "") or "").strip().lower()
    if normalized_event in {"failed", "timeout", "expired", "interrupted", "measurement_failed"} or normalized_status in {"failed", "timeout", "expired", "interrupted"}:
        return "error"
    if normalized_event in {"cancelled", "skipped", "preempted"}:
        return "warning"
    if normalized_event in {"response", "measurement_saved", "completed"}:
        return "success"
    if normalized_event in {"pending", "queued", "running", "sent", "recovering", "cancel_requested"}:
        return "info"
    return "info"


def _command_event_title(event_type: str) -> str:
    return {
        "pending": "Command pending",
        "queued": "Command queued",
        "running": "Command running",
        "sent": "Command sent",
        "response": "Device response",
        "completed": "Command completed",
        "measurement_saved": "Measurement saved",
        "measurement_failed": "Measurement failed",
        "failed": "Command failed",
        "timeout": "Command timeout",
        "expired": "Command expired",
        "interrupted": "Command interrupted",
        "cancel_requested": "Cancellation requested",
        "cancelled": "Command cancelled",
        "skipped": "Command skipped",
        "preempted": "Command preempted",
        "recovering": "Command recovering",
    }.get(str(event_type or "").strip().lower(), f"Command event: {event_type}")


def _command_event_message(event: ControlCommandEvent) -> str:
    command = event.command
    payload = event.event_payload if isinstance(event.event_payload, dict) else {}
    event_type = str(event.event_type or "").strip().lower()
    command_text = _command_text(command)

    if event_type == "pending":
        requested_by = _payload_text(payload, "requested_by", getattr(command, "requested_by", "system"))
        return f"{command_text} is pending for {requested_by}."
    if event_type == "queued":
        requested_by = _payload_text(payload, "requested_by", getattr(command, "requested_by", "system"))
        return f"{command_text} requested by {requested_by}."
    if event_type == "running":
        worker_id = _payload_text(payload, "worker_id")
        return f"{command_text} started on {worker_id}." if worker_id else f"{command_text} started."
    if event_type == "sent":
        return f"{command_text} was sent to the device."
    if event_type == "response":
        response_text = _payload_text(payload, "response_text")
        if response_text:
            return f"{command_text} returned: {response_text}"
        response_hex = _payload_text(payload, "response_hex")
        if response_hex:
            return f"{command_text} returned hex payload: {response_hex}"
        return f"{command_text} returned an acknowledgement."
    if event_type == "completed":
        return f"{command_text} completed successfully."
    if event_type == "measurement_saved":
        channel_code = _payload_text(payload, "channel_code", "measurement")
        value = payload.get("numeric_value", payload.get("text_value")) if isinstance(payload, dict) else None
        unit = _payload_text(payload, "unit")
        suffix = f" {unit}" if unit else ""
        return f"{channel_code} saved from {command_text}: {value}{suffix}"
    if event_type == "measurement_failed":
        return _payload_text(payload, "message", f"Measurement persistence failed for {command_text}.")
    if event_type == "cancel_requested":
        return _payload_text(payload, "reason", f"Cancellation was requested for {command_text}.")
    if event_type in {"failed", "timeout", "expired", "interrupted", "cancelled", "skipped", "preempted"}:
        return _payload_text(payload, "message", getattr(command, "error_message", "") or f"{command_text} failed.")
    if event_type == "recovering":
        return _payload_text(payload, "message", f"{command_text} is being recovered after restart.")
    return f"{command_text}: {event.event_type}"


def _is_read_command(command_text: str) -> bool:
    return command_text.strip().upper().startswith("IN_")


def _is_noisy_command_event(event: ControlCommandEvent) -> bool:
    command = event.command
    event_type = str(event.event_type or "").strip().lower()

    if event_type in {"pending", "queued", "running", "recovering"}:
        return True
    if event_type in {"failed", "timeout", "expired", "interrupted", "measurement_failed", "cancelled", "skipped", "preempted"}:
        return False
    if command is None:
        return False

    command_text = _command_text(command)
    requested_by = str(command.requested_by or "").strip().lower()
    if requested_by == "manual_reconciler" and _is_read_command(command_text):
        return True
    return False


def _recipe_actor(event: RecipeProgramEvent) -> str:
    run = event.run
    if run is None:
        return "Recipe program"
    return str(run.recipe_title or f"Recipe {run.recipe_id or ''}").strip() or "Recipe program"


def _recipe_event_severity(event_type: str) -> str:
    normalized = str(event_type or "").strip().lower()
    if normalized in {"error", "failed"}:
        return "error"
    if normalized in {"stopped", "aborted", "stop_requested"}:
        return "warning"
    if normalized in {"completed"}:
        return "success"
    return "info"


def _recipe_event_title(event_type: str) -> str:
    return {
        "started": "Recipe started",
        "stopped": "Recipe stopped",
        "completed": "Recipe completed",
        "error": "Recipe error",
        "step_started": "Recipe step started",
        "targets_applied": "Recipe targets applied",
    }.get(str(event_type or "").strip().lower(), f"Recipe event: {event_type}")


def _recipe_event_message(event: RecipeProgramEvent) -> str:
    payload = event.event_payload if isinstance(event.event_payload, dict) else {}
    event_type = str(event.event_type or "").strip().lower()
    run = event.run
    requested_by = str(getattr(run, "requested_by", "") or "system")

    if event_type == "started":
        return f"Program started by {requested_by}."
    if event_type == "stopped":
        return f"Program stopped by {requested_by}."
    if event_type == "completed":
        return "Program completed successfully."
    if event_type == "error":
        return _payload_text(payload, "error", getattr(run, "last_error", "") or "Program stopped with an error.")
    if event_type == "step_started":
        step = event.active_step_index
        return f"Step {(int(step) + 1) if step is not None else '?'} started."
    if event_type == "targets_applied":
        changes = payload.get("changes") if isinstance(payload, dict) else None
        if isinstance(changes, list):
            return f"{len(changes)} actor target(s) applied."
        return "Actor targets applied."
    return str(event.event_type or "Recipe program event")


def _connection_actor(connection: DeviceConnection) -> str:
    binding = connection.current_binding
    if binding is not None and binding.device is not None:
        return str(binding.device.display_name or f"Device {binding.device_id}")
    server = connection.device_server
    server_code = str(getattr(server, "server_code", "") or "").strip()
    label = str(connection.connection_label or f"Port {connection.port_number}").strip()
    return f"{server_code} {label}".strip() or f"Connection {connection.connection_id}"


def _manual_state_actor(state: DeviceManualState) -> str:
    if state.device is not None and getattr(state.device, "display_name", None):
        return str(state.device.display_name)
    return f"Device {state.device_id}"


def load_activity_logs(*, days: int = 7, limit: int = 120) -> list[ActivityLogItem]:
    cutoff = activity_log_cutoff(days)
    normalized_limit = max(1, int(limit))
    per_source_limit = max(normalized_limit * 6, 200)
    items: list[ActivityLogItem] = []

    command_events = (
        ControlCommandEvent.query.options(
            joinedload(ControlCommandEvent.command).joinedload(ControlCommand.device),
        )
        .filter(ControlCommandEvent.created_at >= cutoff)
        .order_by(ControlCommandEvent.created_at.desc(), ControlCommandEvent.command_event_id.desc())
        .limit(per_source_limit)
        .all()
    )
    for event in command_events:
        if _is_noisy_command_event(event):
            continue

        command = event.command
        severity = _command_event_severity(event.event_type, command)
        items.append(
            ActivityLogItem(
                event_key=f"command:{event.command_event_id}",
                timestamp=event.created_at,
                severity=severity,
                category="Actor",
                title=_command_event_title(event.event_type),
                message=_command_event_message(event),
                actor=_command_actor(command),
                source="control_command_event",
                status=str(getattr(command, "status", "") or event.event_type),
                details=f"command_id={getattr(command, 'command_id', '')} event_id={event.command_event_id}",
            )
        )

    recipe_events = (
        RecipeProgramEvent.query.options(joinedload(RecipeProgramEvent.run))
        .filter(RecipeProgramEvent.created_at >= cutoff)
        .order_by(RecipeProgramEvent.created_at.desc(), RecipeProgramEvent.recipe_program_event_id.desc())
        .limit(per_source_limit)
        .all()
    )
    for event in recipe_events:
        items.append(
            ActivityLogItem(
                event_key=f"recipe:{event.recipe_program_event_id}",
                timestamp=event.created_at,
                severity=_recipe_event_severity(event.event_type),
                category="Recipe",
                title=_recipe_event_title(event.event_type),
                message=_recipe_event_message(event),
                actor=_recipe_actor(event),
                source="recipe_program_event",
                status=str(event.event_type or ""),
                details=f"run_id={getattr(event.run, 'recipe_program_run_id', '')} event_id={event.recipe_program_event_id}",
            )
        )

    connection_errors = (
        DeviceConnection.query.options(
            joinedload(DeviceConnection.device_server),
            joinedload(DeviceConnection.current_binding).joinedload(DeviceBindingCurrent.device),
        )
        .filter(DeviceConnection.last_error.is_not(None), DeviceConnection.updated_at >= cutoff)
        .order_by(DeviceConnection.updated_at.desc(), DeviceConnection.connection_id.desc())
        .limit(per_source_limit)
        .all()
    )
    for connection in connection_errors:
        label = str(connection.connection_label or f"Port {connection.port_number}").strip()
        items.append(
            ActivityLogItem(
                event_key=f"connection:{connection.connection_id}:{connection.updated_at.isoformat() if connection.updated_at else ''}",
                timestamp=connection.updated_at,
                severity="error",
                category="Connection",
                title="Connection error",
                message=str(connection.last_error or "Connection failed."),
                actor=_connection_actor(connection),
                source="device_connection",
                status="error",
                details=label,
            )
        )

    manual_errors = (
        DeviceManualState.query.options(joinedload(DeviceManualState.device))
        .filter(DeviceManualState.last_error.is_not(None), DeviceManualState.updated_at >= cutoff)
        .order_by(DeviceManualState.updated_at.desc(), DeviceManualState.device_id.desc())
        .limit(per_source_limit)
        .all()
    )
    for state in manual_errors:
        items.append(
            ActivityLogItem(
                event_key=f"manual:{state.device_id}:{state.updated_at.isoformat() if state.updated_at else ''}",
                timestamp=state.updated_at,
                severity="error",
                category="Manual",
                title="Manual runtime error",
                message=str(state.last_error or "Manual runtime failed."),
                actor=_manual_state_actor(state),
                source="device_manual_state",
                status=str(state.queue_status or "error"),
                details=f"device_id={state.device_id}",
            )
        )

    return sorted(
        items,
        key=lambda item: item.timestamp or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:normalized_limit]


def summarize_activity_logs(items: list[ActivityLogItem]) -> dict[str, int]:
    summary = {
        "total": len(items),
        "errors": 0,
        "warnings": 0,
        "success": 0,
        "info": 0,
    }
    for item in items:
        severity = str(item.severity or "info").strip().lower()
        if severity == "error":
            summary["errors"] += 1
        elif severity == "warning":
            summary["warnings"] += 1
        elif severity == "success":
            summary["success"] += 1
        else:
            summary["info"] += 1
    return summary
