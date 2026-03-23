from __future__ import annotations

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverNotFoundError, DriverValidationError
from .generic_text import GenericTextDriver
from .ika_eurostar import IkaEurostarDriver


_DRIVER_TYPES = (GenericTextDriver, IkaEurostarDriver)


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
    "list_supported_protocols",
]
