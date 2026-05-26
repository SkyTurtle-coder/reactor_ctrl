"""Transport abstraction layer.

``ITransport`` is a structural Protocol that all transport implementations must
satisfy.  Existing code that only uses ``TcpSocketTransport`` continues to work
unchanged because the protocol is satisfied structurally (no inheritance needed).

Future transport types (serial, USB, Modbus TCP, OPC-UA, REST, ADC/virtual)
should implement this protocol so that the driver layer and runtime never need
to care which physical medium is in use.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..cancellation import CancellationToken


class TransportTypeNotSupportedError(ValueError):
    """Raised by TransportFactory when the requested transport type has no implementation."""


@runtime_checkable
class ITransport(Protocol):
    """Minimal interface that every device transport must satisfy.

    ``recv_size`` is a hint for drivers that need a default upper bound on how
    many bytes to read per response.  For TCP transports this maps to
    ``TcpSocketConfig.recv_size``; other implementations should supply a
    sensible default (4096 is a safe choice).
    """

    recv_size: int

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Open the underlying connection.  Idempotent: calling on an already
        connected transport must not raise."""
        ...

    def close(self) -> None:
        """Close the underlying connection.  Idempotent."""
        ...

    def is_connected(self) -> bool:
        """Return True when the transport has an active connection."""
        ...

    def bind_runtime_control(self, *, cancellation_token: CancellationToken | None = None) -> None:
        """Attach the runtime cancellation/deadline context for subsequent I/O."""
        ...

    # ------------------------------------------------------------------ #
    # I/O primitives                                                       #
    # ------------------------------------------------------------------ #

    def send(self, payload: bytes) -> None:
        """Send ``payload`` to the device, connecting first if necessary."""
        ...

    def receive(self, recv_size: int | None = None) -> bytes:
        """Read up to ``recv_size`` bytes (default: ``self.recv_size``)."""
        ...

    def receive_until(self, delimiter: bytes, *, max_bytes: int = 65536) -> bytes:
        """Read until ``delimiter`` is found or ``max_bytes`` are buffered."""
        ...

    def send_and_receive(self, payload: bytes, recv_size: int | None = None) -> bytes:
        """Convenience: send ``payload`` then read a single response chunk."""
        ...

    def get_remaining_timeout(self, *, phase: str = "read", default_s: float | None = None) -> float | None:
        """Return the effective timeout for the requested I/O phase."""
        ...

    # ------------------------------------------------------------------ #
    # Context manager                                                      #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "ITransport":
        ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        ...
