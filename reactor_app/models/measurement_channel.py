from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class MeasurementChannel(db.Model):
    __tablename__ = "measurement_channel"

    channel_id = db.Column(unsigned_bigint(), primary_key=True)
    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    channel_code = db.Column(db.String(64), nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    unit = db.Column(db.String(32), nullable=False)
    value_type = db.Column(db.String(16), nullable=False, server_default=db.text("'float'"))
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    __table_args__ = (
        db.UniqueConstraint("device_id", "channel_code", name="uq_channel_device_code"),
    )

    device = db.relationship("Device", back_populates="channels")
    measurements = db.relationship("Measurement", back_populates="channel")
