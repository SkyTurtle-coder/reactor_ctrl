from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class ControlCommandEvent(db.Model):
    __tablename__ = "control_command_event"

    command_event_id = db.Column(unsigned_bigint(), primary_key=True)
    command_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("control_command.command_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(32), nullable=False)
    event_payload = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))

    command = db.relationship("ControlCommand", back_populates="events")
