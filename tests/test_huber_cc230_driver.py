import unittest

from reactor_app.services.drivers import (
    HuberCC230Client,
    HuberCC230Driver,
    HuberCC230MockDriver,
    get_driver,
    list_supported_protocol_options,
)
from reactor_app.services.drivers.base import DeviceCommandRequest, DriverValidationError
from reactor_app.services.drivers.huber_cc230 import (
    HuberCC230Error,
    normalize_line_ending,
    parse_numeric_response,
)
from reactor_app.services.transports.tcp_socket import TcpSocketConfig


class _FakeTransport:
    def __init__(self, responses):
        self.config = TcpSocketConfig("127.0.0.1", 4001, recv_size=8)
        self.responses = list(responses)
        self.sent = []
        self.closed = 0

    def send(self, payload):
        self.sent.append(payload)

    def receive_until(self, delimiter, *, max_bytes):
        self.delimiter = delimiter
        self.max_bytes = max_bytes
        if not self.responses:
            raise AssertionError("No fake response configured.")
        return self.responses.pop(0)

    def close(self):
        self.closed += 1


class HuberCC230ParserTests(unittest.TestCase):
    def test_parse_numeric_response_uses_last_token(self):
        self.assertEqual(parse_numeric_response("IN_PV_00 21.75"), 21.75)
        self.assertEqual(parse_numeric_response("SP=-10,5"), -10.5)

    def test_parse_numeric_response_rejects_non_numeric_text(self):
        with self.assertRaisesRegex(HuberCC230Error, "Could not parse"):
            parse_numeric_response("OK")

    def test_normalize_line_ending_accepts_names_and_raw_values(self):
        self.assertEqual(normalize_line_ending("cr"), "\r")
        self.assertEqual(normalize_line_ending("CRLF"), "\r\n")
        self.assertEqual(normalize_line_ending("\r"), "\r")


class HuberCC230ClientTests(unittest.TestCase):
    def test_mock_client_detects_namur_and_executes_temperature_commands(self):
        client = HuberCC230Client("mock", mock=True)

        detection = client.detect_protocol()
        self.assertEqual(detection["protocol"], "namur")
        self.assertEqual(client.get_internal_temperature(), 20.0)
        self.assertEqual(client.get_setpoint(), 20.0)
        self.assertTrue(client.set_setpoint(25.0))
        self.assertEqual(client.get_setpoint(), 25.0)
        self.assertTrue(client.start_temperature_control())
        self.assertTrue(client.get_status()["temperature_control_active"])
        self.assertTrue(client.stop_temperature_control())

    def test_client_setpoint_range_is_enforced_before_sending(self):
        client = HuberCC230Client("mock", mock=True, min_setpoint_c=-50, max_setpoint_c=200)

        with self.assertRaisesRegex(HuberCC230Error, "outside configured safety range"):
            client.set_setpoint(250.0)


class HuberCC230DriverTests(unittest.TestCase):
    def test_protocols_are_registered(self):
        options = list_supported_protocol_options()
        self.assertIn({"id": "huber_cc230", "label": "Huber CC230"}, options)
        self.assertIn({"id": "huber_cc230_mock", "label": "Huber CC230 Mock"}, options)
        self.assertIsInstance(get_driver("huber_cc230"), HuberCC230Driver)
        self.assertIsInstance(get_driver("huber_cc230_mock"), HuberCC230MockDriver)

    def test_get_internal_temp_uses_namur_ascii_with_cr_by_default(self):
        transport = _FakeTransport([b"IN_PV_00 23.40\r"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_internal_temp",
                payload={"protocol_variant": "namur"},
            ),
        )

        self.assertEqual(transport.sent, [b"IN_PV_00\r"])
        self.assertEqual(result.metadata["value"], 23.4)
        self.assertEqual(result.metadata["protocol"], "namur")

    def test_get_internal_temp_can_use_crlf(self):
        transport = _FakeTransport([b"24.10\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_internal_temp",
                payload={"protocol_variant": "namur", "line_ending": "crlf"},
            ),
        )

        self.assertEqual(transport.sent, [b"IN_PV_00\r\n"])
        self.assertEqual(transport.delimiter, b"\r\n")
        self.assertEqual(result.metadata["value"], 24.1)

    def test_detect_protocol_stores_first_plausible_reply(self):
        transport = _FakeTransport([b"21.50\r"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="detect_protocol", payload={}),
        )

        self.assertEqual(transport.sent, [b"IN_PV_00\r"])
        self.assertEqual(result.metadata["protocol"], "namur")
        self.assertEqual(result.metadata["response"], "21.50")

    def test_set_setpoint_validates_safety_range(self):
        with self.assertRaisesRegex(DriverValidationError, "outside configured safety range"):
            HuberCC230Driver().execute(
                transport=_FakeTransport([]),
                request=DeviceCommandRequest(
                    command_name="set_setpoint",
                    payload={
                        "protocol_variant": "namur",
                        "temp_c": 201.0,
                        "min_setpoint_c": -50,
                        "max_setpoint_c": 200,
                    },
                ),
            )

    def test_mock_driver_runs_without_transport(self):
        result = HuberCC230MockDriver().execute(
            transport=None,
            request=DeviceCommandRequest(command_name="get_setpoint", payload={}),
        )

        self.assertEqual(result.metadata["driver"], "huber_cc230_mock")
        self.assertEqual(result.metadata["value"], 20.0)


if __name__ == "__main__":
    unittest.main()
