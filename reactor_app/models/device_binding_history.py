from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class DeviceBindingHistory(db.Model):
    __tablename__ = "device_binding_history"

    binding_history_id = db.Column(unsigned_bigint(), primary_key=True)
    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device_connection.connection_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bound_from = db.Column(db.DateTime(timezone=True), nullable=False)
    bound_to = db.Column(db.DateTime(timezone=True))
    reason = db.Column(db.String(255))

    device = db.relationship("Device", back_populates="binding_history")
    connection = db.relationship("DeviceConnection", back_populates="binding_history")
