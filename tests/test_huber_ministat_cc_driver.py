import socket
import unittest

from reactor_app.services.drivers import (
    DriverValidationError,
    HuberMinistatCCDriver,
    get_driver,
    list_supported_protocols,
    protocol_label,
)
from reactor_app.services.drivers.base import DeviceCommandRequest, DriverError
from reactor_app.services.drivers.huber_ministat_cc import (
    _format_pp_temperature,
    _temperature_from_pp_response,
)


class _FakeConfig:
    recv_size = 4096


class _FakeTransport:
    def __init__(self, responses):
        self.config = _FakeConfig()
        self.responses = list(responses)
        self.sent = []
        self.drained = 0

    @property
    def recv_size(self):
        return self.config.recv_size

    def connect(self):
        return None

    def close(self):
        return None

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


class HuberMinistatCCDriverTests(unittest.TestCase):
    def execute(self, command_name, payload=None, responses=None):
        transport = _FakeTransport(responses or [])
        result = HuberMinistatCCDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name=command_name, payload=payload or {}),
        )
        return result, transport

    def test_protocol_is_registered(self):
        self.assertIn("huber_ministat_cc", list_supported_protocols())
        self.assertIsInstance(get_driver("huber_ministat_cc"), HuberMinistatCCDriver)
        self.assertEqual(protocol_label("huber_ministat_cc"), "Huber Ministat cc")

    def test_formats_and_parses_pp_temperatures(self):
        self.assertEqual(_format_pp_temperature(25.0), "+02500")
        self.assertEqual(_format_pp_temperature(-12.34), "-01234")
        self.assertEqual(_temperature_from_pp_response("SP +02500\r\n"), 25.0)
        self.assertEqual(_temperature_from_pp_response("TI +02499\r\n"), 24.99)
        self.assertEqual(_temperature_from_pp_response("TE -15100\r\n"), -151.0)

    def test_read_pp_temperature_channels(self):
        cases = (
            ("get_setpoint", b"SP +02500\r\n", 25.0, b"SP?\r\n"),
            ("get_internal_temp", b"TI +02499\r\n", 24.99, b"TI?\r\n"),
            ("get_external_temp", b"TE +02345\r\n", 23.45, b"TE?\r\n"),
        )
        for command_name, response, expected, sent in cases:
            with self.subTest(command_name=command_name):
                result, transport = self.execute(command_name, responses=[response])
                self.assertEqual(result.metadata["value"], expected)
                self.assertEqual(transport.sent, [sent])

    def test_status_start_and_stop_use_ca_commands(self):
        status, status_transport = self.execute("get_status", responses=[b"CA +00001\r\n"])
        self.assertTrue(status.metadata["value"]["temperature_control_active"])
        self.assertTrue(status.metadata["value"]["circulation_active"])
        self.assertEqual(status_transport.sent, [b"CA?\r\n"])

        started, start_transport = self.execute("start", responses=[b"CA +00001\r\n"])
        self.assertTrue(started.metadata["value"])
        self.assertEqual(start_transport.sent, [b"CA@ 00001\r\n"])

        stopped, stop_transport = self.execute("stop", responses=[b"CA +00000\r\n"])
        self.assertTrue(stopped.metadata["value"])
        self.assertEqual(stop_transport.sent, [b"CA@ 00000\r\n"])

    def test_optional_status_and_error_commands_do_not_fail_on_timeout(self):
        status, status_transport = self.execute("get_status", responses=[socket.timeout])
        self.assertFalse(status.metadata["value"]["status_available"])
        self.assertIsNone(status.metadata["value"]["temperature_control_active"])
        self.assertIn("communication_error", status.metadata["value"])
        self.assertEqual(status_transport.sent, [b"CA?\r\n"])

        error, error_transport = self.execute("get_error", responses=[socket.timeout])
        self.assertEqual(error.metadata["value"], "")
        self.assertEqual(error_transport.sent, [b"FSW?\r\n"])

    def test_write_setpoint_uses_pp_setpoint_write_and_verifies_readback(self):
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": -12.34, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[b"SP -01234\r\n"],
        )

        self.assertEqual(transport.sent, [b"SP@ -01234\r\n"])
        self.assertEqual(result.metadata["value"], -12.34)
        self.assertEqual(result.metadata["verified_setpoint"], -12.34)
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")

    def test_write_setpoint_rejects_out_of_range_temperature(self):
        with self.assertRaises(DriverValidationError):
            self.execute(
                "set_setpoint",
                payload={"temp_c": 200, "min_setpoint_c": -40, "max_setpoint_c": 150},
                responses=[b"SP +20000\r\n"],
            )

    def test_write_setpoint_rejects_mismatched_readback(self):
        with self.assertRaises(DriverError):
            self.execute(
                "set_setpoint",
                payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
                responses=[b"SP +02000\r\n"],
            )

    def test_sensor_selection_uses_compatible_control_pp_commands(self):
        internal, internal_transport = self.execute("select_internal_sensor", responses=[b"INTERN ON\r\n"])
        self.assertTrue(internal.metadata["value"])
        self.assertEqual(internal.metadata["active_control_sensor"], "internal")
        self.assertEqual(internal_transport.sent, [b"INTERN@\r\n"])

        external, external_transport = self.execute("select_external_sensor", responses=[b"EXTERN ON\r\n"])
        self.assertTrue(external.metadata["value"])
        self.assertEqual(external.metadata["active_control_sensor"], "external")
        self.assertEqual(external_transport.sent, [b"EXTERN@\r\n"])

    def test_read_live_telemetry_suppresses_missing_external_sensor_sentinel(self):
        result, transport = self.execute(
            "read_live_telemetry",
            responses=[
                b"SP +02500\r\n",
                b"TI +02499\r\n",
                b"TE -15111\r\n",
                b"CA +00001\r\n",
            ],
        )

        self.assertEqual(
            result.metadata["value"],
            {
                "setpoint_C": 25.0,
                "actual_temp_C": 24.99,
                "external_temp_C": None,
                "temperature_control_active": True,
                "circulation_active": True,
                "status_raw": 1,
            },
        )
        self.assertEqual(transport.sent, [b"SP?\r\n", b"TI?\r\n", b"TE?\r\n", b"CA?\r\n"])

    def test_error_status_uses_fsw_query(self):
        result, transport = self.execute("get_error", responses=[b"0\r\n"])
        self.assertEqual(result.metadata["value"], "0")
        self.assertEqual(transport.sent, [b"FSW?\r\n"])

    def test_manual_text_sends_crlf(self):
        result, transport = self.execute(
            "manual_text",
            payload={"text": "FSW?", "expect_response": True},
            responses=[b"0\r\n"],
        )

        self.assertEqual(result.metadata["value"], "0")
        self.assertEqual(transport.sent, [b"FSW?\r\n"])


if __name__ == "__main__":
    unittest.main()
