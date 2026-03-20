from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_flowsheet_library(*, static_folder: str | None, static_url_path: str) -> list[dict[str, Any]]:
    if not static_folder:
        return []

    try:
        manifest_file = Path(static_folder) / "flowsheet" / "library" / "manifest.json"
        if not manifest_file.exists():
            return []
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list):
        return []

    normalized_symbols: list[dict[str, Any]] = []
    for item in symbols:
        if not isinstance(item, dict):
            continue

        svg_file = str(item.get("svg_file", "")).strip()
        symbol_id = str(item.get("id", "")).strip()
        if not svg_file or not symbol_id:
            continue

        ports = item.get("ports", [])
        normalized = dict(item)
        normalized["id"] = symbol_id
        normalized["svg_url"] = f"{static_url_path}/flowsheet/library/{svg_file}"
        normalized["port_count"] = len(ports) if isinstance(ports, list) else 0
        normalized_symbols.append(normalized)

    return normalized_symbols


def build_symbol_index(symbols: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(symbol.get("id") or "").strip(): symbol for symbol in symbols if str(symbol.get("id") or "").strip()}


def format_library_category(value: str | None) -> str:
    normalized = str(value or "").strip().replace("-", " ").replace("_", " ")
    if not normalized:
        return "Uncategorized"
    return " ".join(part.capitalize() for part in normalized.split())


def group_flowsheet_library(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        category_id = str(symbol.get("category") or "").strip() or "uncategorized"
        group = groups.setdefault(
            category_id,
            {
                "id": category_id,
                "label": format_library_category(category_id),
                "symbols": [],
            },
        )
        group["symbols"].append(symbol)

    ordered_groups = sorted(groups.values(), key=lambda item: str(item["label"]).lower())
    for group in ordered_groups:
        group["symbols"] = sorted(
            group["symbols"],
            key=lambda item: (
                str(item.get("label") or "").lower(),
                str(item.get("id") or "").lower(),
            ),
        )
    return ordered_groups
