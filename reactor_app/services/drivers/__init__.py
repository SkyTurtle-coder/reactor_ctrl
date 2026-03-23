from __future__ import annotations

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverNotFoundError, DriverValidationError
from .generic_text import GenericTextDriver
from .ika_eurostar import IkaEurostarDriver


_DRIVER_TYPES = (GenericTextDriver, IkaEurostarDriver)
_PROTOCOL_LABELS = {
    "ascii_text": "ASCII Text",
    "generic_text": "Generic Text",
    "ika_eurostar_60": "IKA 60",
    "line_text": "Line Text",
    "rs232_text": "RS-232 Text",
}


def get_driver(protocol_name: str) -> DeviceDriver:
    normalized = str(protocol_name).strip().lower()
    for driver_type in _DRIVER_TYPES:
        if normalized in driver_type.protocol_names:
            return driver_type()
    supported = ", ".join(sorted(list_supported_protocols()))
    raise DriverNotFoundError(f"Protocol '{protocol_name}' is not supported. Supported protocols: {supported}.")


def list_supported_protocols() -> list[str]:
    protocols: set[str] = set()
    for driver_type in _DRIVER_TYPES:
        protocols.update(driver_type.protocol_names)
    return sorted(protocols)


def protocol_label(protocol_name: str | None) -> str:
    normalized = str(protocol_name or "").strip().lower()
    if not normalized:
        return ""
    return _PROTOCOL_LABELS.get(normalized, str(protocol_name).strip())


def list_supported_protocol_options() -> list[dict[str, str]]:
    return [{"id": protocol_id, "label": protocol_label(protocol_id)} for protocol_id in list_supported_protocols()]


__all__ = [
    "DeviceCommandRequest",
    "DeviceCommandResult",
    "DeviceDriver",
    "DriverError",
    "DriverNotFoundError",
    "DriverValidationError",
    "GenericTextDriver",
    "IkaEurostarDriver",
    "get_driver",
    "list_supported_protocol_options",
    "list_supported_protocols",
    "protocol_label",
]
