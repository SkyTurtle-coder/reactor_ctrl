#!/usr/bin/env python
"""One-shot measurement retention runner.

Deletes measurement rows older than MEASUREMENT_RETENTION_DAYS (default: 30)
from the reactor_ctrl database.  Designed to be invoked by a systemd timer;
all configuration is read from the .env file or environment variables.

Usage:
    .venv/bin/python run_measurement_retention.py

Key settings (in .env or environment):
    MEASUREMENT_RETENTION_ENABLED=true      # must be set to actually delete
    MEASUREMENT_RETENTION_DAYS=30
    MEASUREMENT_RETENTION_BATCH_SIZE=10000
    MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN=50
    MEASUREMENT_RETENTION_DRY_RUN=false     # set to true for a safe trial run

Exit codes:
    0 — completed without error (including disabled / dry-run cases)
    1 — an exception occurred during the retention run
"""

from __future__ import annotations

import os
import sys

# Disable long-running background reconcilers: this process is one-shot and
# exits immediately after the retention run.
os.environ.setdefault("DEVICE_MANUAL_RECONCILER_ENABLED", "false")
os.environ.setdefault("RECIPE_PROGRAM_RECONCILER_ENABLED", "false")

from reactor_app import create_app
from reactor_app.services.measurement_retention import run_retention


def main() -> int:
    app = create_app()
    with app.app_context():
        result = run_retention(app)
    return 1 if result.error else 0


if __name__ == "__main__":
    sys.exit(main())
