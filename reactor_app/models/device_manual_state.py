from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class DeviceManualState(db.Model):
    __tablename__ = "device_manual_state"

    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    desired_is_on = db.Column(db.Boolean)
    desired_speed = db.Column(db.Integer)
    desired_version = db.Column(db.Integer, nullable=False, server_default=db.text("0"))
    applied_version = db.Column(db.Integer, nullable=False, server_default=db.text("0"))
    requested_by = db.Column(db.String(100), nullable=False, server_default=db.text("'system'"))
    last_desired_at = db.Column(db.DateTime(timezone=True))
    reported_is_on = db.Column(db.Boolean)
    reported_setpoint_rpm = db.Column(db.Integer)
    actual_rpm = db.Column(db.Float)
    torque_ncm = db.Column(db.Float)
    active_control_sensor = db.Column(db.String(16))
    reported_extra = db.Column(db.JSON)
    last_reported_at = db.Column(db.DateTime(timezone=True))
    queue_status = db.Column(db.String(16), nullable=False, server_default=db.text("'idle'"), index=True)
    last_error = db.Column(db.Text)
    next_poll_at = db.Column(db.DateTime(timezone=True), index=True)
    watch_expires_at = db.Column(db.DateTime(timezone=True), index=True)
    lease_owner = db.Column(db.String(64), index=True)
    lease_expires_at = db.Column(db.DateTime(timezone=True), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    device = db.relationship("Device", back_populates="manual_state")
