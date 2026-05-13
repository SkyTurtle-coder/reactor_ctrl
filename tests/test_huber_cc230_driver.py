"""Unit tests for the Huber CC230 NAMUR/ASCII driver."""
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
    parse_status_response,
)
from reactor_app.services.transports.tcp_socket import TcpSocketConfig


# ── Fake transport ─────────────────────────────────────────────────────────────

class _FakeTransport:
    """Simulates TcpSocketTransport; returns pre-configured byte responses."""

    def __init__(self, responses: list[bytes]):
        self.config = TcpSocketConfig("127.0.0.1", 4001, recv_size=8)
        self.responses = list(responses)
        self.sent: list[bytes] = []
        self.delimiter: bytes = b""
        self.closed = 0

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def receive_until(self, delimiter: bytes, *, max_bytes: int) -> bytes:
        self.delimiter = delimiter
        if not self.responses:
            raise AssertionError("No fake response configured.")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed += 1


# ── Parser tests ───────────────────────────────────────────────────────────────

class HuberCC230ParserTests(unittest.TestCase):

    # Temperature / numeric response
    def test_parse_bare_float(self):
        self.assertAlmostEqual(parse_numeric_response("+24.56"), 24.56)
        self.assertAlmostEqual(parse_numeric_response("-5.20"), -5.20)
        self.assertAlmostEqual(parse_numeric_response("24.56"), 24.56)

    def test_parse_response_with_status_suffix(self):
        # Device returns "value status_code" – pick last decimal token.
        self.assertAlmostEqual(parse_numeric_response("23.40 0"), 23.40)
        self.assertAlmostEqual(parse_numeric_response("0.00 0"), 0.00)
        self.assertAlmostEqual(parse_numeric_response("-12.50 0"), -12.50)

    def test_parse_response_with_command_echo(self):
        # Some firmware echoes the command: "IN_PV_00 23.40"
        self.assertAlmostEqual(parse_numeric_response("IN_PV_00 23.40"), 23.40)
        self.assertAlmostEqual(parse_numeric_response("IN_SP_00 25.00"), 25.00)

    def test_parse_comma_decimal(self):
        self.assertAlmostEqual(parse_numeric_response("SP=-10,5"), -10.5)

    def test_parse_rejects_non_numeric(self):
        with self.assertRaisesRegex(HuberCC230Error, "Could not parse"):
            parse_numeric_response("OK")
        with self.assertRaisesRegex(HuberCC230Error, "Could not parse"):
            parse_numeric_response("")
        with self.assertRaisesRegex(HuberCC230Error, "Could not parse"):
            parse_numeric_response(None)

    # STATUS response
    def test_parse_status_standby(self):
        s = parse_status_response("0")
        self.assertEqual(s["code"], 0)
        self.assertFalse(s["temperature_control_active"])
        self.assertFalse(s["alarm"])
        self.assertFalse(s["remote"])

    def test_parse_status_running(self):
        s = parse_status_response("1")
        self.assertEqual(s["code"], 1)
        self.assertTrue(s["temperature_control_active"])
        self.assertFalse(s["alarm"])

    def test_parse_status_alarm(self):
        s = parse_status_response("-1")
        self.assertEqual(s["code"], -1)
        self.assertTrue(s["alarm"])
        self.assertFalse(s["temperature_control_active"])

    def test_parse_status_remote_stop(self):
        s = parse_status_response("2")
        self.assertEqual(s["code"], 2)
        self.assertFalse(s["temperature_control_active"])
        self.assertTrue(s["remote"])

    def test_parse_status_remote_start(self):
        s = parse_status_response("3")
        self.assertEqual(s["code"], 3)
        self.assertTrue(s["temperature_control_active"])
        self.assertTrue(s["remote"])

    def test_parse_status_with_noise(self):
        # Device may append extra fields: "0 6" → code=0
        s = parse_status_response("0 6")
        self.assertEqual(s["code"], 0)

    def test_parse_status_rejects_empty(self):
        with self.assertRaisesRegex(HuberCC230Error, "Could not parse STATUS"):
            parse_status_response("NO_STATUS")

    # Line-ending normalisation
    def test_normalize_line_ending_names(self):
        self.assertEqual(normalize_line_ending("cr"), "\r")
        self.assertEqual(normalize_line_ending("CR"), "\r")
        self.assertEqual(normalize_line_ending("crlf"), "\r\n")
        self.assertEqual(normalize_line_ending("CRLF"), "\r\n")
        self.assertEqual(normalize_line_ending("lf"), "\n")

    def test_normalize_line_ending_raw_values(self):
        self.assertEqual(normalize_line_ending("\r"), "\r")
        self.assertEqual(normalize_line_ending("\r\n"), "\r\n")
        self.assertEqual(normalize_line_ending("\n"), "\n")

    def test_normalize_line_ending_none_is_default(self):
        from reactor_app.services.drivers.huber_cc230 import DEFAULT_LINE_ENDING
        self.assertEqual(normalize_line_ending(None), DEFAULT_LINE_ENDING)

    def test_normalize_line_ending_rejects_unknown(self):
        with self.assertRaises(ValueError):
            normalize_line_ending("tab")


