import unittest
from unittest.mock import patch

import configure_ics435_scale as ics435_config


class ConfigureIcs435ScaleTests(unittest.TestCase):
    def test_defaults_match_lab_scale_setup(self):
        parser = ics435_config.build_parser()
        args = parser.parse_args(["--api-token", "token"])

        server_payload = ics435_config._server_payload(args)
        connection_payload = ics435_config._connection_payload(args, device_server_id=11)
        device_payload = ics435_config._device_payload(args)

        self.assertEqual(server_payload["server_code"], "ICS435-01")
        self.assertEqual(server_payload["host"], "192.168.55.29")
        self.assertEqual(server_payload["serial_standard"], "ethernet")
        self.assertEqual(connection_payload["connection_label"], "COM2 Ethernet")
        self.assertEqual(connection_payload["tcp_host"], "192.168.55.29")
        self.assertEqual(connection_payload["tcp_port"], 4305)
        self.assertEqual(device_payload["asset_serial"], "ICS435-01")
        self.assertEqual(device_payload["device_type"], "scale")
        self.assertEqual(device_payload["protocol"], "mettler_toledo_ics435")

    def test_main_creates_server_connection_device_and_binding(self):
        calls = []

        def fake_request_json(*, base_url, path, method="GET", token=None, payload=None):
            calls.append({"path": path, "method": method, "payload": payload})
            if path == "/api/device-servers" and method == "GET":
                return 200, {"items": []}
            if path == "/api/device-servers" and method == "POST":
                return 201, {"device_server_id": 11, **payload}
            if path == "/api/device-connections" and method == "GET":
                return 200, {"items": []}
            if path == "/api/device-connections" and method == "POST":
                return 201, {"connection_id": 22, **payload}
            if path == "/api/devices" and method == "GET":
                return 200, {"items": []}
            if path == "/api/devices" and method == "POST":
                return 201, {"device_id": 33, "current_binding": None, **payload}
            if path == "/api/devices/33/binding" and method == "PUT":
                return 200, {
                    "device_id": 33,
                    "asset_serial": "ICS435-01",
                    "display_name": "ICS435 Balance",
                    "protocol": "mettler_toledo_ics435",
                    "current_binding": {
                        "connection_id": payload["connection_id"],
                        "connection": {"connection_label": "COM2 Ethernet"},
                    },
                }
            raise AssertionError(f"Unexpected request: {method} {path}")

        argv = [
            "configure_ics435_scale.py",
            "--base-url",
            "http://reactor.test",
            "--api-token",
            "token",
        ]

        with patch("configure_ics435_scale._request_json", side_effect=fake_request_json):
            with patch("sys.argv", argv):
                self.assertEqual(ics435_config.main(), 0)

        server_payload = next(
            call["payload"] for call in calls
            if call["path"] == "/api/device-servers" and call["method"] == "POST"
        )
        self.assertEqual(server_payload["host"], "192.168.55.29")
        self.assertEqual(server_payload["vendor"], "Mettler Toledo")
        self.assertEqual(server_payload["model"], "ICS435")
        self.assertEqual(server_payload["port_count"], 1)

        connection_payload = next(
            call["payload"] for call in calls
            if call["path"] == "/api/device-connections" and call["method"] == "POST"
        )
        self.assertEqual(connection_payload["tcp_host"], "192.168.55.29")
        self.assertEqual(connection_payload["tcp_port"], 4305)
        self.assertEqual(connection_payload["transport_type"], "tcp_socket")

        device_payload = next(
            call["payload"] for call in calls
            if call["path"] == "/api/devices" and call["method"] == "POST"
        )
        self.assertEqual(device_payload["asset_serial"], "ICS435-01")
        self.assertEqual(device_payload["device_type"], "scale")
        self.assertEqual(device_payload["protocol"], "mettler_toledo_ics435")

        binding_payload = next(call["payload"] for call in calls if call["path"] == "/api/devices/33/binding")
        self.assertEqual(binding_payload["connection_id"], 22)
        self.assertEqual(binding_payload["quality_state"], "configured")
        self.assertFalse(binding_payload["is_online"])

    def test_main_updates_existing_server_connection_and_device(self):
        calls = []

        def fake_request_json(*, base_url, path, method="GET", token=None, payload=None):
            calls.append({"path": path, "method": method, "payload": payload})
            if path == "/api/device-servers" and method == "GET":
                return 200, {
                    "items": [
                        {
                            "device_server_id": 11,
                            "server_code": "ICS435-01",
                            "display_name": "Old",
                            "vendor": "Mettler Toledo",
                            "model": "ICS435",
                            "host": "192.168.55.10",
                            "serial_standard": "ethernet",
                            "port_count": 1,
                            "notes": None,
                            "is_active": True,
                        }
                    ]
                }
            if path == "/api/device-servers/11" and method == "PATCH":
                return 200, {"device_server_id": 11, **payload}
            if path == "/api/device-connections" and method == "GET":
                return 200, {
                    "items": [
                        {
                            "connection_id": 22,
                            "device_server_id": 11,
                            "port_number": 1,
                            "connection_label": "COM2 Ethernet",
                            "transport_type": "tcp_socket",
                            "tcp_host": "192.168.55.10",
                            "tcp_port": 4305,
                            "baud_rate": 9600,
                            "data_bits": 8,
                            "parity": "N",
                            "stop_bits": 1,
                            "flow_control": "none",
                            "read_timeout_ms": 1200,
                            "write_timeout_ms": 1200,
                            "reconnect_delay_ms": 1000,
                            "is_enabled": True,
                        }
                    ]
                }
            if path == "/api/device-connections/22" and method == "PATCH":
                return 200, {"connection_id": 22, **payload}
            if path == "/api/devices" and method == "GET":
                return 200, {
                    "items": [
                        {
                            "device_id": 33,
                            "asset_serial": "ICS435-01",
                            "display_name": "Old Scale",
                            "device_type": "scale",
                            "protocol": "mettler_toledo_ics435",
                            "manufacturer_serial": None,
                            "firmware_version": None,
                            "is_active": True,
                            "current_binding": {"connection_id": 22},
                        }
                    ]
                }
            if path == "/api/devices/33" and method == "PATCH":
                return 200, {"device_id": 33, "current_binding": {"connection_id": 22}, **payload}
            if path == "/api/devices/33/binding" and method == "PUT":
                return 200, {
                    "device_id": 33,
                    "asset_serial": "ICS435-01",
                    "display_name": "ICS435 Balance",
                    "protocol": "mettler_toledo_ics435",
                    "current_binding": {
                        "connection_id": payload["connection_id"],
                        "connection": {"connection_label": "COM2 Ethernet"},
                    },
                }
            raise AssertionError(f"Unexpected request: {method} {path}")

        argv = [
            "configure_ics435_scale.py",
            "--base-url",
            "http://reactor.test",
            "--api-token",
            "token",
        ]

        with patch("configure_ics435_scale._request_json", side_effect=fake_request_json):
            with patch("sys.argv", argv):
                self.assertEqual(ics435_config.main(), 0)

        self.assertTrue(any(call["path"] == "/api/device-servers/11" for call in calls))
        self.assertTrue(any(call["path"] == "/api/device-connections/22" for call in calls))
        self.assertTrue(any(call["path"] == "/api/devices/33" for call in calls))
        self.assertTrue(any(call["path"] == "/api/devices/33/binding" for call in calls))


if __name__ == "__main__":
    unittest.main()
