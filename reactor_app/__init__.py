from __future__ import annotations

from pathlib import Path

from flask import Flask
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

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


def _is_schema_mismatch_error(exc: ProgrammingError) -> bool:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    if not args:
        return False
    return args[0] in {1054, 1146}


def _initialize_database_schema(app: Flask) -> None:
    if not app.config.get("AUTO_CREATE_SCHEMA", True):
        return

    try:
        db.create_all()
        db.session.execute(text(_LATEST_MEASUREMENT_VIEW_SQL))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Automatic database schema initialization failed.")


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ProgrammingError)
    def handle_programming_error(exc: ProgrammingError):
        if app.debug:
            raise exc

        from flask import jsonify, request

        if _is_schema_mismatch_error(exc):
            message = (
                "Database schema is missing or outdated. Restart the app with AUTO_CREATE_SCHEMA enabled "
                "or apply the current schema from sql/mysql_schema_v1.sql."
            )
            app.logger.exception(message)
            if request.path.startswith("/api/"):
                return jsonify({"error": message}), 503
            return message, 503

        app.logger.exception("Unhandled database programming error.")
        if request.path.startswith("/api/"):
            return jsonify({"error": "Database query failed."}), 500
        return "Database query failed.", 500


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

    _register_error_handlers(app)
    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    return app
