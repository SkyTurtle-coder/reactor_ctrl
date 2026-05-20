from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _quote_identifier(name: str, *, dialect_name: str) -> str:
    if dialect_name == "mysql":
        return f"`{name.replace('`', '``')}`"
    return f'"{name.replace(chr(34), chr(34) + chr(34))}"'


def _drop_existing_database_objects(app, db) -> tuple[list[str], list[str]]:
    dialect_name = db.engine.dialect.name
    dropped_views: list[str] = []
    dropped_tables: list[str] = []

    with db.engine.begin() as connection:
        if dialect_name == "mysql":
            connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))

        inspector = inspect(connection)
        views = sorted(inspector.get_view_names())
        tables = sorted(inspector.get_table_names())

        for view_name in views:
            connection.execute(text(f"DROP VIEW IF EXISTS {_quote_identifier(view_name, dialect_name=dialect_name)}"))
            dropped_views.append(view_name)

        for table_name in tables:
            connection.execute(text(f"DROP TABLE IF EXISTS {_quote_identifier(table_name, dialect_name=dialect_name)}"))
            dropped_tables.append(table_name)

        if dialect_name == "mysql":
            connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    app.logger.info(
        "Dropped %s table(s) and %s view(s) before database reset.",
        len(dropped_tables),
        len(dropped_views),
    )
    return dropped_tables, dropped_views


def reset_database() -> tuple[list[str], list[str]]:
    os.environ.setdefault("DEVICE_MANUAL_RECONCILER_ENABLED", "0")
    os.environ.setdefault("RECIPE_PROGRAM_RECONCILER_ENABLED", "0")
    os.environ.setdefault("AUTO_CREATE_SCHEMA", "1")

    from reactor_app import (
        _ACTIVITY_LOG_INDEX_SPECS,
        _LATEST_MEASUREMENT_VIEW_SQL,
        _MEASUREMENT_INDEX_SPECS,
        _ensure_named_indexes,
        create_app,
    )
    from reactor_app.extensions import db

    app = create_app()
    with app.app_context():
        db.session.remove()
        dropped_tables, dropped_views = _drop_existing_database_objects(app, db)
        db.create_all()
        _ensure_named_indexes(app, _MEASUREMENT_INDEX_SPECS, label="measurement")
        _ensure_named_indexes(app, _ACTIVITY_LOG_INDEX_SPECS, label="activity log")
        db.session.execute(text(_LATEST_MEASUREMENT_VIEW_SQL))
        db.session.commit()
        db.session.remove()
    return dropped_tables, dropped_views


def main() -> int:
    parser = argparse.ArgumentParser(description="Drop all reactor_ctrl database data and recreate an empty schema.")
    parser.add_argument(
        "--yes-delete-all-data",
        action="store_true",
        help="Confirm that every table and view in the configured database may be deleted.",
    )
    args = parser.parse_args()

    if not args.yes_delete_all_data:
        parser.error("Refusing to reset the database without --yes-delete-all-data.")

    dropped_tables, dropped_views = reset_database()
    print(f"Database reset complete. Dropped {len(dropped_tables)} table(s) and {len(dropped_views)} view(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
