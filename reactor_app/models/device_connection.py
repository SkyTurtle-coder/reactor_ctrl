from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class DeviceConnection(db.Model):
    __tablename__ = "device_connection"

    connection_id = db.Column(unsigned_bigint(), primary_key=True)
    device_server_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device_server.device_server_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    port_number = db.Column(db.SmallInteger, nullable=False)
    connection_label = db.Column(db.String(120))
    transport_type = db.Column(db.String(16), nullable=False, server_default=db.text("'tcp_socket'"))
    tcp_host = db.Column(db.String(255), nullable=False)
    tcp_port = db.Column(db.Integer, nullable=False)
    baud_rate = db.Column(db.Integer, nullable=False, server_default=db.text("9600"))
    data_bits = db.Column(db.SmallInteger, nullable=False, server_default=db.text("8"))
    parity = db.Column(db.String(1), nullable=False, server_default=db.text("'N'"))
    stop_bits = db.Column(db.SmallInteger, nullable=False, server_default=db.text("1"))
    flow_control = db.Column(db.String(16), nullable=False, server_default=db.text("'none'"))
    read_timeout_ms = db.Column(db.Integer, nullable=False, server_default=db.text("1200"))
    write_timeout_ms = db.Column(db.Integer, nullable=False, server_default=db.text("1200"))
    reconnect_delay_ms = db.Column(db.Integer, nullable=False, server_default=db.text("1000"))
    last_seen_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)
    # Remembers which setpoint write variant last worked for CC230 devices.
    # 0 = SETPOINT!, 1 = SET decimal, 2 = SET centi-degree, 3 = MATLAB SET.
    # NULL means no preference; the driver chooses a safe order for the sign.
    cc230_setpoint_write_mode = db.Column(db.SmallInteger, nullable=True)
    is_enabled = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    __table_args__ = (
        db.UniqueConstraint("device_server_id", "port_number", name="uq_connection_server_port"),
        db.UniqueConstraint("tcp_host", "tcp_port", name="uq_connection_tcp_endpoint"),
    )

    device_server = db.relationship("DeviceServer", back_populates="connections")
    current_binding = db.relationship("DeviceBindingCurrent", back_populates="connection", uselist=False)
    binding_history = db.relationship("DeviceBindingHistory", back_populates="connection")
