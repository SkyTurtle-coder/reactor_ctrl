from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models import Measurement


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bucket_seconds(*, since_minutes: int, max_points: int) -> int:
    return max(1, math.ceil((since_minutes * 60) / max(1, max_points)))


def _db_dialect_name() -> str:
    return db.session.bind.dialect.name if db.session.bind is not None else ""


def _empty_series(channel_codes: list[str]) -> dict[str, dict[str, Any]]:
    return {channel_code: {"channel_code": channel_code, "unit": None, "items": []} for channel_code in channel_codes}


def _series_has_points(series_by_code: dict[str, dict[str, Any]]) -> bool:
    return any(bool((series or {}).get("items")) for series in series_by_code.values())


def _append_series_item(bucket: dict[str, dict[str, Any]], *, channel_code: str, measured_at: datetime, numeric_value: float, unit: str | None) -> None:
    series = bucket.setdefault(channel_code, {"channel_code": channel_code, "unit": unit, "items": []})
    if series["unit"] in (None, "") and unit not in (None, ""):
        series["unit"] = unit
    series["items"].append(
        {
            "measured_at": _as_utc_datetime(measured_at).isoformat(),
            "numeric_value": float(numeric_value),
            "unit": unit,
        }
    )


def _load_plot_series_mysql(
    *,
    device_id: int,
    channel_codes: list[str],
    cutoff: datetime,
    bucket_seconds: int,
) -> dict[str, dict[str, Any]]:
    sql = text(
        """
        SELECT channel_code, measured_at, numeric_value, unit
        FROM (
            SELECT
                channel_code,
                measured_at,
                numeric_value,
                unit,
                ROW_NUMBER() OVER (
                    PARTITION BY channel_code, FLOOR(TIMESTAMPDIFF(SECOND, :cutoff, measured_at) / :bucket_seconds)
                    ORDER BY measured_at DESC, measurement_id DESC
                ) AS bucket_rank
            FROM measurement
            WHERE device_id = :device_id
              AND channel_code IN :channel_codes
              AND numeric_value IS NOT NULL
              AND measured_at >= :cutoff
        ) ranked
        WHERE bucket_rank = 1
        ORDER BY channel_code ASC, measured_at ASC
        """
    ).bindparams(bindparam("channel_codes", expanding=True))

    rows = db.session.execute(
        sql,
        {
            "device_id": device_id,
            "channel_codes": channel_codes,
            "cutoff": cutoff,
            "bucket_seconds": bucket_seconds,
        },
    ).mappings()

    bucket = _empty_series(channel_codes)
    for row in rows:
        _append_series_item(
            bucket,
            channel_code=str(row["channel_code"]),
            measured_at=row["measured_at"],
            numeric_value=float(row["numeric_value"]),
            unit=row["unit"],
        )
    return bucket


def _load_plot_series_python(
    *,
    device_id: int,
    channel_codes: list[str],
    cutoff: datetime,
    bucket_seconds: int,
) -> dict[str, dict[str, Any]]:
    bucket = _empty_series(channel_codes)
    query = (
        Measurement.query.with_entities(
            Measurement.channel_code,
            Measurement.measured_at,
            Measurement.numeric_value,
            Measurement.unit,
            Measurement.measurement_id,
        )
        .filter(
            Measurement.device_id == device_id,
            Measurement.channel_code.in_(channel_codes),
            Measurement.numeric_value.is_not(None),
            Measurement.measured_at >= cutoff,
        )
        .order_by(
            Measurement.channel_code.asc(),
            Measurement.measured_at.asc(),
            Measurement.measurement_id.asc(),
        )
    )

    current_channel: str | None = None
    current_bucket_index: int | None = None
    latest_row: tuple[str, datetime, float, str | None] | None = None

    for row in query.yield_per(1000):
        channel_code = str(row.channel_code)
        measured_at = _as_utc_datetime(row.measured_at)
        if measured_at is None:
            continue
        bucket_index = max(0, int((measured_at - cutoff).total_seconds()) // bucket_seconds)
        row_payload = (channel_code, measured_at, float(row.numeric_value), row.unit)

        if current_channel is None:
            current_channel = channel_code
            current_bucket_index = bucket_index
            latest_row = row_payload
            continue

        if channel_code != current_channel or bucket_index != current_bucket_index:
            if latest_row is not None:
                _append_series_item(
                    bucket,
                    channel_code=latest_row[0],
                    measured_at=latest_row[1],
                    numeric_value=latest_row[2],
                    unit=latest_row[3],
                )
            current_channel = channel_code
            current_bucket_index = bucket_index

        latest_row = row_payload

    if latest_row is not None:
        _append_series_item(
            bucket,
            channel_code=latest_row[0],
            measured_at=latest_row[1],
            numeric_value=latest_row[2],
            unit=latest_row[3],
        )

    return bucket


def load_device_plot_series(
    *,
    device_id: int,
    channel_codes: list[str],
    since_minutes: int,
    max_points: int,
) -> list[dict[str, Any]]:
    normalized_codes = []
    seen_codes: set[str] = set()
    for channel_code in channel_codes:
        normalized = str(channel_code or "").strip()
        if normalized and normalized not in seen_codes:
            normalized_codes.append(normalized)
            seen_codes.add(normalized)

    if not normalized_codes:
        return []

    cutoff = _now_utc() - timedelta(minutes=max(1, since_minutes))
    bucket_seconds = _bucket_seconds(since_minutes=since_minutes, max_points=max_points)
    dialect_name = _db_dialect_name()

    try:
        if dialect_name == "mysql":
            series_by_code = _load_plot_series_mysql(
                device_id=device_id,
                channel_codes=normalized_codes,
                cutoff=cutoff,
                bucket_seconds=bucket_seconds,
            )
            if not _series_has_points(series_by_code):
                # Some MySQL/MariaDB installations behave differently on the
                # optimized window-function path. Re-run the portable query
                # before reporting "no data" to the UI.
                series_by_code = _load_plot_series_python(
                    device_id=device_id,
                    channel_codes=normalized_codes,
                    cutoff=cutoff,
                    bucket_seconds=bucket_seconds,
                )
        else:
            series_by_code = _load_plot_series_python(
                device_id=device_id,
                channel_codes=normalized_codes,
                cutoff=cutoff,
                bucket_seconds=bucket_seconds,
            )
    except SQLAlchemyError:
        # Fall back to the portable streaming implementation if the optimized
        # SQL path fails on a specific database version.
        db.session.rollback()
        series_by_code = _load_plot_series_python(
            device_id=device_id,
            channel_codes=normalized_codes,
            cutoff=cutoff,
            bucket_seconds=bucket_seconds,
        )

    return [series_by_code.get(channel_code, {"channel_code": channel_code, "unit": None, "items": []}) for channel_code in normalized_codes]
