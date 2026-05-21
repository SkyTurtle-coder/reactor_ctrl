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

    def __post_init__(self) -> None:
        if not str(self.host or "").strip():
            raise ValueError("TcpSocketConfig.host must not be empty.")
        try:
            port = int(self.port)
            connect_timeout = float(self.connect_timeout_s)
            read_timeout = float(self.read_timeout_s)
            write_timeout = float(self.write_timeout_s)
            recv_size = int(self.recv_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("TcpSocketConfig contains invalid numeric values.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("TcpSocketConfig.port must be between 1 and 65535.")
        if connect_timeout <= 0:
            raise ValueError("TcpSocketConfig.connect_timeout_s must be > 0.")
        if read_timeout <= 0:
            raise ValueError("TcpSocketConfig.read_timeout_s must be > 0.")
        if write_timeout <= 0:
            raise ValueError("TcpSocketConfig.write_timeout_s must be > 0.")
        if recv_size <= 0:
            raise ValueError("TcpSocketConfig.recv_size must be > 0.")


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
        if not delimiter:
            raise ValueError("TcpSocketTransport.receive_until requires a non-empty delimiter.")
        if max_bytes <= 0:
            raise ValueError("TcpSocketTransport.receive_until requires max_bytes > 0.")

        buffer = bytearray()
        chunk_size = min(self.config.recv_size, max_bytes)
        while len(buffer) < max_bytes:
            chunk = self._sock.recv(chunk_size)
            if not chunk:
                raise socket.timeout("Connection closed before the expected response terminator was received.")
            buffer.extend(chunk)
            if delimiter in buffer:
                return bytes(buffer)
            chunk_size = min(self.config.recv_size, max_bytes - len(buffer))
            if chunk_size <= 0:
                break
        raise socket.timeout("Response terminator was not received before the maximum response size was reached.")

    def drain_input(self, *, max_bytes: int = 65536, idle_timeout_s: float = 0.02) -> bytes:
        """Best-effort drain of stale bytes already waiting on the socket."""
        self.connect()
        assert self._sock is not None
        if max_bytes <= 0:
            return b""

        previous_timeout = self.config.read_timeout_s
        drained = bytearray()
        self._sock.settimeout(max(0.001, float(idle_timeout_s)))
        try:
            while len(drained) < max_bytes:
                chunk = self._sock.recv(min(self.config.recv_size, max_bytes - len(drained)))
                if not chunk:
                    break
                drained.extend(chunk)
        except socket.timeout:
            pass
        finally:
            self._sock.settimeout(previous_timeout)
        return bytes(drained)

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
