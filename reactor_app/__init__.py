from __future__ import annotations

from pathlib import Path

from flask import Flask
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from werkzeug.exceptions import HTTPException

from .api import api_bp
from .extensions import db
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


def _initialize_database_schema(app: Flask) -> None:
    if not app.config.get("AUTO_CREATE_SCHEMA", True):
        return

    try:
        db.create_all()
        _archive_legacy_binding_tables(app)
        db.create_all()
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

    db.init_app(app)

    with app.app_context():
        from . import models  # noqa: F401
        _initialize_database_schema(app)

    _register_request_cleanup(app)
    _register_error_handlers(app)
    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    return app
