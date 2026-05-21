import unittest
from unittest.mock import patch

import configure_moxa_nport as moxa_config


class ConfigureMoxaNPortTests(unittest.TestCase):
    def test_unistat_430_preset_applies_serial_settings_and_device_protocol(self):
        argv = [
            "--host",
            "192.168.1.50",
            "--device-preset",
            "huber_unistat_430",
            "--api-token",
            "token",
        ]
        parser = moxa_config.build_parser()
        args = parser.parse_args(argv)

        moxa_config._apply_device_preset(args, argv)

        self.assertEqual(args.baud_rate, 9600)
        self.assertEqual(args.data_bits, 8)
        self.assertEqual(args.parity, "N")
        self.assertEqual(args.stop_bits, 1)
        self.assertEqual(args.flow_control, "none")
        self.assertEqual(args.device_protocol, "huber_unistat_430")
        self.assertEqual(args.device_type, "thermostat")

    def test_ika_preset_applies_serial_settings_and_device_protocol(self):
        argv = [
            "--host",
            "192.168.1.50",
            "--device-preset",
            "ika_eurostar_60",
            "--api-token",
            "token",
        ]
        parser = moxa_config.build_parser()
        args = parser.parse_args(argv)

        moxa_config._apply_device_preset(args, argv)

        self.assertEqual(args.device_protocol, "ika_eurostar_60")
        self.assertEqual(args.device_type, "stirrer")

    def test_cc230_preset_applies_legacy_rs232_settings_and_device_protocol(self):
        argv = [
            "--host",
            "192.168.1.50",
            "--device-preset",
            "huber_cc230",
            "--api-token",
            "token",
        ]
        parser = moxa_config.build_parser()
        args = parser.parse_args(argv)

        moxa_config._apply_device_preset(args, argv)

        self.assertEqual(args.baud_rate, 9600)
        self.assertEqual(args.data_bits, 8)
        self.assertEqual(args.parity, "N")
        self.assertEqual(args.stop_bits, 1)
        self.assertEqual(args.flow_control, "none")
        self.assertEqual(args.read_timeout_ms, 5000)
        self.assertEqual(args.write_timeout_ms, 2000)
        self.assertEqual(args.device_protocol, "huber_cc230")
        self.assertEqual(args.device_type, "thermostat")

    def test_bind_device_creates_device_and_binding_for_selected_port(self):
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
                    "asset_serial": "MOXA-UNISTAT-P1-HUBER-UNISTAT-430",
                    "display_name": "Huber Unistat 430 Port 1",
                    "protocol": "huber_unistat_430",
                    "current_binding": {
                        "connection_id": payload["connection_id"],
                        "connection": {
                            "connection_label": "Port 1",
                        },
                    },
                }
            raise AssertionError(f"Unexpected request: {method} {path}")

        argv = [
            "configure_moxa_nport.py",
            "--base-url",
            "http://reactor.test",
            "--api-token",
            "token",
            "--host",
            "192.168.1.50",
            "--server-code",
            "MOXA-UNISTAT",
            "--display-name",
            "MOXA Unistat",
            "--port-count",
            "1",
            "--only-port",
            "1",
            "--device-preset",
            "huber_unistat_430",
            "--bind-device",
        ]

        with patch("configure_moxa_nport._request_json", side_effect=fake_request_json):
            with patch("sys.argv", argv):
                self.assertEqual(moxa_config.main(), 0)

        connection_payload = next(
            call["payload"] for call in calls
            if call["path"] == "/api/device-connections" and call["method"] == "POST"
        )
        self.assertEqual(connection_payload["tcp_port"], 4001)
        self.assertEqual(connection_payload["baud_rate"], 9600)
        self.assertEqual(connection_payload["data_bits"], 8)
        self.assertEqual(connection_payload["parity"], "N")
        self.assertEqual(connection_payload["flow_control"], "none")

        device_payload = next(call["payload"] for call in calls if call["path"] == "/api/devices" and call["method"] == "POST")
        self.assertEqual(device_payload["device_type"], "thermostat")
        self.assertEqual(device_payload["protocol"], "huber_unistat_430")

        binding_payload = next(call["payload"] for call in calls if call["path"] == "/api/devices/33/binding")
        self.assertEqual(binding_payload["connection_id"], 22)
        self.assertEqual(binding_payload["quality_state"], "configured")
        self.assertFalse(binding_payload["is_online"])


if __name__ == "__main__":
    unittest.main()
