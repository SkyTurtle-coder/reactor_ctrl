import unittest

from reactor_app.services.drivers import HuberUnistatTCP, get_driver, list_supported_protocol_options
from reactor_app.services.drivers.base import DeviceCommandRequest, DriverError, DriverValidationError
from reactor_app.services.drivers.huber_unistat import HuberUnistatDriver
from reactor_app.services.transports.tcp_socket import TcpSocketConfig


class _FakeTransport:
    def __init__(self, responses):
        self.config = TcpSocketConfig("127.0.0.1", 8101, recv_size=8)
        self.responses = list(responses)
        self.sent = []

    @property
    def recv_size(self):
        return self.config.recv_size

    def send(self, payload):
        self.sent.append(payload)

    def receive_until(self, delimiter, *, max_bytes):
        self.assert_delimiter = delimiter
        self.assert_max_bytes = max_bytes
        if not self.responses:
            raise AssertionError("No fake response configured.")
        return self.responses.pop(0)


class HuberUnistatTCPTests(unittest.TestCase):
    def test_encodes_and_decodes_signed_temperatures(self):
        self.assertEqual(HuberUnistatTCP.encode_temp(25.0), "09C4")
        self.assertEqual(HuberUnistatTCP.encode_temp(-10.0), "FC18")
        self.assertEqual(HuberUnistatTCP.decode_temp("09C4"), 25.0)
        self.assertEqual(HuberUnistatTCP.decode_temp("FC18"), -10.0)

    def test_validate_response_checks_prefix_address_and_value(self):
        self.assertEqual(HuberUnistatTCP.validate_response("{S0109C4\r\n", "01"), "09C4")

        with self.assertRaisesRegex(DriverError, "address mismatch"):
            HuberUnistatTCP.validate_response("{S0209C4\r\n", "01")
        with self.assertRaisesRegex(DriverError, "not available"):
            HuberUnistatTCP.validate_response("{S017FFF\r\n", "01")
        with self.assertRaisesRegex(DriverError, "Invalid"):
            HuberUnistatTCP.validate_response("bad", "01")

    def test_build_request_uses_pilot_one_wire_format(self):
        self.assertEqual(HuberUnistatTCP.build_request("00", "****"), "{M00****\r\n")
        self.assertEqual(HuberUnistatTCP.build_request("14", "0001"), "{M140001\r\n")


class HuberUnistatDriverTests(unittest.TestCase):
    def test_protocol_is_registered_for_dropdown(self):
        options = list_supported_protocol_options()
        self.assertIn({"id": "huber_unistat_430", "label": "Huber Unistat 430"}, options)
        self.assertIsInstance(get_driver("huber_unistat_430"), HuberUnistatDriver)

    def test_get_internal_temp_reads_pb_01_and_decodes_temperature(self):
        transport = _FakeTransport([b"{S0109C4\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="get_internal_temp", payload={}),
        )

        self.assertEqual(transport.sent, [b"{M01****\r\n"])
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(result.metadata["value_hex"], "09C4")

    def test_get_external_temp_reads_pb_07_and_decodes_temperature(self):
        transport = _FakeTransport([b"{S0709C4\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="get_external_temp", payload={}),
        )

        self.assertEqual(transport.sent, [b"{M07****\r\n"])
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(result.metadata["value_hex"], "09C4")

    def test_read_live_telemetry_reads_setpoint_actual_and_external_temperature(self):
        transport = _FakeTransport([b"{S0009C4\r\n", b"{S010960\r\n", b"{S07092E\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="read_live_telemetry", payload={}),
        )

        self.assertEqual(
            result.metadata["value"],
            {
                "setpoint_C": 25.0,
                "actual_temp_C": 24.0,
                "external_temp_C": 23.5,
            },
        )
        self.assertEqual(transport.sent, [b"{M00****\r\n", b"{M01****\r\n", b"{M07****\r\n"])

    def test_read_live_telemetry_keeps_missing_external_sensor_raw_value(self):
        transport = _FakeTransport([b"{S0009C4\r\n", b"{S010960\r\n", b"{S07C504\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="read_live_telemetry", payload={}),
        )

        self.assertEqual(
            result.metadata["value"],
            {
                "setpoint_C": 25.0,
                "actual_temp_C": 24.0,
                "external_temp_C": -151.0,
            },
        )
        self.assertEqual(transport.sent, [b"{M00****\r\n", b"{M01****\r\n", b"{M07****\r\n"])

    def test_set_setpoint_checks_safety_range_and_writes_pb_00(self):
        transport = _FakeTransport([b"{S0009C4\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="set_setpoint", payload={"temp_c": 25.0}),
        )

        self.assertEqual(transport.sent, [b"{M0009C4\r\n"])
        self.assertEqual(result.metadata["value"], 25.0)

        with self.assertRaisesRegex(DriverValidationError, "outside configured safety range"):
            HuberUnistatDriver().execute(
                transport=_FakeTransport([]),
                request=DeviceCommandRequest(command_name="set_setpoint", payload={"temp_c": 151.0}),
            )

    def test_start_reads_preflight_and_blocks_on_error(self):
        transport = _FakeTransport([b"{S0A0101\r\n", b"{S050002\r\n", b"{S060000\r\n"])

        with self.assertRaisesRegex(DriverError, "start is blocked"):
            HuberUnistatDriver().execute(
                transport=transport,
                request=DeviceCommandRequest(command_name="start", payload={}),
            )

        self.assertEqual(transport.sent, [b"{M0A****\r\n", b"{M05****\r\n", b"{M06****\r\n"])

    def test_start_writes_temperature_control_when_preflight_is_clear(self):
        transport = _FakeTransport([b"{S0A0000\r\n", b"{S050000\r\n", b"{S060000\r\n", b"{S140001\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="start", payload={}),
        )

        self.assertTrue(result.metadata["value"])
        self.assertEqual(
            transport.sent,
            [b"{M0A****\r\n", b"{M05****\r\n", b"{M06****\r\n", b"{M140001\r\n"],
        )

    def test_get_status_skips_stale_mismatched_response(self):
        transport = _FakeTransport([b"{S000A92\r\n", b"{S0A0001\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="get_status", payload={}),
        )

        self.assertEqual(transport.sent, [b"{M0A****\r\n"])
        self.assertEqual(result.metadata["value"]["raw"], 1)
        self.assertTrue(result.metadata["value"]["temperature_control_active"])

    def test_get_status_skips_stale_mismatched_response_in_same_read(self):
        transport = _FakeTransport([b"{S000A92\r\n{S0A0001\r\n"])
        result = HuberUnistatDriver().execute(
            transport=transport,
            request=DeviceCommandRequest(command_name="get_status", payload={}),
        )

        self.assertEqual(transport.sent, [b"{M0A****\r\n"])
        self.assertEqual(result.metadata["value"]["raw"], 1)

    def test_sensor_selection_commands_are_controlled_for_unistat(self):
        internal_transport = _FakeTransport([])
        internal = HuberUnistatDriver().execute(
            transport=internal_transport,
            request=DeviceCommandRequest(command_name="select_internal_sensor", payload={}),
        )
        self.assertEqual(internal.metadata["value"], "internal")
        self.assertEqual(internal.metadata["active_control_sensor"], "internal")
        self.assertEqual(internal_transport.sent, [])

        with self.assertRaisesRegex(DriverValidationError, "sensor selection is not mapped"):
            HuberUnistatDriver().execute(
                transport=_FakeTransport([]),
                request=DeviceCommandRequest(command_name="select_external_sensor", payload={}),
            )


if __name__ == "__main__":
    unittest.main()
