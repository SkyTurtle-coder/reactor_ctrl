import socket
import unittest
from unittest.mock import patch

from reactor_app.services.drivers import DriverValidationError, get_driver, list_supported_protocols, protocol_label
from reactor_app.services.drivers.base import DeviceCommandRequest
from reactor_app.services.drivers.huber_cc230 import (
    DriverError,
    HuberCC230Driver,
    _format_cc230_matlab_set_command,
    _ordered_setpoint_write_variants,
    _format_set_command_b,
    _format_set_command_c,
    _temperature_from_response,
)


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
        self.assertEqual(_temperature_from_response("- 10.0"), -10.0)
        self.assertEqual(_temperature_from_response("SP +02500"), 25.0)
        self.assertEqual(_temperature_from_response("2500"), 25.0)
        self.assertEqual(_temperature_from_response("TE -00500"), -5.0)

    def test_format_cc230_matlab_set_command(self):
        self.assertEqual(_format_cc230_matlab_set_command(10.0), "SET +00010")
        self.assertEqual(_format_cc230_matlab_set_command(-10.0), "SET -00010")
        self.assertEqual(_format_cc230_matlab_set_command(25.5), "SET +025.5")
        self.assertEqual(_format_cc230_matlab_set_command(-25.5), "SET -025.5")
        self.assertEqual(_format_cc230_matlab_set_command(0.0), "SET +00000")
        self.assertEqual(_format_cc230_matlab_set_command(25.0), "SET +00025")
        self.assertEqual(_format_cc230_matlab_set_command(-5.0), "SET -00005")

    def test_format_set_command_b(self):
        self.assertEqual(_format_set_command_b(30.0), "SET +030.0")
        self.assertEqual(_format_set_command_b(-5.0), "SET -005.0")
        self.assertEqual(_format_set_command_b(0.0), "SET +000.0")

    def test_format_set_command_c(self):
        self.assertEqual(_format_set_command_c(30.0), "SET +03000")
        self.assertEqual(_format_set_command_c(-5.0), "SET -00500")
        self.assertEqual(_format_set_command_c(0.0), "SET +00000")

    def test_negative_setpoints_use_safe_write_variant_order(self):
        self.assertEqual(
            _ordered_setpoint_write_variants(-5.0),
            [
                (2, "SET -00500"),
                (1, "SET -005.0"),
                (3, "SET -00005"),
                (0, "SETPOINT! -005.00"),
            ],
        )
        self.assertEqual(
            _ordered_setpoint_write_variants(-5.0, preferred_write_mode=3)[0],
            (2, "SET -00500"),
        )
        self.assertEqual(
            _ordered_setpoint_write_variants(-5.0, preferred_write_mode=1)[0],
            (1, "SET -005.0"),
        )

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
            ("get_internal_temp", b"TI +02300\r\n", 23.0, b"TI?\r\n"),
            ("get_external_temp", b"TE +02200\r\n", 22.0, b"TE?\r\n"),
        )
        for command_name, response, expected, request_bytes in cases:
            with self.subTest(command_name=command_name):
                result, transport = self.execute(command_name, responses=[response])
                self.assertEqual(result.metadata["value"], expected)
                self.assertEqual(transport.sent[0], request_bytes)

    def test_read_setpoint_falls_back_to_sp(self):
        # SETPOINT? times out; SP? provides the value.
        result, transport = self.execute(
            "get_setpoint",
            responses=[socket.timeout, b"SP +02500\r\n"],
        )
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(transport.sent, [b"SETPOINT?\r\n", b"SP?\r\n"])

    def test_read_setpoint_raises_when_both_timeout(self):
        # SETPOINT? and SP? both time out → DriverError.
        with self.assertRaises((socket.timeout, OSError, DriverError)):
            self.execute("get_setpoint", responses=[socket.timeout])

    # ------------------------------------------------------------------ #
    # write_setpoint: readback verified                                   #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_verified(self, _mock_sleep):
        # Readback returns the requested value → verified.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[b"SETPOINT +02500\r\n"],
        )
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(result.metadata["verified_setpoint"], 25.0)
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(result.metadata["write_mode_used"], 3)
        self.assertEqual(
            transport.sent,
            [b"REMOTE\r\n", b"SET +00025\r\n", b"SETPOINT?\r\n"],
        )

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_verified_negative_temperature(self, _mock_sleep):
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": -5, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[b"SETPOINT -00500\r\n"],
        )
        self.assertEqual(result.metadata["value"], -5.0)
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(result.metadata["write_mode_used"], 2)
        self.assertEqual(
            transport.sent,
            [b"REMOTE\r\n", b"SET -00500\r\n", b"SETPOINT?\r\n"],
        )

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_negative_ignores_positive_preferred_mode(self, _mock_sleep):
        result, transport = self.execute(
            "set_setpoint",
            payload={
                "temp_c": -5,
                "min_setpoint_c": -40,
                "max_setpoint_c": 150,
                "cc230_write_mode": 3,
            },
            responses=[b"SETPOINT -00500\r\n"],
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(result.metadata["write_mode_used"], 2)
        self.assertEqual(
            transport.sent,
            [b"REMOTE\r\n", b"SET -00500\r\n", b"SETPOINT?\r\n"],
        )

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_verified_within_tolerance(self, _mock_sleep):
        # Readback is within 0.1 °C tolerance (e.g. rounding artefact).
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[b"SETPOINT +02508\r\n"],  # 25.08 °C → deviation 0.08 < 0.1
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertAlmostEqual(result.metadata["verified_setpoint"], 25.08, places=2)

    # ------------------------------------------------------------------ #
    # write_setpoint: readback times out → unverified                     #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_readback_timeout_returns_unverified(self, _mock_sleep):
        # SETPOINT? and SP? both time out; write accepted as unverified.
        # Empty responses → both queries raise socket.timeout.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[],
        )
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertIsNone(result.metadata["verified_setpoint"])
        self.assertEqual(result.metadata["setpoint_sync_status"], "unverified")
        self.assertEqual(result.metadata["write_mode_used"], 3)
        # After the write, exactly SETPOINT? and SP? are queried.
        self.assertEqual(
            transport.sent,
            [b"REMOTE\r\n", b"SET +00025\r\n", b"SETPOINT?\r\n", b"SP?\r\n"],
        )

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_readback_timeout_does_not_try_next_variant(self, _mock_sleep):
        # A readback timeout means the device never responds to SETPOINT?; there
        # is no point trying the next write variant because its readback would also
        # time out.  Only one write command must be sent.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 30, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[socket.timeout],  # SETPOINT? times out; SP? also (no more)
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "unverified")
        # Only one write variant (mode 3) was attempted.
        sent_commands = [b for b in transport.sent]
        self.assertIn(b"SET +00030\r\n", sent_commands)
        self.assertNotIn(b"SET +030.0\r\n", sent_commands)
        self.assertNotIn(b"SET +03000\r\n", sent_commands)

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_negative_write_setpoint_readback_timeout_uses_safe_first_variant(self, _mock_sleep):
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": -5, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[],
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "unverified")
        self.assertEqual(result.metadata["write_mode_used"], 2)
        self.assertEqual(
            transport.sent,
            [b"REMOTE\r\n", b"SET -00500\r\n", b"SETPOINT?\r\n", b"SP?\r\n"],
        )

    # ------------------------------------------------------------------ #
    # write_setpoint: legacy fallback chain                               #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_falls_back_to_variant_b(self, _mock_sleep):
        # Mode 3 (MATLAB SET) readback returns wrong value; mode 1 (SET decimal) works.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[
                b"SETPOINT +02000\r\n",  # mode 3 readback: 20 °C — wrong
                b"SETPOINT +02500\r\n",  # mode 1 readback: 25 °C — correct
            ],
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(result.metadata["write_mode_used"], 1)
        self.assertEqual(result.metadata["verified_setpoint"], 25.0)
        sent = transport.sent
        self.assertIn(b"SET +00025\r\n", sent)
        self.assertIn(b"SET +025.0\r\n", sent)

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_falls_back_to_variant_c(self, _mock_sleep):
        # Modes 3 and 1 return wrong values; mode 2 (SET integer) works.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[
                b"SETPOINT +02000\r\n",  # mode 3 readback: wrong
                b"SETPOINT +02000\r\n",  # mode 1 readback: wrong
                b"SETPOINT +02500\r\n",  # mode 2 readback: correct
            ],
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(result.metadata["write_mode_used"], 2)
        sent = transport.sent
        self.assertIn(b"SET +02500\r\n", sent)

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_preferred_mode_tried_first(self, _mock_sleep):
        # When preferred_write_mode=1 is passed, SET decimal is tried before the MATLAB mode.
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150, "cc230_write_mode": 1},
            responses=[b"SETPOINT +02500\r\n"],  # first readback confirms
        )
        self.assertEqual(result.metadata["write_mode_used"], 1)
        # SET must appear before SETPOINT! in the sent list.
        sent = transport.sent
        set_idx = next(i for i, b in enumerate(sent) if b"SET +" in b and b"SETPOINT" not in b)
        setpoint_idx = next((i for i, b in enumerate(sent) if b"SETPOINT!" in b), len(sent))
        self.assertLess(set_idx, setpoint_idx)

    # ------------------------------------------------------------------ #
    # write_setpoint: all variants rejected                               #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_all_variants_fail_raises_driver_error(self, _mock_sleep):
        # All four readbacks return a value that is too far from the requested one.
        with self.assertRaises(DriverError):
            self.execute(
                "set_setpoint",
                payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
                responses=[
                    b"SETPOINT +02000\r\n",  # mode 3 readback: wrong
                    b"SETPOINT +02000\r\n",  # mode 1 readback: wrong
                    b"SETPOINT +02000\r\n",  # mode 2 readback: wrong
                    b"SETPOINT +02000\r\n",  # mode 0 readback: wrong
                ],
            )

    # ------------------------------------------------------------------ #
    # write_setpoint: validation                                          #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_rejects_out_of_range_temperature(self, _mock_sleep):
        with self.assertRaises(DriverValidationError):
            self.execute(
                "set_setpoint",
                payload={"temp_c": 200, "min_setpoint_c": -40, "max_setpoint_c": 150},
                responses=[b"SETPOINT +20000\r\n"],
            )

    # ------------------------------------------------------------------ #
    # Other commands                                                      #
    # ------------------------------------------------------------------ #

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

    def test_process_temperature_retries_stale_sensor_ack_before_fallback(self):
        result, transport = self.execute(
            "get_process_temp",
            responses=[b"INTERN\r\n", b"TEMP +02450\r\n"],
        )

        self.assertEqual(result.metadata["value"], 24.5)
        self.assertEqual(transport.sent, [b"TEMP?\r\n", b"TEMP?\r\n"])

    def test_process_temperature_falls_back_to_legacy_internal_query(self):
        result, transport = self.execute(
            "get_process_temp",
            responses=[b"INTERN\r\n", socket.timeout, b"TI +02440\r\n"],
        )

        self.assertEqual(result.metadata["value"], 24.4)
        self.assertEqual(transport.sent, [b"TEMP?\r\n", b"TEMP?\r\n", b"TI?\r\n"])


if __name__ == "__main__":
    unittest.main()
