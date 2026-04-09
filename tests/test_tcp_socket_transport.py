import socket
import unittest

from reactor_app.services.transports.tcp_socket import TcpSocketConfig, TcpSocketTransport


class _FakeSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self):
        self.closed = True


class TcpSocketTransportTests(unittest.TestCase):
    def test_config_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "host"):
            TcpSocketConfig("", 4001)
        with self.assertRaisesRegex(ValueError, "port"):
            TcpSocketConfig("127.0.0.1", 0)
        with self.assertRaisesRegex(ValueError, "recv_size"):
            TcpSocketConfig("127.0.0.1", 4001, recv_size=0)

    def test_receive_until_returns_when_delimiter_is_found(self):
        transport = TcpSocketTransport(TcpSocketConfig("127.0.0.1", 4001, recv_size=8))
        transport._sock = _FakeSocket([b"IKA", b" ES 60\r\n"])

        result = transport.receive_until(b"\r\n", max_bytes=64)

        self.assertEqual(result, b"IKA ES 60\r\n")

    def test_receive_until_raises_when_connection_closes_before_delimiter(self):
        transport = TcpSocketTransport(TcpSocketConfig("127.0.0.1", 4001, recv_size=8))
        transport._sock = _FakeSocket([b"PARTIAL", b""])

        with self.assertRaises(socket.timeout):
            transport.receive_until(b"\r\n", max_bytes=64)

    def test_receive_until_raises_when_max_bytes_is_reached_without_delimiter(self):
        transport = TcpSocketTransport(TcpSocketConfig("127.0.0.1", 4001, recv_size=4))
        transport._sock = _FakeSocket([b"ABCD"])

        with self.assertRaises(socket.timeout):
            transport.receive_until(b"\r\n", max_bytes=4)


if __name__ == "__main__":
    unittest.main()
