from __future__ import annotations

from ..extensions import db
from ._types import unsigned_bigint


class ReactorBuild(db.Model):
    __tablename__ = "reactor_build"

    reactor_build_id = db.Column(unsigned_bigint(), primary_key=True)
    build_name = db.Column(db.String(120), nullable=False, index=True)
    build_date = db.Column(db.Date, nullable=False)
    created_by = db.Column(db.String(120), nullable=False)
    updated_by = db.Column(db.String(120))
    definition_json = db.Column(db.JSON, nullable=False)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text("1"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=db.text("CURRENT_TIMESTAMP(3)"),
    )
