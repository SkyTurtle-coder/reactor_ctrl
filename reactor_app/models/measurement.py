from __future__ import annotations

from ..extensions import db


class Measurement(db.Model):
    __tablename__ = "measurement"

    measurement_id = db.Column(db.BigInteger, primary_key=True)
    device_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = db.Column(
        db.BigInteger,
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
