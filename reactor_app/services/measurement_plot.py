from __future__ import annotations

import copy
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, bindparam, or_, text
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models import Measurement

_LIVE_PLOT_CACHE_LOCK = threading.Lock()
_LIVE_PLOT_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_MAX_LIVE_PLOT_CACHE_ITEMS = 16


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
    return {
        channel_code: {
            "channel_code": channel_code,
            "unit": None,
            "latest_measurement_at": None,
            "items": [],
        }
        for channel_code in channel_codes
    }


def _empty_batched_series(specs: list[tuple[int, str]]) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (device_id, channel_code): {
            "device_id": device_id,
            "channel_code": channel_code,
            "unit": None,
            "latest_measurement_at": None,
            "items": [],
        }
        for device_id, channel_code in specs
    }


def _series_has_points(series_by_code: dict[str, dict[str, Any]]) -> bool:
    return any(bool((series or {}).get("items")) for series in series_by_code.values())


def _batched_series_has_points(series_by_key: dict[tuple[int, str], dict[str, Any]]) -> bool:
    return any(bool((series or {}).get("items")) for series in series_by_key.values())


def _append_series_item(bucket: dict[str, dict[str, Any]], *, channel_code: str, measured_at: datetime, numeric_value: float, unit: str | None) -> None:
    normalized_measured_at = _as_utc_datetime(measured_at)
    if normalized_measured_at is None:
        return
    series = bucket.setdefault(
        channel_code,
        {
            "channel_code": channel_code,
            "unit": unit,
            "latest_measurement_at": None,
            "items": [],
        },
    )
    if series["unit"] in (None, "") and unit not in (None, ""):
        series["unit"] = unit
    series["latest_measurement_at"] = normalized_measured_at.isoformat()
    series["items"].append(
        {
            "measured_at": normalized_measured_at.isoformat(),
            "numeric_value": float(numeric_value),
            "unit": unit,
        }
    )


def _append_batched_series_item(
    bucket: dict[tuple[int, str], dict[str, Any]],
    *,
    device_id: int,
    channel_code: str,
    measured_at: datetime,
    numeric_value: float,
    unit: str | None,
) -> None:
    normalized_measured_at = _as_utc_datetime(measured_at)
    if normalized_measured_at is None:
        return
    key = (int(device_id), channel_code)
    series = bucket.setdefault(
        key,
        {
            "device_id": int(device_id),
            "channel_code": channel_code,
            "unit": unit,
            "latest_measurement_at": None,
            "items": [],
        },
    )
    if series["unit"] in (None, "") and unit not in (None, ""):
        series["unit"] = unit
    series["latest_measurement_at"] = normalized_measured_at.isoformat()
    series["items"].append(
        {
            "measured_at": normalized_measured_at.isoformat(),
            "numeric_value": float(numeric_value),
            "unit": unit,
        }
    )


