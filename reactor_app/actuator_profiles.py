from __future__ import annotations

from copy import deepcopy
from typing import Any


ACTUATOR_PROFILES: dict[str, dict[str, Any]] = {
    "motor_rpm": {
        "id": "motor_rpm",
        "label": "Motor",
        "allowed_symbols": {"motor"},
        "fields": [
            {
                "key": "is_on",
                "label": "Status",
                "type": "boolean",
                "default": False,
            },
            {
                "key": "speed",
                "label": "Drehzahl",
                "type": "number",
                "mode": "int",
                "unit": "rpm",
                "min": 0,
                "max": 10000,
                "step": 10,
                "default": 0,
            },
        ],
        "command_sequence": [
            {"kind": "choice", "field": "is_on", "true": "START", "false": "STOP"},
            {"kind": "template", "template": "RPM={speed}"},
        ],
    },
    "hc_system_temperature": {
        "id": "hc_system_temperature",
        "label": "H/C System",
        "allowed_symbols": {"hc_system"},
        "fields": [
            {
                "key": "is_on",
                "label": "Status",
                "type": "boolean",
                "default": False,
            },
            {
                "key": "target_temp",
                "label": "Soll-Temperatur",
                "type": "number",
                "mode": "float",
                "unit": "C",
                "min": 0,
                "max": 180,
                "step": 0.5,
                "default": 25,
            },
            {
                "key": "ramp",
                "label": "Ramp",
                "type": "number",
                "mode": "float",
                "unit": "C/min",
                "min": 0,
                "max": 50,
                "step": 0.5,
                "default": 1,
            },
        ],
        "command_sequence": [
            {"kind": "choice", "field": "is_on", "true": "START", "false": "STOP"},
            {"kind": "template", "template": "TEMP={target_temp}"},
            {"kind": "template", "template": "RAMP={ramp}"},
        ],
    },
    "pump_rpm": {
        "id": "pump_rpm",
        "label": "Pumpe",
        "allowed_symbols": {"pump"},
        "fields": [
            {
                "key": "speed",
                "label": "Drehzahl",
                "type": "number",
                "mode": "int",
                "unit": "rpm",
                "min": 0,
                "max": 10000,
                "step": 10,
                "default": 0,
            },
        ],
        "command_sequence": [
            {"kind": "template", "template": "RPM={speed}"},
        ],
    },
}

DEFAULT_PROFILE_BY_SYMBOL = {
    "motor": "motor_rpm",
    "hc_system": "hc_system_temperature",
    "pump": "pump_rpm",
}


def list_actuator_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for profile in ACTUATOR_PROFILES.values():
        item = deepcopy(profile)
        item["allowed_symbols"] = sorted(str(symbol).strip() for symbol in profile.get("allowed_symbols", set()))
        profiles.append(item)
    return profiles


def get_actuator_profile(profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    profile = ACTUATOR_PROFILES.get(str(profile_id).strip())
    if profile is None:
        return None
    item = deepcopy(profile)
    item["allowed_symbols"] = sorted(str(symbol).strip() for symbol in profile.get("allowed_symbols", set()))
    return item


def get_default_profile_id(symbol_id: str | None) -> str | None:
    if not symbol_id:
        return None
    return DEFAULT_PROFILE_BY_SYMBOL.get(str(symbol_id).strip())


def default_control_for_symbol(symbol_id: str | None) -> dict[str, Any] | None:
    profile_id = get_default_profile_id(symbol_id)
    profile = ACTUATOR_PROFILES.get(profile_id or "")
    if profile is None:
        return None
    return {
        "profile_id": profile["id"],
        "config": {
            field["key"]: field.get("default")
            for field in profile.get("fields", [])
            if isinstance(field, dict) and field.get("key")
        },
    }


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "ein"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "aus"}:
            return False
    raise ValueError(f"Field '{field_name}' must be a boolean.")


def _coerce_number(value: Any, *, field_name: str, mode: str, minimum: float | None, maximum: float | None) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Field '{field_name}' must be numeric.") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"Field '{field_name}' must be >= {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"Field '{field_name}' must be <= {maximum}.")

    if mode == "int":
        rounded = int(round(parsed))
        if minimum is not None and rounded < minimum:
            raise ValueError(f"Field '{field_name}' must be >= {minimum}.")
        if maximum is not None and rounded > maximum:
            raise ValueError(f"Field '{field_name}' must be <= {maximum}.")
        return rounded
    return round(parsed, 4)


def normalize_control_definition(symbol_id: str | None, value: Any) -> dict[str, Any] | None:
    default_control = default_control_for_symbol(symbol_id)
    if default_control is None:
        return None

    payload = value if isinstance(value, dict) else {}
    profile_id = str(payload.get("profile_id") or default_control["profile_id"]).strip()
    profile = ACTUATOR_PROFILES.get(profile_id)
    if profile is None:
        raise ValueError(f"Unknown actuator control profile '{profile_id}'.")

    allowed_symbols = {str(item).strip() for item in profile.get("allowed_symbols", set())}
    if symbol_id is None or str(symbol_id).strip() not in allowed_symbols:
        raise ValueError(f"Actuator control profile '{profile_id}' is not valid for symbol '{symbol_id}'.")

    config_value = payload.get("config", {})
    if config_value in (None, ""):
        config_value = {}
    if not isinstance(config_value, dict):
        raise ValueError("Field 'definition_json.nodes[].control.config' must be an object.")

    normalized_config: dict[str, Any] = {}
    for field in profile.get("fields", []):
        key = str(field.get("key") or "").strip()
        if not key:
            continue

        field_name = f"definition_json.nodes[].control.config.{key}"
        raw_value = config_value.get(key, field.get("default"))
        field_type = str(field.get("type") or "").strip()
        if field_type == "boolean":
            normalized_config[key] = _coerce_bool(raw_value, field_name=field_name)
            continue
        if field_type == "number":
            normalized_config[key] = _coerce_number(
                raw_value,
                field_name=field_name,
                mode=str(field.get("mode") or "float").strip(),
                minimum=field.get("min"),
                maximum=field.get("max"),
            )
            continue
        raise ValueError(f"Unsupported actuator field type '{field_type}' in profile '{profile_id}'.")

    return {
        "profile_id": profile_id,
        "config": normalized_config,
    }
