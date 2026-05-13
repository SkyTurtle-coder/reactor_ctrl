from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class Device(db.Model):
    __tablename__ = "device"

    device_id = db.Column(unsigned_bigint(), primary_key=True)
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

    current_binding = db.relationship("DeviceBindingCurrent", back_populates="device", uselist=False, passive_deletes=True)
    binding_history = db.relationship("DeviceBindingHistory", back_populates="device", passive_deletes=True)
    channels = db.relationship("MeasurementChannel", back_populates="device", passive_deletes=True)
    measurements = db.relationship("Measurement", back_populates="device", passive_deletes=True)
    commands = db.relationship("ControlCommand", back_populates="device", passive_deletes=True)
    manual_state = db.relationship("DeviceManualState", back_populates="device", uselist=False, passive_deletes=True)
    soft_sensor_estimates = db.relationship("SoftSensorEstimate", back_populates="device", passive_deletes=True)
