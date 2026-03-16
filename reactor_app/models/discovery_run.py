from __future__ import annotations

from ..extensions import db


class DiscoveryRun(db.Model):
    __tablename__ = "discovery_run"

    discovery_run_id = db.Column(db.BigInteger, primary_key=True)
    bus_id = db.Column(
        db.BigInteger,
        db.ForeignKey("rs485_bus.bus_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_by = db.Column(db.String(100), nullable=False, server_default=db.text("'system'"))
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))
    finished_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(16), nullable=False, server_default=db.text("'running'"))
    notes = db.Column(db.String(255))

    bus = db.relationship("Rs485Bus", back_populates="discovery_runs")
    results = db.relationship("DiscoveryResult", back_populates="run")
