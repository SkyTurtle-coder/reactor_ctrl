from __future__ import annotations

from ..extensions import db


class SerialAdapter(db.Model):
    __tablename__ = "serial_adapter"

    adapter_id = db.Column(db.BigInteger, primary_key=True)
    adapter_uid = db.Column(db.String(36), nullable=False, unique=True)
    hub_id = db.Column(db.BigInteger, db.ForeignKey("usb_hub.hub_id", onupdate="CASCADE", ondelete="SET NULL"))
    adapter_label = db.Column(db.String(100))
    usb_vendor_id = db.Column(db.String(8))
    usb_product_id = db.Column(db.String(8))
    usb_serial = db.Column(db.String(128), unique=True)
    usb_location_path = db.Column(db.String(255))
    driver_info = db.Column(db.String(255))
    last_seen_port = db.Column(db.String(64), index=True)
    last_seen_at = db.Column(db.DateTime(timezone=True))
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    hub = db.relationship("UsbHub", back_populates="adapters")
    bus = db.relationship("Rs485Bus", back_populates="adapter", uselist=False)
