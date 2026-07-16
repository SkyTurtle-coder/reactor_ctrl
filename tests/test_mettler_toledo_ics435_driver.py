from __future__ import annotations

import socket
import threading
import time
import unittest
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Callable

from reactor_app.services.drivers import DeviceCapability, get_driver, list_supported_protocol_options
from reactor_app.services.drivers.base import DeviceCommandRequest, DriverError
from reactor_app.services.drivers.mettler_toledo_ics435 import (
    MettlerToledoICS435Driver,
    parse_weight_response,
)
from reactor_app.services.transports.tcp_socket import TcpSocketConfig, TcpSocketTransport


class _FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent: list[bytes] = []
        self.closed = 0
        self.recv_size = 8

    def connect(self):
        return None

    def close(self):
        self.closed += 1

    def is_connected(self):
        return True

    def bind_runtime_control(self, *, cancellation_token=None):
        return None

    def send(self, payload):
        self.sent.append(payload)

    def receive(self, recv_size=None):
        raise AssertionError("receive() is not used by the MT-SICS driver")

    def receive_until(self, delimiter, *, max_bytes):
        if not self.responses:
            raise AssertionError("No fake response configured.")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def send_and_receive(self, payload, recv_size=None):
        self.send(payload)
        return self.receive(recv_size=recv_size)

    def get_remaining_timeout(self, *, phase="read", default_s=None):
        return default_s


class _ScriptedMTSicsServer(AbstractContextManager):
    def __init__(self, scripts: list[Callable[[socket.socket, "_ScriptedMTSicsServer"], None]]):
        self.scripts = scripts
        self.commands: list[str] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.host, self.port = self._server.getsockname()
        self._server.listen()
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2):
                pass
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        self._server.close()

    def _serve(self):
        for script in self.scripts:
            if self._stop.is_set():
                return
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            with conn:
                script(conn, self)

    def read_command(self, conn: socket.socket) -> str:
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = conn.recv(1)
            if not chunk:
                break
            data.extend(chunk)
        command = bytes(data).decode("ascii").strip()
        self.commands.append(command)
        return command


