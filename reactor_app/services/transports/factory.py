"""TransportFactory — creates the right ITransport for a given connection.

Currently only ``tcp_socket`` is fully implemented.  Future transport types
(serial, USB, Modbus TCP, OPC-UA, REST, ADC, virtual) are listed as named
placeholders so their type strings are reserved and documented.

Usage::

    transport = build_transport(connection, payload)
    with transport:
        result = driver.execute(transport=transport, request=request)
"""
from __future__ import annotations

from typing import Any

from .interface import ITransport, TransportTypeNotSupportedError
from .tcp_socket import TcpSocketConfig, TcpSocketTransport


# Transport type strings that are reserved for future implementation.
# Raising a clear error for these prevents silent fall-through to an
# "unknown type" message and documents the planned roadmap.
_FUTURE_TRANSPORT_TYPES: frozenset[str] = frozenset(
    {"serial", "usb", "modbus_tcp", "opcua", "rest", "analog_digital_io", "virtual"}
)

_SUPPORTED_TRANSPORT_TYPES: frozenset[str] = frozenset({"tcp_socket"})


def _parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_tcp_config(connection: Any, payload: dict[str, Any]) -> TcpSocketConfig:
    """Build a ``TcpSocketConfig`` from a ``DeviceConnection`` ORM object and
    the command payload.  Payload overrides take precedence over connection
    defaults so that individual commands can fine-tune timeouts."""
    read_timeout_ms = _parse_optional_int(payload.get("response_timeout_ms"))
    write_timeout_ms = _parse_optional_int(payload.get("write_timeout_ms"))
    connect_timeout_ms = _parse_optional_int(payload.get("connect_timeout_ms"))
    recv_size = _parse_optional_int(payload.get("recv_size"))

    connect_timeout_s = (
        connect_timeout_ms or max(connection.read_timeout_ms, connection.write_timeout_ms, 3000)
    ) / 1000

    return TcpSocketConfig(
        host=connection.tcp_host,
        port=connection.tcp_port,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=(read_timeout_ms or connection.read_timeout_ms) / 1000,
        write_timeout_s=(write_timeout_ms or connection.write_timeout_ms) / 1000,
        recv_size=recv_size or 4096,
    )


def build_transport(connection: Any, payload: dict[str, Any]) -> ITransport:
    """Return a ready-to-use ``ITransport`` for *connection*.

    Only ``tcp_socket`` connections are supported today.  Any other type raises
    ``TransportTypeNotSupportedError`` — the caller is responsible for
    converting that into an appropriate application-level error.

    Args:
        connection: A ``DeviceConnection`` ORM object (or any object with the
            same attributes).  Accepted duck-typed to avoid a model import here.
        payload:    The command payload dict; may override transport timeouts
                    via ``response_timeout_ms``, ``write_timeout_ms``,
                    ``connect_timeout_ms``, and ``recv_size``.
    """
    transport_type = str(connection.transport_type or "tcp_socket").strip().lower()

    if transport_type == "tcp_socket":
        return TcpSocketTransport(_build_tcp_config(connection, payload))

    if transport_type in _FUTURE_TRANSPORT_TYPES:
        raise TransportTypeNotSupportedError(
            f"Transport type '{transport_type}' is planned for a future release. "
            "Only 'tcp_socket' is currently available."
        )

    raise TransportTypeNotSupportedError(
        f"Unknown transport type '{transport_type}'. "
        f"Supported: {', '.join(sorted(_SUPPORTED_TRANSPORT_TYPES))}."
    )