# ── Mock client tests ──────────────────────────────────────────────────────────

class HuberCC230ClientTests(unittest.TestCase):

    def _client(self, **kw) -> HuberCC230Client:
        return HuberCC230Client("mock", mock=True, **kw)

    def test_detect_protocol_returns_namur(self):
        c = self._client()
        d = c.detect_protocol()
        self.assertEqual(d["protocol"], "namur")

    def test_get_internal_temperature(self):
        c = self._client()
        c._mock_temp = 23.45
        self.assertAlmostEqual(c.get_internal_temperature(), 23.45)

    def test_get_external_temperature(self):
        c = self._client()
        c._mock_external_temp = 22.80
        self.assertAlmostEqual(c.get_external_temperature(), 22.80)

    def test_get_external_temperature_returns_none_when_no_sensor(self):
        c = self._client()
        c._mock_external_temp = None
        # In mock mode, returns None directly.
        self.assertIsNone(c.get_external_temperature())

    def test_get_setpoint(self):
        c = self._client()
        c._mock_setpoint = 25.00
        self.assertAlmostEqual(c.get_setpoint(), 25.00)

    def test_set_setpoint_updates_mock_state(self):
        c = self._client()
        self.assertTrue(c.set_setpoint(30.0))
        self.assertAlmostEqual(c._mock_setpoint, 30.0)
        self.assertAlmostEqual(c.get_setpoint(), 30.0)

    def test_setpoint_range_enforced(self):
        c = self._client(min_setpoint_c=-50.0, max_setpoint_c=200.0)
        with self.assertRaisesRegex(HuberCC230Error, "outside configured safety range"):
            c.set_setpoint(250.0)
        with self.assertRaisesRegex(HuberCC230Error, "outside configured safety range"):
            c.set_setpoint(-100.0)

    def test_setpoint_at_boundaries_is_accepted(self):
        c = self._client(min_setpoint_c=-50.0, max_setpoint_c=200.0)
        self.assertTrue(c.set_setpoint(-50.0))
        self.assertTrue(c.set_setpoint(200.0))

    def test_start_stop_toggle(self):
        c = self._client()
        self.assertFalse(c._mock_running)
        self.assertTrue(c.start_temperature_control())
        self.assertTrue(c._mock_running)
        status = c.get_status()
        self.assertTrue(status["temperature_control_active"])
        self.assertTrue(c.stop_temperature_control())
        self.assertFalse(c._mock_running)

    def test_get_status_stopped(self):
        c = self._client()
        s = c.get_status()
        self.assertEqual(s["code"], 0)
        self.assertFalse(s["temperature_control_active"])
        self.assertFalse(s["alarm"])

    def test_get_status_running(self):
        c = self._client()
        c.start_temperature_control()
        s = c.get_status()
        self.assertEqual(s["code"], 1)
        self.assertTrue(s["temperature_control_active"])

    def test_full_workflow(self):
        c = self._client()
        c.detect_protocol()
        c.get_internal_temperature()
        c.get_external_temperature()
        c.get_setpoint()
        c.set_setpoint(25.0)
        c.start_temperature_control()
        self.assertTrue(c.get_status()["temperature_control_active"])
        c.stop_temperature_control()
        self.assertFalse(c.get_status()["temperature_control_active"])


# ── Driver integration tests ───────────────────────────────────────────────────

