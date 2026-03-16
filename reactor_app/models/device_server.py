from __future__ import annotations

from ..extensions import db


class DeviceServer(db.Model):
    __tablename__ = "device_server"

    device_server_id = db.Column(db.BigInteger, primary_key=True)
    server_code = db.Column(db.String(64), nullable=False, unique=True)
    display_name = db.Column(db.String(120), nullable=False)
    vendor = db.Column(db.String(64), nullable=False, server_default=db.text("'Moxa'"))
    model = db.Column(db.String(64))
    host = db.Column(db.String(255), nullable=False, unique=True)
    management_port = db.Column(db.Integer)
    serial_standard = db.Column(db.String(16), nullable=False, server_default=db.text("'rs232'"))
    port_count = db.Column(db.SmallInteger, nullable=False, server_default=db.text("8"))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    connections = db.relationship("DeviceConnection", back_populates="device_server", cascade="all, delete-orphan")
