from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class RecipeProgramState(db.Model):
    __tablename__ = "recipe_program_state"

    recipe_program_state_id = db.Column(unsigned_bigint(), primary_key=True)
    recipe_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("recipe.recipe_id", onupdate="CASCADE", ondelete="SET NULL"),
        index=True,
    )
    reactor_build_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("reactor_build.reactor_build_id", onupdate="CASCADE", ondelete="SET NULL"),
        index=True,
    )
    status = db.Column(db.String(32), nullable=False, server_default=db.text("'idle'"), index=True)
    requested_by = db.Column(db.String(100), nullable=False, server_default=db.text("'system'"))
    recipe_title = db.Column(db.String(120))
    operator_name = db.Column(db.String(120))
    snapshot_json = db.Column(db.JSON)
    last_applied_targets_json = db.Column(db.JSON)
    active_step_index = db.Column(db.Integer, nullable=False, server_default=db.text("0"))
    step_started_at = db.Column(db.DateTime(timezone=True))
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    last_progress_at = db.Column(db.DateTime(timezone=True))
    stop_requested = db.Column(db.Boolean, nullable=False, server_default=db.text("0"))
    last_error = db.Column(db.String(500))
    lease_owner = db.Column(db.String(64), index=True)
    lease_expires_at = db.Column(db.DateTime(timezone=True), index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )

    recipe = db.relationship("Recipe")
    reactor_build = db.relationship("ReactorBuild")
