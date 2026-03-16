from __future__ import annotations

from pathlib import Path

from flask import Flask

from .extensions import db
from .web import web_bp


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

    app.register_blueprint(web_bp)

    return app
