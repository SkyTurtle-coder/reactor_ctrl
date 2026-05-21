import socket
import unittest

from reactor_app.services.drivers import DriverValidationError, get_driver, list_supported_protocols, protocol_label
from reactor_app.services.drivers.base import DeviceCommandRequest
from reactor_app.services.drivers.huber_cc230 import HuberCC230Driver, _temperature_from_response


class _FakeConfig:
    recv_size = 4096


class _FakeTransport:
    def __init__(self, responses):
        self.config = _FakeConfig()
        self.responses = list(responses)
        self.sent = []
        self.drained = 0
        self.closed = False

    def connect(self):
        return None

    def close(self):
        self.closed = True

    def drain_input(self, **_kwargs):
        self.drained += 1
        return b""

    def send(self, payload):
        self.sent.append(payload)

    def receive_until(self, _delimiter, *, max_bytes=65536):
        if not self.responses:
            raise socket.timeout("timeout")
        response = self.responses.pop(0)
        if response == socket.timeout:
            raise socket.timeout("timeout")
        if isinstance(response, str):
            response = response.encode("ascii")
        return response


class HuberCC230DriverTests(unittest.TestCase):
    def execute(self, command_name, payload=None, responses=None):
        transport = _FakeTransport(responses or [])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name=command_name, payload=payload or {}),
        )
        return result, transport

    def test_protocol_is_registered(self):
        self.assertIn("huber_cc230", list_supported_protocols())
        self.assertIsInstance(get_driver("huber_cc230"), HuberCC230Driver)
        self.assertEqual(protocol_label("huber_cc230"), "Huber/Polystat CC230")

    def test_parse_temperature_variants(self):
        self.assertEqual(_temperature_from_response("025.0"), 25.0)
        self.assertEqual(_temperature_from_response("+025.0"), 25.0)
        self.assertEqual(_temperature_from_response("SP +02500"), 25.0)
        self.assertEqual(_temperature_from_response("2500"), 25.0)
        self.assertEqual(_temperature_from_response("TE -00500"), -5.0)

    def test_remote_local_start_stop_commands(self):
        for command_name, expected in (
            ("enable_remote", [b"REMOTE\r\n"]),
            ("enable_local", [b"LOCAL\r\n"]),
            ("start", [b"REMOTE\r\n", b"START\r\n"]),
            ("stop", [b"STOP\r\n"]),
        ):
            with self.subTest(command_name=command_name):
                result, transport = self.execute(command_name)
                self.assertTrue(result.metadata["value"])
                self.assertEqual(transport.sent, expected)

    def test_read_temperatures(self):
        cases = (
            ("get_setpoint", b"SETPOINT +02500\r\n", 25.0, b"SETPOINT?\r\n"),
            ("get_process_temp", b"TEMP +02450\r\n", 24.5, b"TEMP?\r\n"),
            ("get_bath_temp", b"BATH +02400\r\n", 24.0, b"BATH?\r\n"),
            # CC230 has no TI?/TE? commands; internal uses BATH? and external uses TEMP?.
            ("get_internal_temp", b"BATH +02300\r\n", 23.0, b"BATH?\r\n"),
            ("get_external_temp", b"TEMP +02200\r\n", 22.0, b"TEMP?\r\n"),
        )
        for command_name, response, expected, request_bytes in cases:
            with self.subTest(command_name=command_name):
                result, transport = self.execute(command_name, responses=[response])
                self.assertEqual(result.metadata["value"], expected)
                self.assertEqual(transport.sent[0], request_bytes)

    def test_read_setpoint_raises_on_timeout(self):
        with self.assertRaises((socket.timeout, OSError)):
            self.execute("get_setpoint", responses=[socket.timeout])

    def test_write_setpoint_sends_remote_then_setpoint_command(self):
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[],  # REMOTE and SETPOINT! do not expect a response
        )

        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(transport.sent, [b"REMOTE\r\n", b"SETPOINT! +025.00\r\n"])

    def test_write_setpoint_format_for_negative_temperature(self):
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": -5, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[],
        )

        self.assertEqual(result.metadata["value"], -5.0)
        self.assertEqual(transport.sent, [b"REMOTE\r\n", b"SETPOINT! -005.00\r\n"])

    def test_write_setpoint_rejects_out_of_range_temperature(self):
        with self.assertRaises(DriverValidationError):
            self.execute(
                "set_setpoint",
                payload={"temp_c": 200, "min_setpoint_c": -40, "max_setpoint_c": 150},
                responses=[b"SETPOINT +20000\r\n"],
            )

    def test_status_error_warning_and_sensor_commands(self):
        status, status_transport = self.execute("get_status", responses=[b"STATUS ON REMOTE\r\n"])
        self.assertTrue(status.metadata["value"]["temperature_control_active"])
        self.assertTrue(status.metadata["value"]["status_available"])
        self.assertEqual(status_transport.sent, [b"STATUS?\r\n"])

        error, error_transport = self.execute("get_error", responses=[b"ERROR 0\r\n"])
        self.assertEqual(error.metadata["value"], "ERROR 0")
        self.assertEqual(error_transport.sent, [b"ERROR?\r\n"])

        warning, warning_transport = self.execute("get_warning", responses=[b"WARN 0\r\n"])
        self.assertEqual(warning.metadata["value"], "WARN 0")
        self.assertEqual(warning_transport.sent, [b"WARN?\r\n"])

        internal, internal_transport = self.execute("select_internal_sensor")
        self.assertTrue(internal.metadata["value"])
        self.assertEqual(internal_transport.sent, [b"REMOTE\r\n", b"INTERN!\r\n"])

        external, external_transport = self.execute("select_external_sensor")
        self.assertTrue(external.metadata["value"])
        self.assertEqual(external_transport.sent, [b"REMOTE\r\n", b"EXTERN!\r\n"])

    def test_optional_status_commands_do_not_fail_on_timeout(self):
        status, status_transport = self.execute("get_status", responses=[socket.timeout])
        self.assertFalse(status.metadata["value"]["status_available"])
        self.assertIsNone(status.metadata["value"]["temperature_control_active"])
        self.assertIn("communication_error", status.metadata["value"])
        self.assertEqual(status_transport.sent, [b"STATUS?\r\n"])

        error, error_transport = self.execute("get_error", responses=[socket.timeout])
        self.assertEqual(error.metadata["value"], "")
        self.assertEqual(error_transport.sent, [b"ERROR?\r\n"])

        warning, warning_transport = self.execute("get_warning", responses=[socket.timeout])
        self.assertEqual(warning.metadata["value"], "")
        self.assertEqual(warning_transport.sent, [b"WARN?\r\n"])

    def test_manual_text_sends_crlf_and_reads_response_when_requested(self):
        result, transport = self.execute(
            "manual_text",
            payload={"text": "TYPE?", "expect_response": True},
            responses=[b"CC230\r\n"],
        )

        self.assertEqual(result.metadata["value"], "CC230")
        self.assertEqual(transport.sent, [b"TYPE?\r\n"])
        self.assertEqual(transport.drained, 1)


if __name__ == "__main__":
    unittest.main()
