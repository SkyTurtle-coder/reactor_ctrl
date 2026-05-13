from __future__ import annotations

import secrets
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


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://reactor_user:change-me@127.0.0.1:3306/reactor_ctrl",
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": _env_int("DB_POOL_RECYCLE_SECONDS", 1800),
        "pool_timeout": _env_int("DB_POOL_TIMEOUT_SECONDS", 30),
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = _env_bool("FLASK_DEBUG", False)
    AUTO_CREATE_SCHEMA = _env_bool("AUTO_CREATE_SCHEMA", True)
    API_AUTH_REQUIRED = _env_bool("API_AUTH_REQUIRED", True)
    API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN")
    API_AUTH_REALM = os.getenv("API_AUTH_REALM", "reactor_ctrl")
    BUILDER_WRITE_TOKEN_TTL_SECONDS = _env_int("BUILDER_WRITE_TOKEN_TTL_SECONDS", 43200)
    RECIPE_WRITE_TOKEN_TTL_SECONDS = _env_int(
        "RECIPE_WRITE_TOKEN_TTL_SECONDS",
        BUILDER_WRITE_TOKEN_TTL_SECONDS,
    )
    PROCESS_MANUAL_WRITE_TOKEN_TTL_SECONDS = _env_int(
        "PROCESS_MANUAL_WRITE_TOKEN_TTL_SECONDS",
        BUILDER_WRITE_TOKEN_TTL_SECONDS,
    )
    DEVICE_MANUAL_RECONCILER_ENABLED = _env_bool("DEVICE_MANUAL_RECONCILER_ENABLED", True)
    DEVICE_MANUAL_RECONCILER_LOOP_MS = _env_int("DEVICE_MANUAL_RECONCILER_LOOP_MS", 500)
    DEVICE_MANUAL_RECONCILER_POLL_MS = _env_int("DEVICE_MANUAL_RECONCILER_POLL_MS", 2500)
    DEVICE_MANUAL_RECONCILER_WATCH_TTL_SECONDS = _env_int("DEVICE_MANUAL_RECONCILER_WATCH_TTL_SECONDS", 30)
    DEVICE_MANUAL_RECONCILER_LEASE_SECONDS = _env_int("DEVICE_MANUAL_RECONCILER_LEASE_SECONDS", 15)
    RECIPE_PROGRAM_RECONCILER_ENABLED = _env_bool("RECIPE_PROGRAM_RECONCILER_ENABLED", True)
    RECIPE_PROGRAM_RECONCILER_LOOP_MS = _env_int("RECIPE_PROGRAM_RECONCILER_LOOP_MS", 1000)
    RECIPE_PROGRAM_RECONCILER_LEASE_SECONDS = _env_int("RECIPE_PROGRAM_RECONCILER_LEASE_SECONDS", 10)
    APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "")
    SESSION_LIFETIME_HOURS = _env_int("SESSION_LIFETIME_HOURS", 8)
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    MEASUREMENT_POLLER_INTERVAL_SECONDS = _env_int("MEASUREMENT_POLLER_INTERVAL_SECONDS", 30)
    MEASUREMENT_RETENTION_ENABLED = _env_bool("MEASUREMENT_RETENTION_ENABLED", False)
    MEASUREMENT_RETENTION_DAYS = _env_int("MEASUREMENT_RETENTION_DAYS", 30)
    MEASUREMENT_RETENTION_BATCH_SIZE = _env_int("MEASUREMENT_RETENTION_BATCH_SIZE", 10_000)
    MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN = _env_int("MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN", 50)
    MEASUREMENT_RETENTION_DRY_RUN = _env_bool("MEASUREMENT_RETENTION_DRY_RUN", False)
    HUBER_CC230_DEFAULT_PORT = _env_int("HUBER_CC230_DEFAULT_PORT", 4001)
    HUBER_CC230_LINE_ENDING = os.getenv("HUBER_CC230_LINE_ENDING", "crlf")
    HUBER_CC230_MAX_RETRIES = _env_int("HUBER_CC230_MAX_RETRIES", 2)
    HUBER_CC230_MIN_SETPOINT_C = _env_float("HUBER_CC230_MIN_SETPOINT_C", -50.0)
    HUBER_CC230_MAX_SETPOINT_C = _env_float("HUBER_CC230_MAX_SETPOINT_C", 200.0)
    ACTIVITY_LOG_RETENTION_ENABLED = _env_bool("ACTIVITY_LOG_RETENTION_ENABLED", True)
    ACTIVITY_LOG_RETENTION_DAYS = _env_int("ACTIVITY_LOG_RETENTION_DAYS", 7)
    ACTIVITY_LOG_RETENTION_BATCH_SIZE = _env_int("ACTIVITY_LOG_RETENTION_BATCH_SIZE", 5_000)
    ACTIVITY_LOG_RETENTION_MAX_BATCHES_PER_RUN = _env_int("ACTIVITY_LOG_RETENTION_MAX_BATCHES_PER_RUN", 20)
    ACTIVITY_LOG_RETENTION_DRY_RUN = _env_bool("ACTIVITY_LOG_RETENTION_DRY_RUN", False)

    @staticmethod
    def generate_api_auth_token() -> str:
        return secrets.token_urlsafe(32)
