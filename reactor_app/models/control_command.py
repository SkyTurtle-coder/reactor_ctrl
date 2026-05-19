from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class ControlCommand(db.Model):
    __tablename__ = "control_command"

    command_id = db.Column(unsigned_bigint(), primary_key=True)
    device_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_uuid = db.Column(db.String(36), nullable=False, unique=True)
    requested_by = db.Column(db.String(100), nullable=False, server_default=db.text("'system'"))
    command_name = db.Column(db.String(100), nullable=False)
    command_payload = db.Column(db.JSON)
    status = db.Column(db.String(16), nullable=False, server_default=db.text("'queued'"), index=True)
    requested_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    scheduled_for = db.Column(db.DateTime(timezone=True), index=True)
    sent_at = db.Column(db.DateTime(timezone=True))
    ack_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    retry_count = db.Column(db.Integer, nullable=False, server_default=db.text("0"))
    error_message = db.Column(db.Text)

    device = db.relationship("Device", back_populates="commands")
    events = db.relationship("ControlCommandEvent", back_populates="command")
