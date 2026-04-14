from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from flask import Flask
from sqlalchemy import and_, case, or_
from sqlalchemy.exc import OperationalError

from ..extensions import db
from ..models import Device, DeviceManualState, Measurement, MeasurementChannel
from .device_runtime import DeviceCommandError, execute_device_command


_WORKER_EXTENSION_KEY = "device_manual_reconciler_thread"
_DEVICE_DISCOVERY_INTERVAL_SECONDS = 60  # how often to scan for new active IKA devices

# Channel definitions for IKA telemetry that are persisted as measurements
# on every reconciler poll cycle.  channel_code values must match the codes
# referenced in web.py's _fallback_plot_channels_for_target so the plot can
# seamlessly switch from runtime_fallback to stored measurements.
_IKA_TELEMETRY_CHANNELS: tuple[dict, ...] = (
    {"key": "setpoint_rpm", "channel_code": "ika_setpoint_rpm", "display_name": "Setpoint RPM", "unit": "rpm"},
    {"key": "actual_rpm",   "channel_code": "ika_actual_rpm",   "display_name": "Actual RPM",   "unit": "rpm"},
    {"key": "torque_ncm",   "channel_code": "ika_torque_ncm",   "display_name": "Torque",        "unit": "Ncm"},
)


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


def _manual_watch_ttl(app: Flask) -> timedelta:
    seconds = max(5, int(app.config.get("DEVICE_MANUAL_RECONCILER_WATCH_TTL_SECONDS", 30)))
    return timedelta(seconds=seconds)


def _manual_poll_interval(app: Flask) -> timedelta:
    milliseconds = max(1000, int(app.config.get("DEVICE_MANUAL_RECONCILER_POLL_MS", 2500)))
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
    return str(getattr(device, "protocol", "") or "").strip().lower() == "ika_eurostar_60"


def _ensure_manual_state(device: Device) -> DeviceManualState:
    state = db.session.get(DeviceManualState, device.device_id)
    if state is not None:
        return state

    state = DeviceManualState(
        device_id=device.device_id,
        queue_status="idle",
        desired_version=0,
        applied_version=0,
    )
    db.session.add(state)
    db.session.flush()
    return state


