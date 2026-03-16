from __future__ import annotations

import socket
from dataclasses import dataclass
from time import perf_counter


@dataclass(frozen=True)
class TcpSocketConfig:
    host: str
    port: int
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 1.2
    write_timeout_s: float = 1.2
    recv_size: int = 4096


@dataclass(frozen=True)
class TcpSocketProbeResult:
    reachable: bool
    latency_ms: float | None
    error: str | None = None


class TcpSocketTransport:
    def __init__(self, config: TcpSocketConfig):
        self.config = config
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        if self._sock is not None:
            return

        sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=self.config.connect_timeout_s,
        )
        sock.settimeout(self.config.read_timeout_s)
        self._sock = sock

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        finally:
            self._sock = None

    def send(self, payload: bytes) -> None:
        self.connect()
        assert self._sock is not None

        self._sock.settimeout(self.config.write_timeout_s)
        self._sock.sendall(payload)
        self._sock.settimeout(self.config.read_timeout_s)

    def receive(self, recv_size: int | None = None) -> bytes:
        self.connect()
        assert self._sock is not None
        return self._sock.recv(recv_size or self.config.recv_size)

    def receive_until(self, delimiter: bytes, *, max_bytes: int = 65536) -> bytes:
        self.connect()
        assert self._sock is not None

        buffer = bytearray()
        chunk_size = min(self.config.recv_size, max_bytes)
        while len(buffer) < max_bytes:
            chunk = self._sock.recv(chunk_size)
            if not chunk:
                break
            buffer.extend(chunk)
            if delimiter in buffer:
                break
            chunk_size = min(self.config.recv_size, max_bytes - len(buffer))
            if chunk_size <= 0:
                break
        return bytes(buffer)

    def send_and_receive(self, payload: bytes, recv_size: int | None = None) -> bytes:
        self.send(payload)
        return self.receive(recv_size=recv_size)

    def __enter__(self) -> "TcpSocketTransport":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def probe_tcp_socket(config: TcpSocketConfig) -> TcpSocketProbeResult:
    started_at = perf_counter()
    transport = TcpSocketTransport(config)

    try:
        transport.connect()
    except OSError as exc:
        return TcpSocketProbeResult(reachable=False, latency_ms=None, error=str(exc))
    finally:
        transport.close()

    latency_ms = round((perf_counter() - started_at) * 1000, 3)
    return TcpSocketProbeResult(reachable=True, latency_ms=latency_ms)
