from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from flask import Flask
from sqlalchemy import inspect
from sqlalchemy import text

from ..extensions import db
from .activity_log import activity_log_cutoff


@dataclass
class ActivityLogRetentionResult:
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
            f"activity_log_retention [{mode}] "
            f"cutoff={self.cutoff.isoformat()} "
            f"deleted={self.rows_deleted} "
            f"batches={self.batches_run}"
            f"{early} "
            f"elapsed={self.elapsed_seconds:.2f}s "
            f"status={status}"
        )


def _safe_table_exists(table_name: str) -> bool:
    return table_name in set(inspect(db.engine).get_table_names())


def _count_old_rows(cutoff: datetime) -> int:
    statements = [
        ("control_command_event", "SELECT COUNT(*) FROM control_command_event WHERE created_at < :cutoff"),
        (
            "control_command",
            "SELECT COUNT(*) FROM control_command c WHERE c.requested_at < :cutoff "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM control_command_event e "
            "  WHERE e.command_id = c.command_id AND e.created_at >= :cutoff"
            ")",
        ),
        ("recipe_program_event", "SELECT COUNT(*) FROM recipe_program_event WHERE created_at < :cutoff"),
        (
            "recipe_program_run",
            "SELECT COUNT(*) FROM recipe_program_run r "
            "WHERE COALESCE(r.finished_at, r.started_at) < :cutoff AND r.status <> 'running' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM recipe_program_event e "
            "  WHERE e.recipe_program_run_id = r.recipe_program_run_id AND e.created_at >= :cutoff"
            ")",
        ),
    ]
    total = 0
    for table_name, statement in statements:
        if not _safe_table_exists(table_name):
            continue
        total += int(db.session.execute(text(statement), {"cutoff": cutoff}).scalar() or 0)
    return total


def _delete_batch(cutoff: datetime, batch_size: int) -> int:
    # Delete children first for databases that do not enforce ON DELETE CASCADE
    # on older manually-created schemas.
    statements = [
        (
            "control_command_event",
            "DELETE FROM control_command_event WHERE created_at < :cutoff "
            "ORDER BY created_at LIMIT :batch_size",
        ),
        (
            "control_command",
            "DELETE FROM control_command WHERE requested_at < :cutoff "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM control_command_event e "
            "  WHERE e.command_id = control_command.command_id AND e.created_at >= :cutoff"
            ") "
            "ORDER BY requested_at LIMIT :batch_size",
        ),
        (
            "recipe_program_event",
            "DELETE FROM recipe_program_event WHERE created_at < :cutoff "
            "ORDER BY created_at LIMIT :batch_size",
        ),
        (
            "recipe_program_run",
            "DELETE FROM recipe_program_run "
            "WHERE COALESCE(finished_at, started_at) < :cutoff AND status <> 'running' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM recipe_program_event e "
            "  WHERE e.recipe_program_run_id = recipe_program_run.recipe_program_run_id AND e.created_at >= :cutoff"
            ") "
            "ORDER BY COALESCE(finished_at, started_at) LIMIT :batch_size",
        ),
    ]
    deleted = 0
    for table_name, statement in statements:
        if not _safe_table_exists(table_name):
            continue
        deleted += int(
            db.session.execute(
                text(statement),
                {"cutoff": cutoff, "batch_size": batch_size},
            ).rowcount
            or 0
        )
    return deleted


def run_activity_log_retention(app: Flask) -> ActivityLogRetentionResult:
    enabled: bool = bool(app.config.get("ACTIVITY_LOG_RETENTION_ENABLED", True))
    days: int = max(1, int(app.config.get("ACTIVITY_LOG_RETENTION_DAYS", 7)))
    batch_size: int = max(1, int(app.config.get("ACTIVITY_LOG_RETENTION_BATCH_SIZE", 5000)))
    max_batches: int = max(1, int(app.config.get("ACTIVITY_LOG_RETENTION_MAX_BATCHES_PER_RUN", 20)))
    dry_run: bool = bool(app.config.get("ACTIVITY_LOG_RETENTION_DRY_RUN", False))

    cutoff = activity_log_cutoff(days)
    result = ActivityLogRetentionResult(cutoff=cutoff, dry_run=dry_run)
    t0 = time.monotonic()

    if not enabled:
        app.logger.info(
            "activity_log_retention: disabled (set ACTIVITY_LOG_RETENTION_ENABLED=true to enable)"
        )
        result.elapsed_seconds = time.monotonic() - t0
        return result

    try:
        if dry_run:
            result.rows_deleted = _count_old_rows(cutoff)
        else:
            for batch_num in range(1, max_batches + 1):
                deleted = _delete_batch(cutoff, batch_size)
                db.session.commit()
                result.rows_deleted += deleted
                result.batches_run = batch_num
                if deleted < batch_size:
                    break
            else:
                result.stopped_early = True
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        result.error = str(exc)
        app.logger.exception("activity_log_retention failed")

    result.elapsed_seconds = time.monotonic() - t0
    app.logger.info(result.as_log_line())
    return result
