from __future__ import annotations

from ..extensions import db


class DeviceBindingHistory(db.Model):
    __tablename__ = "device_binding_history"

    binding_history_id = db.Column(db.BigInteger, primary_key=True)
    device_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device_connection.connection_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bound_from = db.Column(db.DateTime(timezone=True), nullable=False)
    bound_to = db.Column(db.DateTime(timezone=True))
    reason = db.Column(db.String(255))

    device = db.relationship("Device", back_populates="binding_history")
    connection = db.relationship("DeviceConnection", back_populates="binding_history")
