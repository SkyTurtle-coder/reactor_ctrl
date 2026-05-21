from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from flask import Flask
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from werkzeug.exceptions import HTTPException

from .api import api_bp
from .auth import auth_bp, require_login
from .extensions import db
from .services import start_device_manual_reconciler, start_recipe_program_reconciler
from .web import web_bp


_LATEST_MEASUREMENT_VIEW_SQL = """
CREATE OR REPLACE VIEW v_latest_measurement_per_channel AS
SELECT m.*
FROM measurement m
JOIN (
  SELECT device_id, channel_code, MAX(measured_at) AS max_measured_at
  FROM measurement
  GROUP BY device_id, channel_code
) x
  ON x.device_id = m.device_id
 AND x.channel_code = m.channel_code
 AND x.max_measured_at = m.measured_at
"""

_LEGACY_BINDING_TABLES = (
    {
        "table_name": "device_binding_current",
        "required_column": "connection_id",
        "backup_prefix": "device_binding_current_legacy_rs485",
    },
    {
        "table_name": "device_binding_history",
        "required_column": "connection_id",
        "backup_prefix": "device_binding_history_legacy_rs485",
    },
)

_MEASUREMENT_INDEX_SPECS = (
    (
        "measurement",
        "ix_measurement_device_channel_measured_at",
        "CREATE INDEX ix_measurement_device_channel_measured_at "
        "ON measurement (device_id, channel_code, measured_at)",
    ),
    (
        "measurement",
        "ix_measurement_device_measured_at",
        "CREATE INDEX ix_measurement_device_measured_at "
        "ON measurement (device_id, measured_at)",
    ),
)

# Optional columns added after initial schema release.
# Each entry is (table_name, column_name, column_definition).
# The app adds missing columns automatically on startup so manual SQL
# migrations are not required for in-place upgrades.
_OPTIONAL_COLUMN_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "device_connection",
        "cc230_setpoint_write_mode",
        "SMALLINT NULL",
    ),
)

_ACTIVITY_LOG_INDEX_SPECS = (
    (
        "control_command",
        "ix_control_command_requested_at",
        "CREATE INDEX ix_control_command_requested_at ON control_command (requested_at)",
    ),
    (
        "control_command_event",
        "ix_control_command_event_created_at",
        "CREATE INDEX ix_control_command_event_created_at ON control_command_event (created_at)",
    ),
    (
        "recipe_program_run",
        "ix_recipe_program_run_finished_started",
        "CREATE INDEX ix_recipe_program_run_finished_started ON recipe_program_run (finished_at, started_at)",
    ),
)


def _error_code(exc: SQLAlchemyError) -> int | None:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def _is_schema_mismatch_error(exc: SQLAlchemyError) -> bool:
    return _error_code(exc) in {1054, 1146}


def _next_backup_table_name(existing_tables: set[str], prefix: str) -> str:
    candidate = prefix
    suffix = 1
    while candidate in existing_tables:
        suffix += 1
        candidate = f"{prefix}_{suffix}"
    existing_tables.add(candidate)
    return candidate


def _archive_legacy_binding_tables(app: Flask) -> None:
    if db.engine.dialect.name != "mysql":
        return

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    archived_tables: list[tuple[str, str, list[str]]] = []

    for spec in _LEGACY_BINDING_TABLES:
        table_name = spec["table_name"]
        if table_name not in existing_tables:
            continue

        columns = sorted(column["name"] for column in inspector.get_columns(table_name))
        if spec["required_column"] in columns:
            continue

        backup_table_name = _next_backup_table_name(existing_tables, spec["backup_prefix"])
        db.session.execute(text(f"CREATE TABLE `{backup_table_name}` AS SELECT * FROM `{table_name}`"))
        db.session.execute(text(f"DROP TABLE `{table_name}`"))
        existing_tables.discard(table_name)
        archived_tables.append((table_name, backup_table_name, columns))

    if archived_tables:
        db.session.commit()
        for table_name, backup_table_name, columns in archived_tables:
            app.logger.warning(
                "Archived legacy table %s to %s before recreating the current schema. Columns were: %s",
                table_name,
                backup_table_name,
                ", ".join(columns),
            )


