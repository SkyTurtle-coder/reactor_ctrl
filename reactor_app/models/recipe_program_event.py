from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class RecipeProgramEvent(db.Model):
    __tablename__ = "recipe_program_event"

    recipe_program_event_id = db.Column(unsigned_bigint(), primary_key=True)
    recipe_program_run_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("recipe_program_run.recipe_program_run_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(32), nullable=False, index=True)
    active_step_index = db.Column(db.Integer, index=True)
    event_payload = db.Column(db.JSON)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        index=True,
    )

    run = db.relationship("RecipeProgramRun", back_populates="events")
