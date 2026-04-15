#!/usr/bin/env python
"""One-shot activity-log retention runner.

Deletes operational log rows older than ACTIVITY_LOG_RETENTION_DAYS (default:
7) from command and recipe-program history tables. Designed for a systemd
timer; configuration is read from .env or environment variables.
"""

from __future__ import annotations

import os
import sys

# This process only cleans historical logs and must not start background
# reconciler threads.
os.environ.setdefault("DEVICE_MANUAL_RECONCILER_ENABLED", "false")
os.environ.setdefault("RECIPE_PROGRAM_RECONCILER_ENABLED", "false")

from reactor_app import create_app
from reactor_app.services.activity_log_retention import run_activity_log_retention


def main() -> int:
    app = create_app()
    with app.app_context():
        result = run_activity_log_retention(app)
    return 1 if result.error else 0


if __name__ == "__main__":
    sys.exit(main())
