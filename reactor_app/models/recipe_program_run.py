from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class RecipeProgramRun(db.Model):
    __tablename__ = "recipe_program_run"

    recipe_program_run_id = db.Column(unsigned_bigint(), primary_key=True)
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
    status = db.Column(db.String(32), nullable=False, server_default=db.text("'running'"), index=True)
    requested_by = db.Column(db.String(100), nullable=False, server_default=db.text("'system'"))
    recipe_title = db.Column(db.String(120))
    operator_name = db.Column(db.String(120))
    snapshot_json = db.Column(db.JSON)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"), index=True)
    finished_at = db.Column(db.DateTime(timezone=True), index=True)
    last_progress_at = db.Column(db.DateTime(timezone=True), index=True)
    last_error = db.Column(db.String(500))
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
    events = db.relationship(
        "RecipeProgramEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RecipeProgramEvent.recipe_program_event_id.asc()",
    )
