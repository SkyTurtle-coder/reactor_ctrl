from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..transports import TcpSocketTransport


@dataclass(frozen=True)
class DeviceCommandRequest:
    command_name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DeviceCommandResult:
    acknowledged: bool
    response_text: str | None = None
    response_hex: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class DriverError(ValueError):
    pass


class DriverValidationError(DriverError):
    pass


class DriverNotFoundError(DriverError):
    pass


class DeviceDriver(ABC):
    protocol_names: tuple[str, ...] = ()
    uses_transport: bool = True

    @abstractmethod
    def execute(self, *, transport: TcpSocketTransport | None, request: DeviceCommandRequest) -> DeviceCommandResult:
        raise NotImplementedError
