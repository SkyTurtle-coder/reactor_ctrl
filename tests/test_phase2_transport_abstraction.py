"""Phase 2 — Device and Transport Abstraction tests.

Covers:
- ITransport Protocol satisfaction by TcpSocketTransport
- TcpSocketTransport.recv_size and is_connected()
- build_transport() factory for supported and unsupported types
- DeviceCapability constants existence
- get_capabilities() on every concrete driver
"""
import unittest

from reactor_app.services.drivers import DeviceCapability
from reactor_app.services.drivers.base import DeviceDriver
from reactor_app.services.drivers.huber_cc230 import HuberCC230Driver
from reactor_app.services.drivers.huber_unistat import HuberUnistatDriver
from reactor_app.services.drivers.ika_eurostar import IkaEurostarDriver
from reactor_app.services.transports.factory import build_transport
from reactor_app.services.transports.interface import ITransport, TransportTypeNotSupportedError
from reactor_app.services.transports.tcp_socket import TcpSocketConfig, TcpSocketTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeConnection:
    """Minimal duck-typed DeviceConnection for factory tests."""

    def __init__(self, transport_type="tcp_socket", host="127.0.0.1", port=8101,
                 read_timeout_ms=1200, write_timeout_ms=1200):
        self.transport_type = transport_type
        self.tcp_host = host
        self.tcp_port = port
        self.read_timeout_ms = read_timeout_ms
        self.write_timeout_ms = write_timeout_ms


# ---------------------------------------------------------------------------
# ITransport protocol
# ---------------------------------------------------------------------------

class ITransportProtocolTests(unittest.TestCase):
    def _make_transport(self, recv_size=4096):
        cfg = TcpSocketConfig("127.0.0.1", 8101, recv_size=recv_size)
        return TcpSocketTransport(cfg)

    def test_tcp_socket_transport_satisfies_itransport_protocol(self):
        transport = self._make_transport()
        self.assertIsInstance(transport, ITransport)

    def test_recv_size_returns_config_value(self):
        transport = self._make_transport(recv_size=2048)
        self.assertEqual(transport.recv_size, 2048)

    def test_is_connected_false_before_connect(self):
        transport = self._make_transport()
        self.assertFalse(transport.is_connected())

    def test_recv_size_default(self):
        cfg = TcpSocketConfig("127.0.0.1", 8101)
        transport = TcpSocketTransport(cfg)
        self.assertEqual(transport.recv_size, 4096)


# ---------------------------------------------------------------------------
# build_transport() factory
# ---------------------------------------------------------------------------

