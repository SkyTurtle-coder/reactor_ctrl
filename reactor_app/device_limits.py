from __future__ import annotations

from typing import Any


IKA_EUROSTAR_60_MAX_RPM = 2000


def normalized_protocol_name(value: Any) -> str:
    return str(value or "").strip().lower()


def max_rpm_for_protocol(protocol: Any, *, default: int | None = None) -> int | None:
    if normalized_protocol_name(protocol) == "ika_eurostar_60":
        return IKA_EUROSTAR_60_MAX_RPM
    return default
