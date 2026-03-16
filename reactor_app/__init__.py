from __future__ import annotations

from flask import Flask

from .extensions import db
from .web import web_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object("config.Config")

    db.init_app(app)

    with app.app_context():
        from . import models  # noqa: F401

    app.register_blueprint(web_bp)

    return app