class MettlerToledoICS435DriverTests(unittest.TestCase):
    def test_protocol_is_registered_for_dropdown(self):
        options = list_supported_protocol_options()
        self.assertIn({"id": "mettler_toledo_ics435", "label": "Mettler Toledo ICS435"}, options)
        driver = get_driver("mettler_toledo_ics435")
        self.assertIsInstance(driver, MettlerToledoICS435Driver)
        self.assertIn(DeviceCapability.CAN_WEIGH, driver.get_capabilities())

    def test_parse_weight_response_uses_decimal_and_stability_status(self):
        reading = parse_weight_response("S S     -12.340 kg")

        self.assertEqual(reading.value, Decimal("-12.340"))
        self.assertEqual(reading.unit, "kg")
        self.assertTrue(reading.stable)

    def test_read_weight_sends_si_and_returns_stable_metadata(self):
        transport = _FakeTransport([b"S S      12.34 g\r\n"])

        result = MettlerToledoICS435Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="read_weight", payload={}),
        )

        self.assertEqual(transport.sent, [b"SI\r\n"])
        self.assertEqual(result.response_text, "S S      12.34 g")
        self.assertEqual(result.metadata["weight"]["value_decimal"], "12.34")
        self.assertEqual(result.metadata["weight"]["unit"], "g")
        self.assertTrue(result.metadata["weight"]["stable"])

    def test_read_weight_reports_dynamic_negative_values(self):
        transport = _FakeTransport([b"S D      -0.15 kg\r\n"])

        result = MettlerToledoICS435Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="read_weight", payload={}),
        )

        self.assertEqual(result.metadata["weight"]["value_decimal"], "-0.15")
        self.assertFalse(result.metadata["weight"]["stable"])
        self.assertEqual(result.metadata["weight"]["quality_status"], "dynamic")

    def test_tare_clear_tare_and_zero_parse_acknowledgements(self):
        driver = MettlerToledoICS435Driver()

        tare = driver.execute(
            transport=_FakeTransport([b"T S       0.00 g\r\n"]),
            request=DeviceCommandRequest(command_name="tare", payload={}),
        )
        clear_tare = driver.execute(
            transport=_FakeTransport([b"TAC A\r\n"]),
            request=DeviceCommandRequest(command_name="clear_tare", payload={}),
        )
        zero = driver.execute(
            transport=_FakeTransport([b"Z A\r\n"]),
            request=DeviceCommandRequest(command_name="zero", payload={}),
        )

        self.assertEqual(tare.metadata["tare"]["value_decimal"], "0.00")
        self.assertTrue(clear_tare.metadata["value"])
        self.assertTrue(zero.metadata["value"])

    def test_i0_multiline_response_collects_supported_commands(self):
        transport = _FakeTransport([
            b'I0 B 0 "I0"\r\nI0 B 0 "SI"\r\n',
            b'I0 A 1 "T"\r\n',
        ])

        result = MettlerToledoICS435Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="list_commands", payload={}),
        )

        self.assertEqual(transport.sent, [b"I0\r\n"])
        self.assertEqual(result.metadata["supported_commands"], ["I0", "SI", "T"])

    def test_initialize_queries_device_info_before_i0(self):
        transport = _FakeTransport([
            b'I4 A "SN123"\r\n',
            b'I3 A "2.10 10.28"\r\n',
            b'I2 A "ICS435"\r\n',
            b'I1 A "0123" "2.00"\r\n',
            b'I0 A 0 "SI"\r\n',
        ])

        result = MettlerToledoICS435Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="initialize", payload={}),
        )

        self.assertEqual(transport.sent, [b"I4\r\n", b"I3\r\n", b"I2\r\n", b"I1\r\n", b"I0\r\n"])
        info = result.metadata["device_info"]
        self.assertEqual(info["serial_number"], "SN123")
        self.assertEqual(info["device_identification"], "ICS435")
        self.assertEqual(info["supported_commands"], ["SI"])

    def test_unsolicited_i4_before_weight_is_skipped(self):
        transport = _FakeTransport([b'I4 A "BOOT"\r\nS S       1.23 g\r\n'])

        result = MettlerToledoICS435Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="read_weight", payload={}),
        )

        self.assertEqual(result.metadata["weight"]["value_decimal"], "1.23")
        self.assertEqual(result.metadata["skipped_responses"], ['I4 A "BOOT"'])

    def test_device_error_responses_raise_driver_errors(self):
        for response, message in (
            (b"S +\r\n", "overload"),
            (b"S -\r\n", "underload"),
            (b"S I\r\n", "not executable"),
            (b"ES\r\n", "Syntax error"),
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(DriverError, message):
                    MettlerToledoICS435Driver().execute(
                        transport=_FakeTransport([response]),
                        request=DeviceCommandRequest(command_name="read_weight", payload={}),
                    )

    def test_tcp_emulator_handles_fragmented_response_and_extra_line(self):
        def script(conn: socket.socket, server: _ScriptedMTSicsServer):
            server.read_command(conn)
            conn.sendall(b"S S")
            time.sleep(0.01)
            conn.sendall(b"       1.23 g\r\nI4 A \"BOOT\"\r\n")

        with _ScriptedMTSicsServer([script]) as server:
            transport = TcpSocketTransport(TcpSocketConfig(server.host, server.port, read_timeout_s=1.0))
            result = MettlerToledoICS435Driver().execute(
                transport=transport,
                request=DeviceCommandRequest(command_name="read_weight", payload={}),
            )
            transport.close()

        self.assertEqual(server.commands, ["SI"])
        self.assertEqual(result.metadata["weight"]["value_decimal"], "1.23")

    def test_reconnect_retries_after_connection_close(self):
        def close_after_request(conn: socket.socket, server: _ScriptedMTSicsServer):
            server.read_command(conn)

        def answer_second_request(conn: socket.socket, server: _ScriptedMTSicsServer):
            server.read_command(conn)
            conn.sendall(b"S S       2.50 g\r\n")

        with _ScriptedMTSicsServer([close_after_request, answer_second_request]) as server:
            transport = TcpSocketTransport(TcpSocketConfig(server.host, server.port, read_timeout_s=0.5))
            result = MettlerToledoICS435Driver().execute(
                transport=transport,
                request=DeviceCommandRequest(
                    command_name="read_weight",
                    payload={"max_retries": 1, "retry_delay_ms": 0},
                ),
            )
            transport.close()

        self.assertEqual(server.commands, ["SI", "SI"])
        self.assertEqual(result.metadata["weight"]["value_decimal"], "2.50")


if __name__ == "__main__":
    unittest.main()