class BuildTransportFactoryTests(unittest.TestCase):
    def test_tcp_socket_returns_tcp_transport(self):
        conn = _FakeConnection(transport_type="tcp_socket")
        transport = build_transport(conn, {})
        self.assertIsInstance(transport, TcpSocketTransport)
        self.assertIsInstance(transport, ITransport)

    def test_tcp_socket_is_default_when_transport_type_is_none(self):
        conn = _FakeConnection(transport_type=None)
        transport = build_transport(conn, {})
        self.assertIsInstance(transport, TcpSocketTransport)

    def test_payload_overrides_recv_size(self):
        conn = _FakeConnection()
        transport = build_transport(conn, {"recv_size": "1024"})
        self.assertEqual(transport.recv_size, 1024)

    def test_payload_overrides_read_timeout(self):
        conn = _FakeConnection()
        transport = build_transport(conn, {"response_timeout_ms": "500"})
        self.assertAlmostEqual(transport.config.read_timeout_s, 0.5)

    def test_unknown_type_raises_transport_type_not_supported(self):
        conn = _FakeConnection(transport_type="banana")
        with self.assertRaises(TransportTypeNotSupportedError):
            build_transport(conn, {})

    def test_future_type_serial_raises_transport_type_not_supported(self):
        conn = _FakeConnection(transport_type="serial")
        with self.assertRaises(TransportTypeNotSupportedError):
            build_transport(conn, {})

    def test_future_type_opcua_raises_transport_type_not_supported(self):
        conn = _FakeConnection(transport_type="opcua")
        with self.assertRaises(TransportTypeNotSupportedError):
            build_transport(conn, {})

    def test_future_type_modbus_tcp_raises_transport_type_not_supported(self):
        conn = _FakeConnection(transport_type="modbus_tcp")
        with self.assertRaises(TransportTypeNotSupportedError):
            build_transport(conn, {})

    def test_future_type_usb_raises_transport_type_not_supported(self):
        conn = _FakeConnection(transport_type="usb")
        with self.assertRaises(TransportTypeNotSupportedError):
            build_transport(conn, {})

    def test_error_message_mentions_future_release_for_planned_types(self):
        conn = _FakeConnection(transport_type="serial")
        with self.assertRaises(TransportTypeNotSupportedError) as ctx:
            build_transport(conn, {})
        self.assertIn("future release", str(ctx.exception))

    def test_error_message_mentions_unknown_for_arbitrary_strings(self):
        conn = _FakeConnection(transport_type="xyz_unknown")
        with self.assertRaises(TransportTypeNotSupportedError) as ctx:
            build_transport(conn, {})
        self.assertIn("Unknown", str(ctx.exception))

    def test_transport_type_not_supported_is_value_error(self):
        """TransportTypeNotSupportedError must be a ValueError for catch compatibility."""
        self.assertTrue(issubclass(TransportTypeNotSupportedError, ValueError))


# ---------------------------------------------------------------------------
# DeviceCapability constants
# ---------------------------------------------------------------------------

class DeviceCapabilityConstantsTests(unittest.TestCase):
    def test_thermal_capability_strings_are_defined(self):
        self.assertEqual(DeviceCapability.CAN_HEAT, "can_heat")
        self.assertEqual(DeviceCapability.CAN_COOL, "can_cool")
        self.assertEqual(DeviceCapability.CAN_SET_TEMPERATURE, "can_set_temperature")
        self.assertEqual(DeviceCapability.CAN_MEASURE_TEMPERATURE, "can_measure_temperature")

    def test_mechanical_capability_strings_are_defined(self):
        self.assertEqual(DeviceCapability.CAN_STIR, "can_stir")
        self.assertEqual(DeviceCapability.CAN_PUMP, "can_pump")

    def test_safety_and_feedback_strings_are_defined(self):
        self.assertEqual(DeviceCapability.CAN_EMERGENCY_STOP, "can_emergency_stop")
        self.assertEqual(DeviceCapability.HAS_FEEDBACK, "has_feedback")

    def test_recipe_capability_strings_are_defined(self):
        self.assertEqual(DeviceCapability.SUPPORTS_RAMP, "supports_ramp")
        self.assertEqual(DeviceCapability.SUPPORTS_MANUAL_MODE, "supports_manual_mode")
        self.assertEqual(DeviceCapability.SUPPORTS_RECIPE_MODE, "supports_recipe_mode")

    def test_all_constant_values_are_lowercase_strings(self):
        for attr in vars(DeviceCapability):
            if attr.startswith("_"):
                continue
            value = getattr(DeviceCapability, attr)
            if callable(value):
                continue
            self.assertIsInstance(value, str, msg=f"{attr} is not a str")
            self.assertEqual(value, value.lower(), msg=f"{attr} is not lowercase")


# ---------------------------------------------------------------------------
# DeviceDriver base class
# ---------------------------------------------------------------------------

class DeviceDriverBaseTests(unittest.TestCase):
    def test_base_get_capabilities_returns_empty_frozenset(self):
        class _MinimalDriver(DeviceDriver):
            protocol_names = ("test_only",)

            def execute(self, *, transport, request):
                raise NotImplementedError

        caps = _MinimalDriver().get_capabilities()
        self.assertIsInstance(caps, frozenset)
        self.assertEqual(len(caps), 0)