def _telemetry_to_snapshot(state: DeviceManualState) -> dict[str, Any]:
    return {
        "is_on": bool(state.reported_is_on) if state.reported_is_on is not None else None,
        "setpoint_rpm": state.reported_setpoint_rpm,
        "actual_rpm": state.actual_rpm,
        "torque_ncm": state.torque_ncm,
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
    state = _ensure_manual_state(device)
    now = _now_utc()
    if watch:
        state.watch_expires_at = now + _manual_watch_ttl(app)
    if refresh or state.last_reported_at is None:
        state.next_poll_at = now
        if state.queue_status != "running":
            state.queue_status = "queued"
    db.session.flush()
    return state


def queue_manual_state_update(
    app: Flask,
    device: Device,
    *,
    desired_is_on: bool,
    desired_speed: int,
    requested_by: str,
) -> DeviceManualState:
    state = _ensure_manual_state(device)
    now = _now_utc()
    state.desired_is_on = bool(desired_is_on)
    state.desired_speed = int(desired_speed)
    state.desired_version = int(state.desired_version or 0) + 1
    state.requested_by = requested_by
    state.last_desired_at = now
    state.watch_expires_at = now + _manual_watch_ttl(app)
    state.next_poll_at = now
    if state.queue_status != "running":
        state.queue_status = "queued"
    state.last_error = None
    db.session.flush()
    return state


def _run_logged_manual_command(device: Device, command_text: str) -> str | None:
    try:
        execution = execute_device_command(
            device,
            command_name="manual_text",
            payload=_manual_command_payload(command_text),
            requested_by="manual_reconciler",
        )
    except DeviceCommandError as exc:
        if exc.command is not None:
            db.session.commit()
        raise

    db.session.commit()
    return execution.result.response_text


def _read_ika_status(device: Device) -> dict[str, float | None]:
    setpoint_response = _run_logged_manual_command(device, "IN_SP_4")
    actual_response = _run_logged_manual_command(device, "IN_PV_4")
    torque_response = _run_logged_manual_command(device, "IN_PV_5")

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


def _refresh_state_from_telemetry(state: DeviceManualState, telemetry: dict[str, float | None]) -> None:
    now = _now_utc()
    setpoint = telemetry.get("setpoint_rpm")
    actual = telemetry.get("actual_rpm")
    torque = telemetry.get("torque_ncm")
    state.reported_setpoint_rpm = None if setpoint is None else max(0, int(round(setpoint)))
    state.actual_rpm = actual
    state.torque_ncm = torque
    state.reported_is_on = bool(actual is not None and actual > 0.5)
    state.last_reported_at = now

    if state.desired_version == 0 and state.desired_is_on is None:
        state.desired_is_on = bool(state.reported_is_on)
        state.desired_speed = state.reported_setpoint_rpm or 0


def _persist_ika_telemetry_as_measurements(
    device: Device,
    telemetry: dict[str, float | None],
    measured_at: datetime,
) -> None:
    """Write IKA telemetry values to the measurement table.

    Called after every successful telemetry poll so that the complete
    history of actual and setpoint RPM plus torque is stored and
    available for the process-view plot and the Data export.
    """
    for spec in _IKA_TELEMETRY_CHANNELS:
        value = telemetry.get(spec["key"])
        if value is None:
            continue

        channel = MeasurementChannel.query.filter_by(
            device_id=device.device_id,
            channel_code=spec["channel_code"],
        ).one_or_none()

        if channel is None:
            channel = MeasurementChannel(
                device_id=device.device_id,
                channel_code=spec["channel_code"],
                display_name=spec["display_name"],
                unit=spec["unit"],
                value_type="float",
                is_active=True,
            )
            db.session.add(channel)
            db.session.flush()
        else:
            channel.display_name = spec["display_name"]
            channel.unit = spec["unit"]
            channel.is_active = True

        db.session.add(Measurement(
            device_id=device.device_id,
            channel_id=channel.channel_id,
            channel_code=channel.channel_code,
            measured_at=measured_at,
            numeric_value=float(value),
            unit=channel.unit,
            source="manual_reconciler",
        ))

    db.session.flush()


def _apply_desired_ika_state(device: Device, state: DeviceManualState) -> None:
    desired_is_on = bool(state.desired_is_on)
    desired_speed = max(0, int(state.desired_speed or 0))

    if desired_is_on:
        _run_logged_manual_command(device, "START_4")
        # Give the device time to process the start command before sending
        # the setpoint.  0.5 s is more robust than 0.18 s, especially after
        # a power cycle when firmware may not be fully ready.
        time.sleep(0.5)
        _run_logged_manual_command(device, f"OUT_SP_4 {desired_speed}")
        time.sleep(0.5)

        # Verify the setpoint was accepted: a None response means the device
        # is not communicating (e.g. still booting).  Raise so the reconciler
        # stores a visible error and retries on the next cycle.
        sp_response = _run_logged_manual_command(device, "IN_SP_4")
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

    _run_logged_manual_command(device, "STOP_4")
    # Give device time to process the stop command before subsequent reads.
    time.sleep(0.5)


def _ensure_manual_states_for_active_ika_devices(app: Flask) -> None:
    """Create DeviceManualState rows for active IKA devices that have no state row yet.

    This seeds background telemetry polling for devices that have never been
    accessed through the Process UI, so measurements are stored continuously
    regardless of whether any user has the page open.
    """
    active_devices = (
        db.session.query(Device)
        .filter(Device.protocol == "ika_eurostar_60", Device.is_active.is_(True))
        .all()
    )
    seeded = 0
    for device in active_devices:
        existing = db.session.get(DeviceManualState, device.device_id)
        if existing is None:
            state = DeviceManualState(
                device_id=device.device_id,
                queue_status="idle",
                desired_version=0,
                applied_version=0,
            )
            db.session.add(state)
            seeded += 1
    if seeded:
        db.session.commit()
        app.logger.info(
            "Measurement poller: seeded DeviceManualState for %d IKA device(s).", seeded
        )


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
        state.last_error = "Manual runtime is not supported for this device."
        _release_manual_state_lease(state, status="error")
        db.session.commit()
        return

    if not desired_pending and not poll_due:
        _release_manual_state_lease(state, status="idle")
        db.session.commit()
        return

    processed_version = int(state.desired_version or 0)
    try:
        if desired_pending:
            _apply_desired_ika_state(device, state)

        telemetry = _read_ika_status(device)
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            db.session.rollback()
            return

        _refresh_state_from_telemetry(state, telemetry)
        _persist_ika_telemetry_as_measurements(device, telemetry, state.last_reported_at)
        if desired_pending:
            # Final sanity check: if the desired state was ON but the setpoint
            # came back as None in the post-apply telemetry, the device silently
            # dropped our command (e.g. a second boot glitch).  Do NOT mark as
            # applied so the reconciler retries on the next cycle.
            if bool(state.desired_is_on) and state.reported_setpoint_rpm is None:
                raise RuntimeError(
                    "Stirrer accepted the START command but setpoint reads as "
                    "None in the subsequent telemetry poll. The device may have "
                    "reset. Will retry automatically."
                )
            state.applied_version = processed_version
        state.last_error = None
        # When a UI session is active use the fast live-poll interval.
        # Otherwise fall back to the slower background poll interval so
        # telemetry keeps being stored continuously even with no browser open.
        if watch_active:
            state.next_poll_at = _now_utc() + _manual_poll_interval(app)
        else:
            state.next_poll_at = _now_utc() + bg_interval
        _release_manual_state_lease(state, status="idle")
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        state = db.session.get(DeviceManualState, device_id)
        if state is None:
            return
        state.last_error = str(exc)
        state.next_poll_at = _now_utc() + _manual_poll_interval(app)
        _release_manual_state_lease(state, status="error")
        db.session.commit()
        app.logger.warning("Manual reconciler failed for device %s: %s", device_id, exc)


def _claim_next_device_id(app: Flask, worker_id: str) -> int | None:
    now = _now_utc()
    # Background telemetry cutoff: poll even without an active UI session.
    bg_cutoff = now - _background_poll_interval(app)
    candidates = (
        db.session.query(DeviceManualState.device_id)
        .filter(
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
                # Background telemetry poll: device hasn't been read recently
                or_(
                    DeviceManualState.last_reported_at.is_(None),
                    DeviceManualState.last_reported_at <= bg_cutoff,
                ),
            ),
        )
        .order_by(
            case((DeviceManualState.desired_version > DeviceManualState.applied_version, 0), else_=1),
            DeviceManualState.next_poll_at.asc(),
            DeviceManualState.last_desired_at.asc(),
            DeviceManualState.device_id.asc(),
        )
        .limit(16)
        .all()
    )

    lease_until = now + _manual_lease_duration(app)
    for (device_id,) in candidates:  # noqa: variable reuse
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
            if _is_mysql_record_changed_error(exc):
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
                    _ensure_manual_states_for_active_ika_devices(app)
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
