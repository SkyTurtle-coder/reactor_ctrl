from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from ..cancellation import CancellationToken


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
        self._cancellation_token: CancellationToken | None = None
        self._cooperative_poll_interval_s = 0.25

    @property
    def recv_size(self) -> int:
        return self.config.recv_size

    def is_connected(self) -> bool:
        return self._sock is not None

    def bind_runtime_control(self, *, cancellation_token: CancellationToken | None = None) -> None:
        self._cancellation_token = cancellation_token

    def get_remaining_timeout(self, *, phase: str = "read", default_s: float | None = None) -> float | None:
        timeout_s = default_s
        if timeout_s is None:
            phase_key = str(phase or "read").strip().lower()
            if phase_key == "connect":
                timeout_s = self.config.connect_timeout_s
            elif phase_key == "write":
                timeout_s = self.config.write_timeout_s
            else:
                timeout_s = self.config.read_timeout_s

        if self._cancellation_token is None:
            return timeout_s

        deadline_remaining = self._cancellation_token.time_remaining(now=datetime.now(timezone.utc))
        if deadline_remaining is None:
            return timeout_s
        if timeout_s is None:
            return max(0.001, deadline_remaining)
        return max(0.001, min(timeout_s, deadline_remaining))

    def _throw_if_interrupted(self, *, location: str) -> None:
        if self._cancellation_token is None:
            return
        self._cancellation_token.throw_if_interrupted(location=location)

    def _operation_deadline(self, *, phase: str, default_s: float) -> float:
        effective_timeout_s = self.get_remaining_timeout(phase=phase, default_s=default_s) or default_s
        return perf_counter() + max(0.001, float(effective_timeout_s))

    def _step_timeout(self, operation_deadline: float) -> float:
        remaining = operation_deadline - perf_counter()
        if remaining <= 0:
            return 0.0
        return max(0.001, min(remaining, self._cooperative_poll_interval_s))

    def connect(self) -> None:
        if self._sock is not None:
            return

        self._throw_if_interrupted(location="transport.connect")
        connect_timeout_s = self.get_remaining_timeout(phase="connect", default_s=self.config.connect_timeout_s)
        sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=connect_timeout_s,
        )
        try:
            sock.settimeout(self.get_remaining_timeout(phase="read", default_s=self.config.read_timeout_s))
            self._sock = sock
            self._throw_if_interrupted(location="transport.connect")
        except Exception:
            sock.close()
            self._sock = None
            raise

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
        if self._cancellation_token is None:
            self._sock.settimeout(self.config.write_timeout_s)
            self._sock.sendall(payload)
            self._sock.settimeout(self.config.read_timeout_s)
            return

        self._throw_if_interrupted(location="transport.send")
        operation_deadline = self._operation_deadline(phase="write", default_s=self.config.write_timeout_s)
        view = memoryview(payload)
        bytes_sent = 0
        while bytes_sent < len(view):
            self._throw_if_interrupted(location="transport.send")
            timeout_s = self._step_timeout(operation_deadline)
            if timeout_s <= 0:
                self._throw_if_interrupted(location="transport.send")
                raise socket.timeout("Timed out while sending device request bytes.")
            self._sock.settimeout(timeout_s)
            try:
                sent = self._sock.send(view[bytes_sent:])
            except socket.timeout:
                self._throw_if_interrupted(location="transport.send")
                if perf_counter() >= operation_deadline:
                    raise
                continue
            if sent <= 0:
                raise ConnectionError("Connection closed while sending device request bytes.")
            bytes_sent += sent
        self._sock.settimeout(self.get_remaining_timeout(phase="read", default_s=self.config.read_timeout_s))

    def receive(self, recv_size: int | None = None) -> bytes:
        self.connect()
        assert self._sock is not None
        if self._cancellation_token is None:
            return self._sock.recv(recv_size or self.config.recv_size)

        operation_deadline = self._operation_deadline(phase="read", default_s=self.config.read_timeout_s)
        while True:
            self._throw_if_interrupted(location="transport.receive")
            timeout_s = self._step_timeout(operation_deadline)
            if timeout_s <= 0:
                self._throw_if_interrupted(location="transport.receive")
                raise socket.timeout("Timed out while waiting for device response bytes.")
            self._sock.settimeout(timeout_s)
            try:
                return self._sock.recv(recv_size or self.config.recv_size)
            except socket.timeout:
                self._throw_if_interrupted(location="transport.receive")
                if perf_counter() >= operation_deadline:
                    raise

    def receive_until(self, delimiter: bytes, *, max_bytes: int = 65536) -> bytes:
        self.connect()
        assert self._sock is not None
        if not delimiter:
            raise ValueError("TcpSocketTransport.receive_until requires a non-empty delimiter.")
        if max_bytes <= 0:
            raise ValueError("TcpSocketTransport.receive_until requires max_bytes > 0.")

        buffer = bytearray()
        chunk_size = min(self.config.recv_size, max_bytes)
        if self._cancellation_token is None:
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

        operation_deadline = self._operation_deadline(phase="read", default_s=self.config.read_timeout_s)
        while len(buffer) < max_bytes:
            self._throw_if_interrupted(location="transport.receive_until")
            timeout_s = self._step_timeout(operation_deadline)
            if timeout_s <= 0:
                self._throw_if_interrupted(location="transport.receive_until")
                raise socket.timeout("Response terminator was not received before the read deadline expired.")
            self._sock.settimeout(timeout_s)
            try:
                chunk = self._sock.recv(chunk_size)
            except socket.timeout:
                self._throw_if_interrupted(location="transport.receive_until")
                if perf_counter() >= operation_deadline:
                    raise
                continue
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
