import socket
import unittest
from unittest.mock import patch

from reactor_app.services.drivers import DriverValidationError, get_driver, list_supported_protocols, protocol_label
from reactor_app.services.drivers.base import DeviceCommandRequest
from reactor_app.services.drivers.huber_cc230 import (
    DriverError,
    HuberCC230Driver,
    _CC230_READ_INTER_COMMAND_DELAY_S,
    _format_cc230_matlab_set_command,
    _is_stale_cc230_response,
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

    @property
    def recv_size(self):
        return self.config.recv_size

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
        self.assertEqual(_temperature_from_response("TI -00069"), -0.69)
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
            ("get_setpoint", b"SP +02500\r\n", 25.0, b"SP?\r\n"),
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

    def test_read_setpoint_falls_back_to_setpoint_query(self):
        # SP? is tried first (more reliable on older units); if it times out
        # the driver falls back to SETPOINT?.
        result, transport = self.execute(
            "get_setpoint",
            responses=[socket.timeout, b"SETPOINT +02500\r\n"],
        )
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(transport.sent, [b"SP?\r\n", b"SETPOINT?\r\n"])

    def test_read_setpoint_raises_when_both_timeout(self):
        # SETPOINT? and SP? both time out → DriverError.
        with self.assertRaises((socket.timeout, OSError, DriverError)):
            self.execute("get_setpoint", responses=[socket.timeout])

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_read_live_telemetry_batches_temperature_reads_into_one_driver_command(self, _mock_sleep):
        result, transport = self.execute(
            "read_live_telemetry",
            responses=[
                b"TI +02440\r\n",
                b"TE +02420\r\n",
                b"SP +02500\r\n",
            ],
        )

        self.assertEqual(
            result.metadata["value"],
            {
                "setpoint_C": 25.0,
                "internal_temp_C": 24.4,
                "external_temp_C": 24.2,
            },
        )
        self.assertNotIn("actual_temp_C", result.metadata["value"])
        self.assertNotIn("bath_temp_C", result.metadata["value"])
        self.assertNotIn("cc230_error", result.metadata["value"])
        self.assertEqual(
            transport.sent,
            [b"TI?\r\n", b"TE?\r\n", b"SP?\r\n"],
        )

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

    # ------------------------------------------------------------------ #
    # read_live_telemetry: partial failures / robustness                  #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_read_live_telemetry_partial_timeout_external_still_returns_other_channels(self, _mock_sleep):
        # TE? times out (no external sensor) but internal_temp_C and setpoint_C
        # must still be returned.  No DriverError should be raised.
        result, transport = self.execute(
            "read_live_telemetry",
            responses=[
                b"TI +02440\r\n",   # TI? → internal_temp_C
                socket.timeout,     # TE? → times out, external_temp_C = None
                b"SP +02500\r\n",   # SP? → setpoint_C
            ],
        )

        telemetry = result.metadata["value"]
        self.assertEqual(telemetry["internal_temp_C"], 24.4)
        self.assertEqual(telemetry["setpoint_C"], 25.0)
        self.assertIsNone(telemetry["external_temp_C"])
        self.assertIn(b"TI?\r\n", transport.sent)
        self.assertIn(b"TE?\r\n", transport.sent)
        self.assertIn(b"SP?\r\n", transport.sent)

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_read_live_telemetry_partial_timeout_setpoint_still_returns_temps(self, _mock_sleep):
        # SP? and SETPOINT? both time out, but the two temperature channels succeed.
        result, transport = self.execute(
            "read_live_telemetry",
            responses=[
                b"TI +02440\r\n",  # internal_temp_C
                b"TE +02420\r\n",  # external_temp_C
                socket.timeout,    # SP? → timeout
                socket.timeout,    # SETPOINT? → timeout
            ],
        )

        telemetry = result.metadata["value"]
        self.assertEqual(telemetry["internal_temp_C"], 24.4)
        self.assertEqual(telemetry["external_temp_C"], 24.2)
        self.assertIsNone(telemetry["setpoint_C"])

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_read_live_telemetry_all_channels_fail_raises_driver_error(self, _mock_sleep):
        # All three reads time out → DriverError.
        with self.assertRaises(DriverError):
            self.execute("read_live_telemetry", responses=[])

    def test_read_external_temperature_does_not_fall_back_to_temp_query(self):
        # TE? times out.  TEMP? must NOT be tried, because TEMP? returns the
        # active (potentially internal) sensor and would be misleadingly stored
        # as external_temp_C.
        from reactor_app.services.drivers.huber_cc230 import HuberCC230Client
        transport = _FakeTransport([socket.timeout])
        client = HuberCC230Client(transport)
        with self.assertRaises((DriverError, socket.timeout, OSError)):
            client.read_external_temperature()
        # Only TE? should have been sent — no TEMP? fallback.
        self.assertEqual(transport.sent, [b"TE?\r\n"])

    # ------------------------------------------------------------------ #
    # Temperature parsing: additional formats                             #
    # ------------------------------------------------------------------ #

    def test_parse_temperature_comma_decimal_separator(self):
        self.assertAlmostEqual(_temperature_from_response("25,00"), 25.0)
        self.assertAlmostEqual(_temperature_from_response("SP 25,50"), 25.5)
        self.assertAlmostEqual(_temperature_from_response("-5,00"), -5.0)

    def test_parse_temperature_integer_hundredths_all_formats(self):
        # These are the documented CC230 fixed-width integer formats.
        self.assertEqual(_temperature_from_response("+02500"), 25.0)
        self.assertEqual(_temperature_from_response("02500"), 25.0)
        self.assertEqual(_temperature_from_response("-00500"), -5.0)
        self.assertEqual(_temperature_from_response("TE -00500"), -5.0)
        self.assertEqual(_temperature_from_response("TI -00069"), -0.69)

    def test_parse_temperature_decimal_point_formats(self):
        self.assertEqual(_temperature_from_response("025.0"), 25.0)
        self.assertEqual(_temperature_from_response("+025.0"), 25.0)
        self.assertEqual(_temperature_from_response("25.00"), 25.0)
        self.assertEqual(_temperature_from_response("-5.00"), -5.0)
        self.assertEqual(_temperature_from_response("- 10.0"), -10.0)

    def test_parse_temperature_raises_on_text_only_response(self):
        with self.assertRaises(DriverError):
            _temperature_from_response("INTERN")
        with self.assertRaises(DriverError):
            _temperature_from_response("OK")
        with self.assertRaises(DriverError):
            _temperature_from_response("")
        with self.assertRaises(DriverError):
            _temperature_from_response("SENSOR OFF")

    # ------------------------------------------------------------------ #
    # drain idle timeout constant is exposed                              #
    # ------------------------------------------------------------------ #

    def test_drain_idle_timeout_constant_is_at_least_50ms(self):
        from reactor_app.services.drivers.huber_cc230 import _CC230_DRAIN_IDLE_TIMEOUT_S
        # Must be generous enough to absorb Moxa NPort delayed ACKs.
        self.assertGreaterEqual(_CC230_DRAIN_IDLE_TIMEOUT_S, 0.05)

    # ------------------------------------------------------------------ #
    # Timing constants: ramp-phase robustness                             #
    # ------------------------------------------------------------------ #

    def test_write_settle_constant_at_least_700ms(self):
        from reactor_app.services.drivers.huber_cc230 import _CC230_WRITE_SETTLE_S
        # Must be long enough for the CC230 to process a SET during a ramp
        # without a delayed ACK racing the subsequent readback query.
        self.assertGreaterEqual(_CC230_WRITE_SETTLE_S, 0.7)

    def test_read_inter_command_delay_constant_is_at_least_150ms(self):
        # Pause between TI?, TE? and SP? prevents stale responses from one
        # query contaminating the next channel read during rapid polling.
        self.assertGreaterEqual(_CC230_READ_INTER_COMMAND_DELAY_S, 0.15)

    # ------------------------------------------------------------------ #
    # _is_stale_cc230_response: unit tests                                #
    # ------------------------------------------------------------------ #

    def test_is_stale_response_detects_echo(self):
        # Moxa NPort software echo: response == sent command.
        self.assertTrue(_is_stale_cc230_response("TI?", "TI?"))
        self.assertTrue(_is_stale_cc230_response("TEMP?", "TEMP?"))
        self.assertTrue(_is_stale_cc230_response("SP?", "SP?"))
        # Command without trailing '?' also counts as echo.
        self.assertTrue(_is_stale_cc230_response("TI", "TI?"))

    def test_is_stale_response_detects_ack_tokens(self):
        # Known status/ACK strings emitted by the CC230 or its firmware.
        self.assertTrue(_is_stale_cc230_response("INTERN", "TI?"))
        self.assertTrue(_is_stale_cc230_response("OK", "TI?"))
        self.assertTrue(_is_stale_cc230_response("REMOTE", "SP?"))
        self.assertTrue(_is_stale_cc230_response("RUNNING", "TI?"))
        self.assertTrue(_is_stale_cc230_response("LOCAL", "TE?"))
        self.assertTrue(_is_stale_cc230_response("STOP", "TI?"))

    def test_is_stale_response_does_not_classify_temperature_responses(self):
        # Real temperature responses must never be treated as stale.
        self.assertFalse(_is_stale_cc230_response("TI +02440", "TI?"))
        self.assertFalse(_is_stale_cc230_response("TEMP +02450", "TEMP?"))
        self.assertFalse(_is_stale_cc230_response("+02500", "SP?"))
        self.assertFalse(_is_stale_cc230_response("TE -00500", "TE?"))

    def test_is_stale_response_returns_false_for_empty_or_none(self):
        self.assertFalse(_is_stale_cc230_response(None, "TI?"))
        self.assertFalse(_is_stale_cc230_response("", "TI?"))

    # ------------------------------------------------------------------ #
    # Stale ACK / echo in temperature chain                               #
    # ------------------------------------------------------------------ #

    def test_stale_ok_ack_in_chain_skips_to_next_fallback_command(self):
        # An "OK" ACK left in the buffer from a prior write is received as the
        # TI? response; the chain detects it as stale and falls to BATH?.
        result, transport = self.execute(
            "get_internal_temp",
            responses=[b"OK\r\n", b"BATH +02440\r\n"],
        )
        self.assertEqual(result.metadata["value"], 24.4)
        self.assertEqual(transport.sent, [b"TI?\r\n", b"BATH?\r\n"])

    def test_echo_response_in_chain_skips_to_next_fallback_command(self):
        # Moxa NPort software echo reflects TI? back; the chain detects it
        # and falls to BATH? which returns the real temperature.
        result, transport = self.execute(
            "get_internal_temp",
            responses=[b"TI?\r\n", b"BATH +02440\r\n"],
        )
        self.assertEqual(result.metadata["value"], 24.4)
        self.assertEqual(transport.sent, [b"TI?\r\n", b"BATH?\r\n"])

    def test_stale_remote_ack_in_setpoint_chain_skips_to_setpoint_fallback(self):
        # "REMOTE" ACK lands in the buffer just before SP? reads it; the chain
        # falls to SETPOINT? which responds correctly.
        result, transport = self.execute(
            "get_setpoint",
            responses=[b"REMOTE\r\n", b"SETPOINT +02500\r\n"],
        )
        self.assertEqual(result.metadata["value"], 25.0)
        self.assertEqual(transport.sent, [b"SP?\r\n", b"SETPOINT?\r\n"])

    # ------------------------------------------------------------------ #
    # write_setpoint: explicit drain after write-settle                   #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_write_setpoint_drains_buffer_after_write_settle(self, _mock_sleep):
        # An explicit _clear_input_buffer() call after the write-settle sleep
        # means the total drain count for a single verified write is 4:
        #   REMOTE (drain in send_command) + SET (drain in send_command)
        #   + explicit post-settle drain + SETPOINT? (drain in send_command).
        result, transport = self.execute(
            "set_setpoint",
            payload={"temp_c": 25, "min_setpoint_c": -40, "max_setpoint_c": 150},
            responses=[b"SETPOINT +02500\r\n"],
        )
        self.assertEqual(result.metadata["setpoint_sync_status"], "verified")
        self.assertEqual(transport.drained, 4)

    # ------------------------------------------------------------------ #
    # read_live_telemetry: inter-command delay                            #
    # ------------------------------------------------------------------ #

    @patch("reactor_app.services.drivers.huber_cc230.time.sleep")
    def test_read_live_telemetry_inserts_inter_command_delays(self, mock_sleep):
        # Two pauses of _CC230_READ_INTER_COMMAND_DELAY_S are inserted between
        # the three channel reads (TI→TE, TE→SP) to prevent response crosstalk
        # during ramp phases.
        self.execute(
            "read_live_telemetry",
            responses=[b"TI +02440\r\n", b"TE +02420\r\n", b"SP +02500\r\n"],
        )
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        inter_delay_count = sum(1 for v in sleep_args if v == _CC230_READ_INTER_COMMAND_DELAY_S)
        self.assertEqual(inter_delay_count, 2)


if __name__ == "__main__":
    unittest.main()
