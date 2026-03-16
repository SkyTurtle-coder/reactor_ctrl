from __future__ import annotations

from ..extensions import db


class Device(db.Model):
    __tablename__ = "device"

    device_id = db.Column(db.BigInteger, primary_key=True)
    asset_serial = db.Column(db.String(64), nullable=False, unique=True)
    manufacturer_serial = db.Column(db.String(128))
    display_name = db.Column(db.String(120), nullable=False)
    device_type = db.Column(db.String(64), nullable=False, index=True)
    protocol = db.Column(db.String(32), nullable=False)
    firmware_version = db.Column(db.String(64))
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    __table_args__ = (
        db.UniqueConstraint("manufacturer_serial", "protocol", name="uq_device_manufacturer_serial_protocol"),
    )

    current_binding = db.relationship("DeviceBindingCurrent", back_populates="device", uselist=False)
    binding_history = db.relationship("DeviceBindingHistory", back_populates="device")
    channels = db.relationship("MeasurementChannel", back_populates="device")
    measurements = db.relationship("Measurement", back_populates="device")
    commands = db.relationship("ControlCommand", back_populates="device")
    soft_sensor_estimates = db.relationship("SoftSensorEstimate", back_populates="device")
    discovery_matches = db.relationship("DiscoveryResult", back_populates="matched_device")
