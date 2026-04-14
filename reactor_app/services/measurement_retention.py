from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import Flask
from sqlalchemy import text

from ..extensions import db


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_for_days(days: int) -> datetime:
    """Return the UTC cutoff timestamp for measurements older than *days* days."""
    return _now_utc() - timedelta(days=days)


@dataclass
class RetentionResult:
    cutoff: datetime
    rows_deleted: int = 0
    batches_run: int = 0
    dry_run: bool = False
    stopped_early: bool = False
    error: str | None = None
    elapsed_seconds: float = 0.0

    def as_log_line(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        status = f"error={self.error!r}" if self.error else "ok"
        early = " stopped_early=true" if self.stopped_early else ""
        return (
            f"measurement_retention [{mode}] "
            f"cutoff={self.cutoff.isoformat()} "
            f"deleted={self.rows_deleted} "
            f"batches={self.batches_run}"
            f"{early} "
            f"elapsed={self.elapsed_seconds:.2f}s "
            f"status={status}"
        )


def run_retention(app: Flask) -> RetentionResult:
    """Delete measurements older than the configured retention window.

    Must be called within an active Flask application context.
    Returns a :class:`RetentionResult` describing what happened.
    """
    enabled: bool = bool(app.config.get("MEASUREMENT_RETENTION_ENABLED", False))
    days: int = max(1, int(app.config.get("MEASUREMENT_RETENTION_DAYS", 30)))
    batch_size: int = max(1, int(app.config.get("MEASUREMENT_RETENTION_BATCH_SIZE", 10_000)))
    max_batches: int = max(1, int(app.config.get("MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN", 50)))
    dry_run: bool = bool(app.config.get("MEASUREMENT_RETENTION_DRY_RUN", False))

    cutoff = cutoff_for_days(days)
    result = RetentionResult(cutoff=cutoff, dry_run=dry_run)
    t0 = time.monotonic()

    if not enabled:
        app.logger.info(
            "measurement_retention: disabled (set MEASUREMENT_RETENTION_ENABLED=true to enable)"
        )
        result.elapsed_seconds = time.monotonic() - t0
        return result

    try:
        if dry_run:
            count = db.session.execute(
                text("SELECT COUNT(*) FROM measurement WHERE measured_at < :cutoff"),
                {"cutoff": cutoff},
            ).scalar()
            result.rows_deleted = int(count or 0)
        else:
            # Delete in small batches ordered by measured_at so that:
            # - each transaction is short and MySQL lock pressure stays low;
            # - oldest rows go first, keeping the hot working set recent;
            # - the loop terminates once no full batch remains.
            for batch_num in range(1, max_batches + 1):
                deleted = db.session.execute(
                    text(
                        "DELETE FROM measurement WHERE measured_at < :cutoff "
                        "ORDER BY measured_at LIMIT :batch_size"
                    ),
                    {"cutoff": cutoff, "batch_size": batch_size},
                ).rowcount
                db.session.commit()
                result.rows_deleted += deleted
                result.batches_run = batch_num
                if deleted < batch_size:
                    # Fewer rows than the batch limit → nothing left before the cutoff.
                    break
            else:
                # Exited without break → hit max_batches; more rows may remain.
                result.stopped_early = True

    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        result.error = str(exc)
        app.logger.exception("measurement_retention failed")

    result.elapsed_seconds = time.monotonic() - t0
    app.logger.info(result.as_log_line())
    return result
