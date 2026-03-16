from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://reactor_user:change-me@127.0.0.1:3306/reactor_ctrl",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = _env_bool("FLASK_DEBUG", False)
    AUTO_CREATE_SCHEMA = _env_bool("AUTO_CREATE_SCHEMA", True)
