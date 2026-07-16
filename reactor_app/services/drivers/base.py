from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..cancellation import CancellationToken
from ..transports.interface import ITransport


@dataclass(frozen=True)
class DeviceCommandRequest:
    command_name: str
    payload: dict[str, Any]
    cancellation_token: CancellationToken | None = None

    def time_remaining(self) -> float | None:
        if self.cancellation_token is None:
            return None
        return self.cancellation_token.time_remaining()

    def throw_if_interrupted(self, *, location: str | None = None) -> None:
        if self.cancellation_token is None:
            return
        self.cancellation_token.throw_if_interrupted(location=location)


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
    persistent_transport: bool = False

    @abstractmethod
    def execute(self, *, transport: ITransport | None, request: DeviceCommandRequest) -> DeviceCommandResult:
        raise NotImplementedError

    def get_capabilities(self) -> frozenset[str]:
        """Return the set of capability strings this driver supports.

        Override in concrete drivers.  The default is an empty frozenset,
        which means the runtime must fall back to protocol-name checks (the
        old behaviour) until a driver is updated.
        """
        return frozenset()
