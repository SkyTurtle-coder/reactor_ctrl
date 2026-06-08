import tempfile
import unittest
from pathlib import Path

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import Device, DeviceBindingCurrent, DeviceConnection, DeviceServer


class ReactorBuilderDisplayTargetApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "reactor_builder_display_targets.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False

        cls.app = create_app()
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.session.execute(
                text(
                    """
                    CREATE TABLE device_server (
                        device_server_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        server_code TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        vendor TEXT NOT NULL DEFAULT 'Moxa',
                        model TEXT,
                        host TEXT NOT NULL UNIQUE,
                        management_port INTEGER,
                        serial_standard TEXT NOT NULL DEFAULT 'rs232',
                        port_count INTEGER NOT NULL DEFAULT 8,
                        notes TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE device_connection (
                        connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_server_id INTEGER NOT NULL,
                        port_number INTEGER NOT NULL,
                        connection_label TEXT,
                        transport_type TEXT NOT NULL DEFAULT 'tcp_socket',
                        tcp_host TEXT NOT NULL,
                        tcp_port INTEGER NOT NULL,
                        baud_rate INTEGER NOT NULL DEFAULT 9600,
                        data_bits INTEGER NOT NULL DEFAULT 8,
                        parity TEXT NOT NULL DEFAULT 'N',
                        stop_bits INTEGER NOT NULL DEFAULT 1,
                        flow_control TEXT NOT NULL DEFAULT 'none',
                        read_timeout_ms INTEGER NOT NULL DEFAULT 1200,
                        write_timeout_ms INTEGER NOT NULL DEFAULT 1200,
                        reconnect_delay_ms INTEGER NOT NULL DEFAULT 1000,
                        last_seen_at TEXT,
                        last_error TEXT,
                        cc230_setpoint_write_mode INTEGER,
                        is_enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT,
                        UNIQUE(device_server_id, port_number),
                        UNIQUE(tcp_host, tcp_port)
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE device (
                        device_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        asset_serial TEXT NOT NULL UNIQUE,
                        manufacturer_serial TEXT,
                        display_name TEXT NOT NULL,
                        device_type TEXT NOT NULL,
                        protocol TEXT NOT NULL,
                        firmware_version TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT,
                        UNIQUE(manufacturer_serial, protocol)
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE device_binding_current (
                        device_id INTEGER PRIMARY KEY,
                        connection_id INTEGER NOT NULL UNIQUE,
                        first_seen_at TEXT,
                        last_seen_at TEXT,
                        is_online INTEGER NOT NULL DEFAULT 0,
                        quality_state TEXT NOT NULL DEFAULT 'unknown'
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE measurement_channel (
                        channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id INTEGER NOT NULL,
                        channel_code TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        unit TEXT NOT NULL,
                        value_type TEXT NOT NULL DEFAULT 'float',
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT,
                        UNIQUE(device_id, channel_code)
                    )
                    """
                )
            )
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            for table_name in (
                "measurement_channel",
                "device_binding_current",
                "device",
                "device_connection",
                "device_server",
            ):
                db.session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            db.session.commit()
            db.engine.dispose()

        cls._tmpdir.cleanup()
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = cls._original_manual_reconciler_enabled
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = cls._original_program_reconciler_enabled

    def setUp(self):
        with self.app.app_context():
            db.session.execute(text("DELETE FROM measurement_channel"))
            db.session.execute(text("DELETE FROM device_binding_current"))
            Device.query.delete()
            DeviceConnection.query.delete()
            DeviceServer.query.delete()
            db.session.commit()

    def _seed_ika_on_moxa_port_1(self) -> None:
        server = DeviceServer(server_code="MOXA-01", display_name="Moxa NPort", host="127.0.0.1")
        db.session.add(server)
        db.session.flush()

        connection = DeviceConnection(
            device_server_id=server.device_server_id,
            port_number=1,
            connection_label="Port 1",
            transport_type="tcp_socket",
            tcp_host="127.0.0.1",
            tcp_port=4001,
            baud_rate=9600,
            data_bits=8,
            parity="N",
            stop_bits=1,
            flow_control="none",
            read_timeout_ms=1200,
            write_timeout_ms=1200,
            reconnect_delay_ms=1000,
            is_enabled=True,
        )
        db.session.add(connection)
        db.session.flush()

        device = Device(
            asset_serial="IKA-PORT-1",
            manufacturer_serial="IKA-SN-PORT-1",
            display_name="IKA 60 Port 1",
            device_type="actuator",
            protocol="ika_eurostar_60",
            is_active=True,
        )
        db.session.add(device)
        db.session.flush()

        db.session.add(
            DeviceBindingCurrent(
                device_id=device.device_id,
                connection_id=connection.connection_id,
                is_online=True,
                quality_state="configured",
            )
        )
        db.session.commit()

    def test_display_targets_include_ika_channels_for_new_display_draft(self):
        with self.app.app_context():
            self._seed_ika_on_moxa_port_1()

        response = self.client.post(
            "/api/reactor-builds/display-targets",
            json={
                "definition_json": {
                    "canvas": {"width": 1200, "height": 800},
                    "nodes": [
                        {
                            "id": "node-display",
                            "symbol_id": "display",
                            "instance_id": "DISPLAY",
                            "label": "Display",
                            "category": "displays",
                            "x": 100,
                            "y": 100,
                            "width": 150,
                            "height": 62,
                            "communication": {},
                            "display": {},
                            "anchors": [],
                        },
                        {
                            "id": "node-motor",
                            "symbol_id": "motor",
                            "instance_id": "MOTOR",
                            "label": "Motor",
                            "category": "actuators",
                            "x": 300,
                            "y": 120,
                            "width": 82,
                            "height": 82,
                            "communication": {
                                "device_server_code": "MOXA",
                                "connection_label": "Port 1",
                                "protocol": "ika_eurostar_60",
                            },
                            "control": {"profile_id": "legacy-invalid-builder-profile", "config": {}},
                            "display": {},
                            "anchors": [],
                        },
                    ],
                    "edges": [],
                }
            },
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        motor_target = payload["targets"]["node-motor"]
        channel_codes = {channel["channel_code"] for channel in motor_target["channels"]}

        self.assertTrue(motor_target["is_resolved"])
        self.assertIn("ika_actual_rpm", channel_codes)
        self.assertIn("ika_setpoint_rpm", channel_codes)
        self.assertIn("ika_torque_ncm", channel_codes)


if __name__ == "__main__":
    unittest.main()
