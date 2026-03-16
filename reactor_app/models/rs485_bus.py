from __future__ import annotations

from ..extensions import db


class Rs485Bus(db.Model):
    __tablename__ = "rs485_bus"

    bus_id = db.Column(db.BigInteger, primary_key=True)
    adapter_id = db.Column(
        db.BigInteger,
        db.ForeignKey("serial_adapter.adapter_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    bus_name = db.Column(db.String(100), nullable=False, unique=True)
    protocol = db.Column(db.String(32), nullable=False, server_default=db.text("'modbus_rtu'"))
    baud_rate = db.Column(db.Integer, nullable=False, server_default=db.text("9600"))
    data_bits = db.Column(db.SmallInteger, nullable=False, server_default=db.text("8"))
    parity = db.Column(db.String(1), nullable=False, server_default=db.text("'N'"))
    stop_bits = db.Column(db.SmallInteger, nullable=False, server_default=db.text("1"))
    poll_interval_ms = db.Column(db.Integer, nullable=False, server_default=db.text("1000"))
    timeout_ms = db.Column(db.Integer, nullable=False, server_default=db.text("1200"))
    is_enabled = db.Column(db.Boolean, nullable=False, server_default=db.text("1"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    adapter = db.relationship("SerialAdapter", back_populates="bus")
    current_bindings = db.relationship("DeviceBindingCurrent", back_populates="bus")
    binding_history = db.relationship("DeviceBindingHistory", back_populates="bus")
    discovery_runs = db.relationship("DiscoveryRun", back_populates="bus")
