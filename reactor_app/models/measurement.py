from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class Measurement(db.Model):
    __tablename__ = "measurement"
    __table_args__ = (
        db.Index("ix_measurement_device_channel_measured_at", "device_id", "channel_code", "measured_at"),
        db.Index("ix_measurement_device_measured_at", "device_id", "measured_at"),
    )

    measurement_id = db.Column(unsigned_bigint(), primary_key=True)
    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("measurement_channel.channel_id", onupdate="CASCADE", ondelete="SET NULL"),
    )
    channel_code = db.Column(db.String(64), nullable=False, index=True)
    measured_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    ingested_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    numeric_value = db.Column(db.Float)
    text_value = db.Column(db.String(255))
    unit = db.Column(db.String(32))
    quality_score = db.Column(db.Numeric(5, 4))
    raw_payload = db.Column(db.JSON)
    source = db.Column(db.String(32), nullable=False, server_default=db.text("'poller'"), index=True)

    device = db.relationship("Device", back_populates="measurements")
    channel = db.relationship("MeasurementChannel", back_populates="measurements")
