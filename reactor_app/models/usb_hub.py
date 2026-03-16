from __future__ import annotations

from ..extensions import db


class UsbHub(db.Model):
    __tablename__ = "usb_hub"

    hub_id = db.Column(db.BigInteger, primary_key=True)
    hub_name = db.Column(db.String(100), nullable=False)
    hub_serial = db.Column(db.String(128), unique=True)
    host_name = db.Column(db.String(100))
    physical_location = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    adapters = db.relationship("SerialAdapter", back_populates="hub")