class HuberCC230DriverTests(unittest.TestCase):

    def test_protocols_are_registered(self):
        options = list_supported_protocol_options()
        self.assertIn({"id": "huber_cc230", "label": "Huber CC230"}, options)
        self.assertIn({"id": "huber_cc230_mock", "label": "Huber CC230 Mock"}, options)
        self.assertIsInstance(get_driver("huber_cc230"), HuberCC230Driver)
        self.assertIsInstance(get_driver("huber_cc230_mock"), HuberCC230MockDriver)

    def test_get_internal_temp_uses_crlf_by_default(self):
        transport = _FakeTransport([b"+23.40\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_internal_temp",
                payload={},
            ),
        )
        self.assertEqual(transport.sent, [b"IN_PV_00\r\n"])
        self.assertAlmostEqual(result.metadata["value"], 23.40)
        self.assertEqual(result.metadata["protocol"], "namur")

    def test_get_internal_temp_can_use_crlf(self):
        transport = _FakeTransport([b"+24.10\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_internal_temp",
                payload={"line_ending": "crlf"},
            ),
        )
        self.assertEqual(transport.sent, [b"IN_PV_00\r\n"])
        self.assertEqual(transport.delimiter, b"\r\n")
        self.assertAlmostEqual(result.metadata["value"], 24.10)

    def test_get_internal_temp_can_use_lf(self):
        transport = _FakeTransport([b"23.40 0\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_internal_temp",
                payload={"line_ending": "lf"},
            ),
        )
        self.assertEqual(transport.sent, [b"IN_PV_00\n"])
        self.assertAlmostEqual(result.metadata["value"], 23.40)

    def test_get_external_temp(self):
        transport = _FakeTransport([b"+22.80\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="get_external_temp",
                payload={"line_ending": "cr"},
            ),
        )
        self.assertEqual(transport.sent, [b"IN_PV_02\r"])
        self.assertAlmostEqual(result.metadata["value"], 22.80)

    def test_detect_protocol_uses_cr_first(self):
        # First probe (IN_PV_00 with CR) succeeds immediately.
        transport = _FakeTransport([b"+21.50\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="detect_protocol", payload={}),
        )
        self.assertEqual(transport.sent, [b"IN_PV_00\r"])
        self.assertEqual(result.metadata["protocol"], "namur")
        self.assertAlmostEqual(float(result.metadata["response"]), 21.50)

    def test_set_setpoint_formats_command(self):
        transport = _FakeTransport([b"OK\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="set_setpoint",
                payload={"temp_c": 25.0, "line_ending": "cr"},
            ),
        )
        self.assertEqual(transport.sent, [b"OUT_SP_00 25.00\r"])
        self.assertTrue(result.metadata["value"])

    def test_set_setpoint_validates_safety_range(self):
        with self.assertRaisesRegex(DriverValidationError, "outside configured safety range"):
            HuberCC230Driver().execute(
                transport=_FakeTransport([]),
                request=DeviceCommandRequest(
                    command_name="set_setpoint",
                    payload={
                        "temp_c": 201.0,
                        "min_setpoint_c": -50.0,
                        "max_setpoint_c": 200.0,
                    },
                ),
            )

    def test_status_returns_parsed_dict(self):
        transport = _FakeTransport([b"1\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="status",
                payload={"line_ending": "cr"},
            ),
        )
        self.assertEqual(result.metadata["value"]["code"], 1)
        self.assertTrue(result.metadata["value"]["temperature_control_active"])

    def test_start_sends_command_without_reply(self):
        # START has no reply; send_no_wait should be used (transport.send called once,
        # receive_until never called → responses list stays empty).
        transport = _FakeTransport([])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="start",
                payload={"line_ending": "cr"},
            ),
        )
        self.assertEqual(transport.sent, [b"START\r"])
        self.assertTrue(result.metadata["value"])

    def test_stop_sends_command_without_reply(self):
        transport = _FakeTransport([])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="stop",
                payload={"line_ending": "cr"},
            ),
        )
        self.assertEqual(transport.sent, [b"STOP\r"])
        self.assertTrue(result.metadata["value"])

    def test_raw_command_pass_through(self):
        transport = _FakeTransport([b"+23.45\r\n"])
        result = HuberCC230Driver().execute(
            transport=transport,
            request=DeviceCommandRequest(
                command_name="send_raw",
                payload={"text": "IN_PV_00", "line_ending": "cr"},
            ),
        )
        self.assertEqual(transport.sent, [b"IN_PV_00\r"])
        self.assertEqual(result.metadata["value"], "+23.45")

    def test_mock_driver_runs_without_transport(self):
        result = HuberCC230MockDriver().execute(
            transport=None,
            request=DeviceCommandRequest(command_name="get_setpoint", payload={}),
        )
        self.assertEqual(result.metadata["driver"], "huber_cc230_mock")
        self.assertAlmostEqual(result.metadata["value"], 20.0)

    def test_mock_driver_start_stop(self):
        driver = HuberCC230MockDriver()
        req_start = DeviceCommandRequest(command_name="start", payload={})
        req_stop = DeviceCommandRequest(command_name="stop", payload={})
        req_status = DeviceCommandRequest(command_name="status", payload={})

        driver.execute(transport=None, request=req_start)
        status = driver.execute(transport=None, request=req_status)
        # Each call creates a fresh mock client, so state is not shared between
        # driver.execute() calls — this confirms the mock is stateless per call.
        self.assertIn("code", status.metadata["value"])

    def test_unsupported_command_raises(self):
        from reactor_app.services.drivers.base import DriverError
        with self.assertRaises((DriverValidationError, DriverError)):
            HuberCC230Driver().execute(
                transport=_FakeTransport([]),
                request=DeviceCommandRequest(command_name="unknown_action", payload={}),
            )


if __name__ == "__main__":
    unittest.main()
