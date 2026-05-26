from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class Recipe(db.Model):
    __tablename__ = "recipe"

    recipe_id = db.Column(unsigned_bigint(), primary_key=True)
    title = db.Column(db.String(120), nullable=False, index=True)
    operator_name = db.Column(db.String(120), nullable=False)
    version = db.Column(db.SmallInteger, nullable=False, server_default=db.text("1"))
    status = db.Column(db.String(32), nullable=False, server_default=db.text("'draft'"), index=True)
    reactor_build_id = db.Column(
        unsigned_bigint(),
        db.ForeignKey("reactor_build.reactor_build_id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    steps_json = db.Column(db.JSON, nullable=False)
    safe_state_json = db.Column(db.JSON, nullable=True)
    created_by = db.Column(db.String(120), nullable=False)
    updated_by = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
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
