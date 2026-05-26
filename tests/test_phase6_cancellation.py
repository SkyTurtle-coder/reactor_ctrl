import socket
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from reactor_app.services import (
    CancellationToken,
    CommandExecutionInterrupted,
    CommandPriority,
    CommandSource,
    DeviceCommand,
    RuntimeCommandInterruptedError,
    RuntimeCommandScheduler,
    RuntimeStatus,
    ScheduledRuntimeCommand,
)
from reactor_app.services.drivers.base import DeviceCommandRequest
from reactor_app.services.drivers.huber_unistat import HuberUnistatDriver
from reactor_app.services.transports.tcp_socket import TcpSocketConfig, TcpSocketTransport


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CancellationTokenTests(unittest.TestCase):
    def test_cancelled_token_raises_cancelled_interrupt(self):
        token = CancellationToken()

        token.cancel("Stop requested.")

        with self.assertRaises(CommandExecutionInterrupted) as ctx:
            token.throw_if_interrupted(location="test.token")

        self.assertEqual(ctx.exception.status, RuntimeStatus.CANCELLED)
        self.assertEqual(ctx.exception.location, "test.token")

    def test_expired_token_raises_configured_deadline_status(self):
        token = CancellationToken()
        token.set_deadline(
            _now_utc() - timedelta(milliseconds=1),
            status=RuntimeStatus.EXPIRED,
            reason="Total deadline elapsed.",
            source="test.deadline",
        )

        with self.assertRaises(CommandExecutionInterrupted) as ctx:
            token.throw_if_interrupted()

        self.assertEqual(ctx.exception.status, RuntimeStatus.EXPIRED)
        self.assertEqual(ctx.exception.location, "test.deadline")


class _FakeSocket:
    def __init__(self, *, recv_actions=None):
        self.recv_actions = list(recv_actions or [])
        self.timeouts: list[float | None] = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _recv_size):
        if not self.recv_actions:
            raise AssertionError("No fake recv action configured.")
        action = self.recv_actions.pop(0)
        if callable(action):
            return action()
        if action is socket.timeout:
            raise socket.timeout("timeout")
        return action

    def close(self):
        self.closed = True


class TcpSocketTransportCancellationTests(unittest.TestCase):
    def test_deadline_caps_transport_timeout_budget(self):
        token = CancellationToken(
            deadline=_now_utc() + timedelta(milliseconds=200),
            deadline_status=RuntimeStatus.TIMEOUT,
        )
        transport = TcpSocketTransport(TcpSocketConfig("127.0.0.1", 8101, read_timeout_s=1.2))
        transport.bind_runtime_control(cancellation_token=token)

        remaining = transport.get_remaining_timeout(phase="read")

        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, 0.2)
        self.assertGreater(remaining, 0.0)

    def test_receive_until_raises_cancelled_when_token_is_cancelled_mid_wait(self):
        token = CancellationToken()

        def cancel_then_timeout():
            token.cancel("Emergency stop requested.")
            raise socket.timeout("timeout")

        fake_socket = _FakeSocket(recv_actions=[cancel_then_timeout])
        transport = TcpSocketTransport(TcpSocketConfig("127.0.0.1", 8101, read_timeout_s=1.2))
        transport.bind_runtime_control(cancellation_token=token)

        with patch("reactor_app.services.transports.tcp_socket.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(CommandExecutionInterrupted) as ctx:
                transport.receive_until(b"\n", max_bytes=64)

        self.assertEqual(ctx.exception.status, RuntimeStatus.CANCELLED)
        self.assertTrue(any((timeout or 1.0) <= 0.25 for timeout in fake_socket.timeouts))


class RuntimeWorkerCooperativeCancellationTests(unittest.TestCase):
    def test_running_command_observes_cancellation_token(self):
        scheduler = RuntimeCommandScheduler(worker_count=1, idle_wait_s=0.01)
        command = DeviceCommand(
            device_id=7,
            command_type="phase6_wait",
            payload={},
            priority=CommandPriority.MANUAL,
            source=CommandSource.API,
            requested_by="phase6_test",
        )
        item = ScheduledRuntimeCommand(command=command, execute=lambda: None)

        def execute():
            while True:
                item.cancellation_token.throw_if_interrupted(location="test.worker_loop")
                time.sleep(0.01)

        item.execute = execute

        try:
            future = scheduler.submit(item, wait=False)
            deadline = time.monotonic() + 1.0
            while item.status != RuntimeStatus.RUNNING and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(item.status, RuntimeStatus.RUNNING)

            cancelled = scheduler.request_cancellation(device_id=7, reason="Emergency stop.")
            self.assertEqual([entry.command_id for entry in cancelled], [item.command_id])

            with self.assertRaises(RuntimeCommandInterruptedError) as ctx:
                future.result(timeout=1.0)

            self.assertEqual(ctx.exception.status, RuntimeStatus.CANCELLED)
            self.assertEqual(item.status, RuntimeStatus.CANCELLED)
        finally:
            scheduler.stop(timeout_s=1.0)


class _HuberCancellationTransport:
    recv_size = 64

    def __init__(self, token: CancellationToken):
        self.token = token
        self.sent: list[bytes] = []
        self.receive_calls = 0

    def send(self, payload):
        self.sent.append(payload)

    def receive_until(self, _delimiter, *, max_bytes):
        self.receive_calls += 1
        if max_bytes <= 0:
            raise AssertionError("max_bytes must be positive.")
        if self.receive_calls == 1:
            self.token.cancel("Stop during Huber preflight.")
            return b"{S0A0000\r\n"
        raise AssertionError("Driver should stop before the second preflight read.")


class HuberUnistatDriverCancellationTests(unittest.TestCase):
    def test_start_preflight_checks_cancellation_between_device_reads(self):
        token = CancellationToken()
        transport = _HuberCancellationTransport(token)
        request = DeviceCommandRequest(
            command_name="start",
            payload={},
            cancellation_token=token,
        )

        with self.assertRaises(CommandExecutionInterrupted) as ctx:
            HuberUnistatDriver().execute(transport=transport, request=request)

        self.assertEqual(ctx.exception.status, RuntimeStatus.CANCELLED)
        self.assertEqual(transport.sent, [b"{M0A****\r\n"])


if __name__ == "__main__":
    unittest.main()
