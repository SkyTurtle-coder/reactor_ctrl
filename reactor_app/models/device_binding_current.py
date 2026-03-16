from __future__ import annotations

from ..extensions import db


class DeviceBindingCurrent(db.Model):
    __tablename__ = "device_binding_current"

    device_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    bus_id = db.Column(
        db.BigInteger,
        db.ForeignKey("rs485_bus.bus_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    rs485_address = db.Column(db.SmallInteger, nullable=False)
    register_profile = db.Column(db.String(120))
    first_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    is_online = db.Column(db.Boolean, nullable=False, server_default=db.text("0"), index=True)
    quality_state = db.Column(db.String(32), nullable=False, server_default=db.text("'unknown'"))

    __table_args__ = (
        db.UniqueConstraint("bus_id", "rs485_address", name="uq_binding_bus_address"),
    )

    device = db.relationship("Device", back_populates="current_binding")
    bus = db.relationship("Rs485Bus", back_populates="current_bindings")
