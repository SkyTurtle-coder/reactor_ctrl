from __future__ import annotations

from ..extensions import db


class DiscoveryResult(db.Model):
    __tablename__ = "discovery_result"

    discovery_result_id = db.Column(db.BigInteger, primary_key=True)
    discovery_run_id = db.Column(
        db.BigInteger,
        db.ForeignKey("discovery_run.discovery_run_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    rs485_address = db.Column(db.SmallInteger, nullable=False)
    protocol = db.Column(db.String(32), nullable=False)
    device_type_guess = db.Column(db.String(64))
    manufacturer_serial = db.Column(db.String(128), index=True)
    raw_identity = db.Column(db.JSON)
    matched_device_id = db.Column(
        db.BigInteger,
        db.ForeignKey("device.device_id", onupdate="CASCADE", ondelete="SET NULL"),
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.text("CURRENT_TIMESTAMP(3)"))

    __table_args__ = (
        db.UniqueConstraint("discovery_run_id", "rs485_address", name="uq_discovery_run_addr"),
    )

    run = db.relationship("DiscoveryRun", back_populates="results")
    matched_device = db.relationship("Device", back_populates="discovery_matches")