# ---------------------------------------------------------------------------
# HuberUnistatDriver capabilities
# ---------------------------------------------------------------------------

class HuberUnistatDriverCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.caps = HuberUnistatDriver().get_capabilities()

    def test_returns_frozenset(self):
        self.assertIsInstance(self.caps, frozenset)

    def test_includes_thermal_control(self):
        self.assertIn(DeviceCapability.CAN_HEAT, self.caps)
        self.assertIn(DeviceCapability.CAN_COOL, self.caps)
        self.assertIn(DeviceCapability.CAN_SET_TEMPERATURE, self.caps)
        self.assertIn(DeviceCapability.CAN_MEASURE_TEMPERATURE, self.caps)

    def test_includes_emergency_stop_and_feedback(self):
        self.assertIn(DeviceCapability.CAN_EMERGENCY_STOP, self.caps)
        self.assertIn(DeviceCapability.HAS_FEEDBACK, self.caps)

    def test_includes_recipe_and_manual_mode(self):
        self.assertIn(DeviceCapability.SUPPORTS_MANUAL_MODE, self.caps)
        self.assertIn(DeviceCapability.SUPPORTS_RECIPE_MODE, self.caps)

    def test_includes_ramp(self):
        self.assertIn(DeviceCapability.SUPPORTS_RAMP, self.caps)

    def test_does_not_include_stirring(self):
        self.assertNotIn(DeviceCapability.CAN_STIR, self.caps)


# ---------------------------------------------------------------------------
# IkaEurostarDriver capabilities
# ---------------------------------------------------------------------------

class IkaEurostarDriverCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.caps = IkaEurostarDriver().get_capabilities()

    def test_returns_frozenset(self):
        self.assertIsInstance(self.caps, frozenset)

    def test_includes_stirring(self):
        self.assertIn(DeviceCapability.CAN_STIR, self.caps)

    def test_includes_feedback_and_emergency_stop(self):
        self.assertIn(DeviceCapability.HAS_FEEDBACK, self.caps)
        self.assertIn(DeviceCapability.CAN_EMERGENCY_STOP, self.caps)

    def test_includes_manual_and_recipe_mode(self):
        self.assertIn(DeviceCapability.SUPPORTS_MANUAL_MODE, self.caps)
        self.assertIn(DeviceCapability.SUPPORTS_RECIPE_MODE, self.caps)

    def test_does_not_include_thermal_control(self):
        self.assertNotIn(DeviceCapability.CAN_HEAT, self.caps)
        self.assertNotIn(DeviceCapability.CAN_COOL, self.caps)
        self.assertNotIn(DeviceCapability.CAN_SET_TEMPERATURE, self.caps)


# ---------------------------------------------------------------------------
# HuberCC230Driver capabilities (legacy)
# ---------------------------------------------------------------------------

class HuberCC230DriverCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.caps = HuberCC230Driver().get_capabilities()

    def test_returns_frozenset(self):
        self.assertIsInstance(self.caps, frozenset)

    def test_includes_thermal_control(self):
        self.assertIn(DeviceCapability.CAN_HEAT, self.caps)
        self.assertIn(DeviceCapability.CAN_COOL, self.caps)
        self.assertIn(DeviceCapability.CAN_SET_TEMPERATURE, self.caps)
        self.assertIn(DeviceCapability.CAN_MEASURE_TEMPERATURE, self.caps)

    def test_includes_feedback_and_manual_mode(self):
        self.assertIn(DeviceCapability.HAS_FEEDBACK, self.caps)
        self.assertIn(DeviceCapability.SUPPORTS_MANUAL_MODE, self.caps)

    def test_legacy_driver_does_not_include_recipe_mode(self):
        self.assertNotIn(DeviceCapability.SUPPORTS_RECIPE_MODE, self.caps)

    def test_does_not_include_stirring(self):
        self.assertNotIn(DeviceCapability.CAN_STIR, self.caps)
