from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class SoftSensorEstimate(db.Model):
    __tablename__ = "soft_sensor_estimate"

    soft_sensor_estimate_id = db.Column(unsigned_bigint(), primary_key=True)
    soft_sensor_model_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("soft_sensor_model.soft_sensor_model_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="SET NULL"),
        index=True,
    )
    metric_name = db.Column(db.String(64), nullable=False)
    estimated_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    numeric_value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(32), nullable=False)
    confidence = db.Column(db.Numeric(5, 4))
    input_snapshot = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))

    model = db.relationship("SoftSensorModel", back_populates="estimates")
    device = db.relationship("Device", back_populates="soft_sensor_estimates")