def _ensure_named_indexes(app: Flask, specs: tuple[tuple[str, str, str], ...], *, label: str) -> None:
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    indexes_by_table: dict[str, set[str]] = {}
    created_indexes: list[str] = []

    for table_name, index_name, create_sql in specs:
        if table_name not in existing_tables:
            continue
        existing_indexes = indexes_by_table.get(table_name)
        if existing_indexes is None:
            existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            indexes_by_table[table_name] = existing_indexes
        if index_name in existing_indexes:
            continue
        try:
            db.session.execute(text(create_sql))
            db.session.commit()
            created_indexes.append(index_name)
            existing_indexes.add(index_name)
        except SQLAlchemyError as exc:
            db.session.rollback()
            duplicate_index_name = _error_code(exc) == 1061
            duplicate_index_text = "already exists" in str(exc).lower()
            if duplicate_index_name or duplicate_index_text:
                existing_indexes.add(index_name)
                continue
            raise

    if created_indexes:
        app.logger.info("Created %s index(es): %s", label, ", ".join(created_indexes))


def _ensure_optional_columns(app: Flask) -> None:
    """Add optional schema columns that may not exist in older deployments.

    Called on every startup so in-place upgrades work without running a
    manual migration script.  Idempotent: skips columns that already exist.
    """
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table_name, column_name, column_def in _OPTIONAL_COLUMN_SPECS:
        if table_name not in existing_tables:
            continue
        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception:
            continue
        if column_name in existing_columns:
            continue
        try:
            db.session.execute(
                text(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_def}")
            )
            db.session.commit()
            added.append(f"{table_name}.{column_name}")
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.warning(
                "Auto-migration: could not add column %s.%s — run migrate_v7_cc230_setpoint_write_mode.sql manually.",
                table_name,
                column_name,
                exc_info=True,
            )

    if added:
        app.logger.info("Auto-migration: added column(s): %s.", ", ".join(added))


def _ensure_measurement_indexes(app: Flask) -> None:
    _ensure_named_indexes(app, _MEASUREMENT_INDEX_SPECS, label="measurement")


def _ensure_activity_log_indexes(app: Flask) -> None:
    _ensure_named_indexes(app, _ACTIVITY_LOG_INDEX_SPECS, label="activity log")


def _initialize_database_schema(app: Flask) -> None:
    if not app.config.get("AUTO_CREATE_SCHEMA", True):
        return

    try:
        db.create_all()
        _archive_legacy_binding_tables(app)
        db.create_all()
        _ensure_optional_columns(app)
        _ensure_measurement_indexes(app)
        _ensure_activity_log_indexes(app)
        db.session.execute(text(_LATEST_MEASUREMENT_VIEW_SQL))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            app.logger.exception("Database session rollback after schema initialization failure also failed.")
        app.logger.exception("Automatic database schema initialization failed.")


def _register_error_handlers(app: Flask) -> None:
    def rollback_session() -> None:
        try:
            db.session.rollback()
        except Exception:
            app.logger.exception("Database session rollback failed while handling an application error.")

    def handle_database_error(exc: SQLAlchemyError):
        if app.debug:
            raise exc

        from flask import jsonify, request

        rollback_session()
        if _is_schema_mismatch_error(exc):
            message = (
                "Database schema is missing or outdated. Restart the app with AUTO_CREATE_SCHEMA enabled "
                "or apply the current schema from sql/mysql_schema_v1.sql."
            )
            app.logger.exception(message)
            if request.path.startswith("/api/"):
                return jsonify({"error": message}), 503
            return message, 503

        app.logger.exception("Unhandled database error.")
        if request.path.startswith("/api/"):
            return jsonify({"error": "Database query failed."}), 500
        return "Database query failed.", 500

    def handle_unexpected_error(exc: Exception):
        if app.debug:
            raise exc
        if isinstance(exc, HTTPException):
            return exc

        from flask import jsonify, request

        rollback_session()
        app.logger.exception("Unhandled application error.")
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unexpected server error."}), 500
        return "Unexpected server error.", 500

    app.register_error_handler(ProgrammingError, handle_database_error)
    app.register_error_handler(OperationalError, handle_database_error)
    app.register_error_handler(SQLAlchemyError, handle_database_error)
    app.register_error_handler(Exception, handle_unexpected_error)


def _register_request_cleanup(app: Flask) -> None:
    @app.teardown_request
    def rollback_failed_request(exc):
        if exc is None:
            return None
        try:
            db.session.rollback()
        except Exception:
            app.logger.exception("Database session rollback failed during request teardown.")
        return None


def create_app() -> Flask:
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
    app.config.from_object("config.Config")

    # Session security settings
    session_hours = app.config.get("SESSION_LIFETIME_HOURS", 8)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=session_hours)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if app.config.get("SESSION_COOKIE_SECURE", False):
        app.config["SESSION_COOKIE_SECURE"] = True

    db.init_app(app)

    with app.app_context():
        from . import models  # noqa: F401
        _initialize_database_schema(app)

    _register_request_cleanup(app)
    _register_error_handlers(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)
    app.before_request(require_login)
    start_device_manual_reconciler(app)
    start_recipe_program_reconciler(app)

    return app
