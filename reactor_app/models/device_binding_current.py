from __future__ import annotations

from ..extensions import db


class DeviceBindingCurrent(db.Model):
    __tablename__ = "device_binding_current"

    device_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    connection_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device_connection.connection_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    first_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    is_online = db.Column(db.Boolean, nullable=False, server_default=db.text("0"), index=True)
    quality_state = db.Column(db.String(32), nullable=False, server_default=db.text("'unknown'"))

    device = db.relationship("Device", back_populates="current_binding")
    connection = db.relationship("DeviceConnection", back_populates="current_binding")
