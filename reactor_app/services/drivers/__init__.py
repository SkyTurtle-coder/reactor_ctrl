from __future__ import annotations

from .base import DeviceCommandRequest, DeviceCommandResult, DeviceDriver, DriverError, DriverNotFoundError, DriverValidationError
from .capabilities import DeviceCapability
from .generic_text import GenericTextDriver
from .huber_cc230 import HuberCC230Driver
from .huber_unistat import HuberUnistatDriver, HuberUnistatTCP
from .ika_eurostar import IkaEurostarDriver
from .mettler_toledo_ics435 import MettlerToledoICS435Driver


_DRIVER_TYPES = (HuberUnistatDriver, HuberCC230Driver, IkaEurostarDriver, MettlerToledoICS435Driver)

# Protocols available in the UI selection list.
_PROTOCOL_LABELS = {
    "huber_cc230": "Huber/Polystat CC230",
    "huber_unistat_430": "Huber Unistat 430",
    "ika_eurostar_60": "IKA 60",
    "mettler_toledo_ics435": "Mettler Toledo ICS435",
}


def get_driver(protocol_name: str) -> DeviceDriver:
    normalized = str(protocol_name).strip().lower()
    for driver_type in _DRIVER_TYPES:
        if normalized in driver_type.protocol_names:
            return driver_type()
    supported = ", ".join(sorted(_PROTOCOL_LABELS.keys()))
    raise DriverNotFoundError(f"Protocol '{protocol_name}' is not supported. Supported protocols: {supported}.")


def list_supported_protocols() -> list[str]:
    return sorted(_PROTOCOL_LABELS.keys())


def protocol_label(protocol_name: str | None) -> str:
    normalized = str(protocol_name or "").strip().lower()
    if not normalized:
        return ""
    return _PROTOCOL_LABELS.get(normalized, str(protocol_name).strip())


def list_supported_protocol_options() -> list[dict[str, str]]:
    return [{"id": protocol_id, "label": _PROTOCOL_LABELS[protocol_id]} for protocol_id in sorted(_PROTOCOL_LABELS.keys())]


__all__ = [
    "DeviceCapability",
    "DeviceCommandRequest",
    "DeviceCommandResult",
    "DeviceDriver",
    "DriverError",
    "DriverNotFoundError",
    "DriverValidationError",
    "GenericTextDriver",
    "HuberCC230Driver",
    "HuberUnistatDriver",
    "HuberUnistatTCP",
    "IkaEurostarDriver",
    "MettlerToledoICS435Driver",
    "get_driver",
    "list_supported_protocol_options",
    "list_supported_protocols",
    "protocol_label",
]
