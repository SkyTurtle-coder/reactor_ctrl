from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class SoftSensorModel(db.Model):
    __tablename__ = "soft_sensor_model"

    soft_sensor_model_id = db.Column(unsigned_bigint(), primary_key=True)
    model_code = db.Column(db.String(64), nullable=False)
    model_version = db.Column(db.String(32), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    __table_args__ = (
        db.UniqueConstraint("model_code", "model_version", name="uq_soft_sensor_model_code_version"),
    )

    estimates = db.relationship("SoftSensorEstimate", back_populates="model")