def _load_plot_series_mysql(
    *,
    device_id: int,
    channel_codes: list[str],
    window_start: datetime,
    window_end: datetime,
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
              AND measured_at <= :window_end
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
            "cutoff": window_start,
            "window_end": window_end,
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


def _series_spec_where_sql(specs: list[tuple[int, str]]) -> tuple[str, dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {}
    for index, (device_id, channel_code) in enumerate(specs):
        device_param = f"device_id_{index}"
        channel_param = f"channel_code_{index}"
        clauses.append(f"(device_id = :{device_param} AND channel_code = :{channel_param})")
        params[device_param] = int(device_id)
        params[channel_param] = channel_code
    return " OR ".join(clauses) or "1 = 0", params


def _load_batched_plot_series_mysql(
    *,
    specs: list[tuple[int, str]],
    window_start: datetime,
    window_end: datetime,
    bucket_seconds: int,
) -> dict[tuple[int, str], dict[str, Any]]:
    spec_where_sql, spec_params = _series_spec_where_sql(specs)
    sql = text(
        f"""
        SELECT device_id, channel_code, measured_at, numeric_value, unit
        FROM (
            SELECT
                device_id,
                channel_code,
                measured_at,
                numeric_value,
                unit,
                ROW_NUMBER() OVER (
                    PARTITION BY device_id, channel_code, FLOOR(TIMESTAMPDIFF(SECOND, :window_start, measured_at) / :bucket_seconds)
                    ORDER BY measured_at DESC, measurement_id DESC
                ) AS bucket_rank
            FROM measurement
            WHERE ({spec_where_sql})
              AND numeric_value IS NOT NULL
              AND measured_at >= :window_start
              AND measured_at <= :window_end
        ) ranked
        WHERE bucket_rank = 1
        ORDER BY device_id ASC, channel_code ASC, measured_at ASC
        """
    )

    rows = db.session.execute(
        sql,
        {
            **spec_params,
            "window_start": window_start,
            "window_end": window_end,
            "bucket_seconds": bucket_seconds,
        },
    ).mappings()

    bucket = _empty_batched_series(specs)
    for row in rows:
        _append_batched_series_item(
            bucket,
            device_id=int(row["device_id"]),
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
    window_start: datetime,
    window_end: datetime,
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
            Measurement.measured_at >= window_start,
            Measurement.measured_at <= window_end,
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
        bucket_index = max(0, int((measured_at - window_start).total_seconds()) // bucket_seconds)
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


def _load_batched_plot_series_python(
    *,
    specs: list[tuple[int, str]],
    window_start: datetime,
    window_end: datetime,
    bucket_seconds: int,
) -> dict[tuple[int, str], dict[str, Any]]:
    bucket = _empty_batched_series(specs)
    allowed_keys = set(specs)
    if not allowed_keys:
        return bucket

    conditions = [
        and_(Measurement.device_id == device_id, Measurement.channel_code == channel_code)
        for device_id, channel_code in specs
    ]
    query = (
        Measurement.query.with_entities(
            Measurement.device_id,
            Measurement.channel_code,
            Measurement.measured_at,
            Measurement.numeric_value,
            Measurement.unit,
            Measurement.measurement_id,
        )
        .filter(
            or_(*conditions),
            Measurement.numeric_value.is_not(None),
            Measurement.measured_at >= window_start,
            Measurement.measured_at <= window_end,
        )
        .order_by(
            Measurement.device_id.asc(),
            Measurement.channel_code.asc(),
            Measurement.measured_at.asc(),
            Measurement.measurement_id.asc(),
        )
    )

    current_key: tuple[int, str] | None = None
    current_bucket_index: int | None = None
    latest_row: tuple[int, str, datetime, float, str | None] | None = None

    for row in query.yield_per(1000):
        device_id = int(row.device_id)
        channel_code = str(row.channel_code)
        key = (device_id, channel_code)
        if key not in allowed_keys:
            continue
        measured_at = _as_utc_datetime(row.measured_at)
        if measured_at is None:
            continue
        bucket_index = max(0, int((measured_at - window_start).total_seconds()) // bucket_seconds)
        row_payload = (device_id, channel_code, measured_at, float(row.numeric_value), row.unit)

        if current_key is None:
            current_key = key
            current_bucket_index = bucket_index
            latest_row = row_payload
            continue

        if key != current_key or bucket_index != current_bucket_index:
            if latest_row is not None:
                _append_batched_series_item(
                    bucket,
                    device_id=latest_row[0],
                    channel_code=latest_row[1],
                    measured_at=latest_row[2],
                    numeric_value=latest_row[3],
                    unit=latest_row[4],
                )
            current_key = key
            current_bucket_index = bucket_index

        latest_row = row_payload

    if latest_row is not None:
        _append_batched_series_item(
            bucket,
            device_id=latest_row[0],
            channel_code=latest_row[1],
            measured_at=latest_row[2],
            numeric_value=latest_row[3],
            unit=latest_row[4],
        )

    return bucket


def _load_last_known_batched_mysql(
    *,
    specs: list[tuple[int, str]],
) -> dict[tuple[int, str], dict[str, Any]]:
    """One query: the single most-recent measurement per spec, no time restriction."""
    spec_where_sql, spec_params = _series_spec_where_sql(specs)
    sql = text(
        f"""
        SELECT device_id, channel_code, measured_at, numeric_value, unit
        FROM (
            SELECT
                device_id,
                channel_code,
                measured_at,
                numeric_value,
                unit,
                ROW_NUMBER() OVER (
                    PARTITION BY device_id, channel_code
                    ORDER BY measured_at DESC, measurement_id DESC
                ) AS rn
            FROM measurement
            WHERE ({spec_where_sql})
              AND numeric_value IS NOT NULL
        ) ranked
        WHERE rn = 1
        """
    )
    rows = db.session.execute(sql, spec_params).mappings()
    bucket: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        _append_batched_series_item(
            bucket,
            device_id=int(row["device_id"]),
            channel_code=str(row["channel_code"]),
            measured_at=row["measured_at"],
            numeric_value=float(row["numeric_value"]),
            unit=row["unit"],
        )
    return bucket


def _load_last_known_batched_python(
    *,
    specs: list[tuple[int, str]],
) -> dict[tuple[int, str], dict[str, Any]]:
    """Get the most recent measurement per spec using a streaming query, no time restriction."""
    bucket: dict[tuple[int, str], dict[str, Any]] = {}
    allowed_keys = set(specs)
    if not allowed_keys:
        return bucket

    conditions = [
        and_(Measurement.device_id == device_id, Measurement.channel_code == channel_code)
        for device_id, channel_code in specs
    ]
    query = (
        Measurement.query.with_entities(
            Measurement.device_id,
            Measurement.channel_code,
            Measurement.measured_at,
            Measurement.numeric_value,
            Measurement.unit,
            Measurement.measurement_id,
        )
        .filter(or_(*conditions), Measurement.numeric_value.is_not(None))
        .order_by(
            Measurement.device_id.asc(),
            Measurement.channel_code.asc(),
            Measurement.measured_at.desc(),
            Measurement.measurement_id.desc(),
        )
    )

    seen_keys: set[tuple[int, str]] = set()
    for row in query.yield_per(200):
        key = (int(row.device_id), str(row.channel_code))
        if key not in allowed_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        measured_at = _as_utc_datetime(row.measured_at)
        if measured_at is None:
            continue
        _append_batched_series_item(
            bucket,
            device_id=key[0],
            channel_code=key[1],
            measured_at=measured_at,
            numeric_value=float(row.numeric_value),
            unit=row.unit,
        )
        if len(seen_keys) >= len(allowed_keys):
            break

    return bucket


def _normalize_channel_codes(channel_codes: list[str]) -> list[str]:
    normalized_codes = []
    seen_codes: set[str] = set()
    for channel_code in channel_codes:
        normalized = str(channel_code or "").strip()
        if normalized and normalized not in seen_codes:
            normalized_codes.append(normalized)
            seen_codes.add(normalized)
    return normalized_codes


def _normalize_series_specs(series_specs: list[dict[str, Any] | tuple[Any, Any]]) -> list[tuple[int, str]]:
    normalized_specs: list[tuple[int, str]] = []
    seen_specs: set[tuple[int, str]] = set()
    for item in series_specs:
        if isinstance(item, dict):
            raw_device_id = item.get("device_id")
            raw_channel_code = item.get("channel_code")
        elif isinstance(item, tuple) and len(item) >= 2:
            raw_device_id, raw_channel_code = item[0], item[1]
        else:
            continue
        try:
            device_id = int(raw_device_id)
        except (TypeError, ValueError):
            continue
        channel_code = str(raw_channel_code or "").strip()
        if device_id <= 0 or not channel_code:
            continue
        key = (device_id, channel_code)
        if key in seen_specs:
            continue
        normalized_specs.append(key)
        seen_specs.add(key)
    return normalized_specs


def _plot_window(*, since_minutes: int, window_end: datetime | None) -> tuple[datetime, datetime]:
    normalized_window_end = _as_utc_datetime(window_end) or _now_utc()
    normalized_window_start = normalized_window_end - timedelta(minutes=max(1, since_minutes))
    return normalized_window_start, normalized_window_end


def _cache_aligned_window_end(*, cache_seconds: float, window_end: datetime | None) -> datetime | None:
    if window_end is not None or cache_seconds <= 0:
        return window_end
    now = _now_utc()
    quantum = max(0.1, float(cache_seconds))
    aligned_timestamp = math.floor(now.timestamp() / quantum) * quantum
    return datetime.fromtimestamp(aligned_timestamp, tz=timezone.utc)


def _live_plot_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _LIVE_PLOT_CACHE_LOCK:
        cached = _LIVE_PLOT_CACHE.get(key)
        if cached is None:
            return None
        return copy.deepcopy(cached)


def _live_plot_cache_set(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    with _LIVE_PLOT_CACHE_LOCK:
        if len(_LIVE_PLOT_CACHE) >= _MAX_LIVE_PLOT_CACHE_ITEMS:
            oldest_key = next(iter(_LIVE_PLOT_CACHE))
            _LIVE_PLOT_CACHE.pop(oldest_key, None)
        _LIVE_PLOT_CACHE[key] = copy.deepcopy(payload)


def load_device_plot_series_window(
    *,
    device_id: int,
    channel_codes: list[str],
    since_minutes: int,
    max_points: int,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    normalized_codes = _normalize_channel_codes(channel_codes)

    if not normalized_codes:
        start, end = _plot_window(since_minutes=since_minutes, window_end=window_end)
        return {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "bucket_seconds": _bucket_seconds(since_minutes=since_minutes, max_points=max_points),
            "series": [],
        }

    window_start, normalized_window_end = _plot_window(since_minutes=since_minutes, window_end=window_end)
    bucket_seconds = _bucket_seconds(since_minutes=since_minutes, max_points=max_points)
    dialect_name = _db_dialect_name()

    try:
        if dialect_name == "mysql":
            series_by_code = _load_plot_series_mysql(
                device_id=device_id,
                channel_codes=normalized_codes,
                window_start=window_start,
                window_end=normalized_window_end,
                bucket_seconds=bucket_seconds,
            )
            if not _series_has_points(series_by_code):
                # Some MySQL/MariaDB installations behave differently on the
                # optimized window-function path. Re-run the portable query
                # before reporting "no data" to the UI.
                series_by_code = _load_plot_series_python(
                    device_id=device_id,
                    channel_codes=normalized_codes,
                    window_start=window_start,
                    window_end=normalized_window_end,
                    bucket_seconds=bucket_seconds,
                )
        else:
            series_by_code = _load_plot_series_python(
                device_id=device_id,
                channel_codes=normalized_codes,
                window_start=window_start,
                window_end=normalized_window_end,
                bucket_seconds=bucket_seconds,
            )
    except SQLAlchemyError:
        # Fall back to the portable streaming implementation if the optimized
        # SQL path fails on a specific database version.
        db.session.rollback()
        series_by_code = _load_plot_series_python(
            device_id=device_id,
            channel_codes=normalized_codes,
            window_start=window_start,
            window_end=normalized_window_end,
            bucket_seconds=bucket_seconds,
        )

    return {
        "window_start": window_start.isoformat(),
        "window_end": normalized_window_end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "series": [
            series_by_code.get(
                channel_code,
                {
                    "channel_code": channel_code,
                    "unit": None,
                    "latest_measurement_at": None,
                    "items": [],
                },
            )
            for channel_code in normalized_codes
        ],
    }


def load_batched_device_plot_series_window(
    *,
    series_specs: list[dict[str, Any] | tuple[Any, Any]],
    since_minutes: int,
    max_points: int,
    window_end: datetime | None = None,
    cache_seconds: float = 0,
) -> dict[str, Any]:
    normalized_specs = _normalize_series_specs(series_specs)
    effective_window_end = _cache_aligned_window_end(cache_seconds=cache_seconds, window_end=window_end)
    window_start, normalized_window_end = _plot_window(since_minutes=since_minutes, window_end=effective_window_end)
    bucket_seconds = _bucket_seconds(since_minutes=since_minutes, max_points=max_points)

    if not normalized_specs:
        return {
            "window_start": window_start.isoformat(),
            "window_end": normalized_window_end.isoformat(),
            "bucket_seconds": bucket_seconds,
            "cache_hit": False,
            "series": [],
        }

    cache_key: tuple[Any, ...] | None = None
    if cache_seconds > 0:
        cache_key = (
            "batched_plot",
            tuple(normalized_specs),
            int(max(1, since_minutes)),
            int(max(1, max_points)),
            normalized_window_end.isoformat(),
        )
        cached = _live_plot_cache_get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

    dialect_name = _db_dialect_name()
    try:
        if dialect_name == "mysql":
            series_by_key = _load_batched_plot_series_mysql(
                specs=normalized_specs,
                window_start=window_start,
                window_end=normalized_window_end,
                bucket_seconds=bucket_seconds,
            )
            if not _batched_series_has_points(series_by_key):
                series_by_key = _load_batched_plot_series_python(
                    specs=normalized_specs,
                    window_start=window_start,
                    window_end=normalized_window_end,
                    bucket_seconds=bucket_seconds,
                )
        else:
            series_by_key = _load_batched_plot_series_python(
                specs=normalized_specs,
                window_start=window_start,
                window_end=normalized_window_end,
                bucket_seconds=bucket_seconds,
            )
    except SQLAlchemyError:
        db.session.rollback()
        series_by_key = _load_batched_plot_series_python(
            specs=normalized_specs,
            window_start=window_start,
            window_end=normalized_window_end,
            bucket_seconds=bucket_seconds,
        )

    # Fallback: for specs with no data in the time window, show the most recent
    # available measurement regardless of age so the last-known state is always
    # visible even when polling was paused or the selected range has no data.
    empty_specs = [
        key for key in normalized_specs
        if not series_by_key.get(key, {}).get("items")
    ]
    if empty_specs:
        try:
            if dialect_name == "mysql":
                last_known = _load_last_known_batched_mysql(specs=empty_specs)
            else:
                last_known = _load_last_known_batched_python(specs=empty_specs)
            for key, series in last_known.items():
                series_by_key[key] = series
        except SQLAlchemyError:
            db.session.rollback()

    payload = {
        "window_start": window_start.isoformat(),
        "window_end": normalized_window_end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "cache_hit": False,
        "series": [
            series_by_key.get(
                key,
                {
                    "device_id": key[0],
                    "channel_code": key[1],
                    "unit": None,
                    "latest_measurement_at": None,
                    "items": [],
                },
            )
            for key in normalized_specs
        ],
    }
    if cache_key is not None:
        _live_plot_cache_set(cache_key, payload)
    return payload


def load_device_plot_series(
    *,
    device_id: int,
    channel_codes: list[str],
    since_minutes: int,
    max_points: int,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    return load_device_plot_series_window(
        device_id=device_id,
        channel_codes=channel_codes,
        since_minutes=since_minutes,
        max_points=max_points,
        window_end=window_end,
    )["series"]
