import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import (
    ControlCommand,
    ControlCommandEvent,
    Device,
    DeviceConnection,
    DeviceServer,
    RecipeProgramEvent,
    RecipeProgramRun,
)
from reactor_app.services.activity_log import load_activity_logs, summarize_activity_logs
from reactor_app.services.activity_log_retention import run_activity_log_retention


class ActivityLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED
        cls._original_activity_log_retention_enabled = app_config.Config.ACTIVITY_LOG_RETENTION_ENABLED
        cls._original_activity_log_retention_days = app_config.Config.ACTIVITY_LOG_RETENTION_DAYS
        cls._original_activity_log_retention_dry_run = app_config.Config.ACTIVITY_LOG_RETENTION_DRY_RUN

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "activity_log.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False
        app_config.Config.ACTIVITY_LOG_RETENTION_ENABLED = True
        app_config.Config.ACTIVITY_LOG_RETENTION_DAYS = 7
        app_config.Config.ACTIVITY_LOG_RETENTION_DRY_RUN = False

        cls.app = create_app()
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.session.execute(
                text(
                    """
                    CREATE TABLE device (
                        device_id INTEGER PRIMARY KEY,
                        asset_serial TEXT NOT NULL UNIQUE,
                        manufacturer_serial TEXT,
                        display_name TEXT NOT NULL,
                        device_type TEXT NOT NULL,
                        protocol TEXT NOT NULL,
                        firmware_version TEXT,
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
                    CREATE TABLE control_command (
                        command_id INTEGER PRIMARY KEY,
                        device_id INTEGER NOT NULL,
                        request_uuid TEXT NOT NULL UNIQUE,
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        command_name TEXT NOT NULL,
                        command_payload TEXT,
                        status TEXT NOT NULL DEFAULT 'queued',
                        requested_at TEXT,
                        scheduled_for TEXT,
                        sent_at TEXT,
                        ack_at TEXT,
                        finished_at TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        error_message TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE control_command_event (
                        command_event_id INTEGER PRIMARY KEY,
                        command_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        event_payload TEXT,
                        created_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE device_server (
                        device_server_id INTEGER PRIMARY KEY,
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
                        connection_id INTEGER PRIMARY KEY,
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
                        updated_at TEXT
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
                    CREATE TABLE device_manual_state (
                        device_id INTEGER PRIMARY KEY,
                        desired_is_on INTEGER,
                        desired_speed INTEGER,
                        desired_version INTEGER NOT NULL DEFAULT 0,
                        applied_version INTEGER NOT NULL DEFAULT 0,
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        last_desired_at TEXT,
                        reported_is_on INTEGER,
                        reported_setpoint_rpm INTEGER,
                        actual_rpm REAL,
                        torque_ncm REAL,
                        last_reported_at TEXT,
                        queue_status TEXT NOT NULL DEFAULT 'idle',
                        last_error TEXT,
                        next_poll_at TEXT,
                        watch_expires_at TEXT,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE measurement (
                        measurement_id INTEGER PRIMARY KEY,
                        device_id INTEGER NOT NULL,
                        channel_id INTEGER,
                        channel_code TEXT NOT NULL,
                        measured_at TEXT NOT NULL,
                        ingested_at TEXT,
                        numeric_value REAL,
                        text_value TEXT,
                        unit TEXT,
                        quality_score REAL,
                        raw_payload TEXT,
                        source TEXT NOT NULL
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_run (
                        recipe_program_run_id INTEGER PRIMARY KEY,
                        recipe_id INTEGER,
                        reactor_build_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'running',
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        recipe_title TEXT,
                        operator_name TEXT,
                        snapshot_json TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        last_progress_at TEXT,
                        last_error TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_event (
                        recipe_program_event_id INTEGER PRIMARY KEY,
                        recipe_program_run_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        active_step_index INTEGER,
                        event_payload TEXT,
                        created_at TEXT
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
                "recipe_program_event",
                "recipe_program_run",
                "measurement",
                "device_manual_state",
                "device_binding_current",
                "device_connection",
                "device_server",
                "control_command_event",
                "control_command",
                "device",
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
        app_config.Config.ACTIVITY_LOG_RETENTION_ENABLED = cls._original_activity_log_retention_enabled
        app_config.Config.ACTIVITY_LOG_RETENTION_DAYS = cls._original_activity_log_retention_days
        app_config.Config.ACTIVITY_LOG_RETENTION_DRY_RUN = cls._original_activity_log_retention_dry_run

    def setUp(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
        with self.app.app_context():
            RecipeProgramEvent.query.delete()
            RecipeProgramRun.query.delete()
            ControlCommandEvent.query.delete()
            ControlCommand.query.delete()
            db.session.execute(text("DELETE FROM device_manual_state"))
            DeviceConnection.query.delete()
            DeviceServer.query.delete()
            Device.query.delete()
            db.session.commit()

    def _seed_activity(self):
        now = datetime.now(timezone.utc)
        device = Device(
            device_id=1,
            asset_serial="LOG-IKA-001",
            manufacturer_serial="LOG-SN-001",
            display_name="IKA Log Test",
            device_type="actuator",
            protocol="ika_eurostar_60",
            is_active=True,
        )
        command = ControlCommand(
            command_id=1,
            device_id=1,
            request_uuid="00000000-0000-0000-0000-000000000001",
            requested_by="operator",
            command_name="manual_text",
            command_payload={"text": "OUT_SP_4 500"},
            status="acked",
            requested_at=now - timedelta(minutes=5),
            sent_at=now - timedelta(minutes=5),
            ack_at=now - timedelta(minutes=4),
            finished_at=now - timedelta(minutes=4),
        )
        server = DeviceServer(
            device_server_id=1,
            server_code="MOXA-LOG",
            display_name="Moxa Log Test",
            host="192.0.2.10",
            port_count=8,
        )
        connection = DeviceConnection(
            connection_id=1,
            device_server_id=1,
            port_number=1,
            connection_label="Port 1",
            transport_type="tcp_socket",
            tcp_host="192.0.2.10",
            tcp_port=4001,
            last_error="connection refused",
            updated_at=now - timedelta(minutes=3),
        )
        run = RecipeProgramRun(
            recipe_program_run_id=1,
            status="running",
            requested_by="operator",
            recipe_title="Log Recipe",
            started_at=now - timedelta(minutes=2),
            last_progress_at=now - timedelta(minutes=2),
        )
        db.session.add_all([device, command, server, connection, run])
        db.session.flush()
        db.session.add_all(
            [
                ControlCommandEvent(
                    command_event_id=1,
                    command_id=1,
                    event_type="queued",
                    event_payload={"requested_by": "operator"},
                    created_at=now - timedelta(minutes=5),
                ),
                ControlCommandEvent(
                    command_event_id=2,
                    command_id=1,
                    event_type="sent",
                    event_payload={"sent_at": (now - timedelta(minutes=4, seconds=30)).isoformat()},
                    created_at=now - timedelta(minutes=4, seconds=30),
                ),
                ControlCommandEvent(
                    command_event_id=3,
                    command_id=1,
                    event_type="response",
                    event_payload={"response_text": "500.0 4"},
                    created_at=now - timedelta(minutes=4),
                ),
                RecipeProgramEvent(
                    recipe_program_event_id=1,
                    recipe_program_run_id=1,
                    event_type="started",
                    event_payload={},
                    created_at=now - timedelta(minutes=2),
                ),
            ]
        )
        db.session.commit()

    def test_activity_log_combines_command_recipe_and_connection_events(self):
        with self.app.app_context():
            self._seed_activity()

            items = load_activity_logs(days=7, limit=20)
            titles = [item.title for item in items]
            summary = summarize_activity_logs(items)

        self.assertNotIn("Command queued", titles)
        self.assertIn("Command sent", titles)
        self.assertIn("Device response", titles)
        self.assertIn("Recipe started", titles)
        self.assertIn("Connection error", titles)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["errors"], 1)
        self.assertGreaterEqual(summary["success"], 1)

    def test_activity_log_hides_internal_telemetry_poll_noise(self):
        now = datetime.now(timezone.utc)
        with self.app.app_context():
            device = Device(
                device_id=3,
                asset_serial="LOG-IKA-POLL",
                manufacturer_serial="LOG-POLL-SN",
                display_name="IKA Poll Test",
                device_type="actuator",
                protocol="ika_eurostar_60",
                is_active=True,
            )
            command = ControlCommand(
                command_id=3,
                device_id=3,
                request_uuid="00000000-0000-0000-0000-000000000003",
                requested_by="manual_reconciler",
                command_name="manual_text",
                command_payload={"text": "IN_PV_4"},
                status="acked",
                requested_at=now - timedelta(seconds=6),
                sent_at=now - timedelta(seconds=5),
                ack_at=now - timedelta(seconds=4),
                finished_at=now - timedelta(seconds=4),
            )
            db.session.add_all([device, command])
            db.session.flush()
            db.session.add_all(
                [
                    ControlCommandEvent(
                        command_event_id=30,
                        command_id=3,
                        event_type="queued",
                        event_payload={"requested_by": "manual_reconciler"},
                        created_at=now - timedelta(seconds=6),
                    ),
                    ControlCommandEvent(
                        command_event_id=31,
                        command_id=3,
                        event_type="sent",
                        event_payload={},
                        created_at=now - timedelta(seconds=5),
                    ),
                    ControlCommandEvent(
                        command_event_id=32,
                        command_id=3,
                        event_type="response",
                        event_payload={"response_text": "123.0 4"},
                        created_at=now - timedelta(seconds=4),
                    ),
                ]
            )
            db.session.commit()

            items = load_activity_logs(days=7, limit=20)

        self.assertEqual(items, [])

    def test_logs_route_replaces_alerts_tab(self):
        with self.app.app_context():
            self._seed_activity()

        response = self.client.get("/logs")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>Logs</h1>", html)
        self.assertIn("OUT_SP_4 500", html)
        self.assertIn("Log Recipe", html)
        self.assertIn("connection refused", html)
        self.assertIn('id="logs-table-body"', html)
        self.assertIn("js/logs.js", html)

    def test_logs_api_returns_filtered_live_payload(self):
        with self.app.app_context():
            self._seed_activity()

        response = self.client.get("/api/logs?limit=20")
        payload = response.get_json()
        titles = [item["title"] for item in payload["items"]]

        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", payload)
        self.assertIn("event_key", payload["items"][0])
        self.assertNotIn("Command queued", titles)
        self.assertIn("Command sent", titles)
        self.assertIn("Device response", titles)

    def test_alerts_route_redirects_to_logs(self):
        response = self.client.get("/alerts")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/logs", response.headers["Location"])

    def test_activity_log_retention_dry_run_counts_old_rows(self):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        with self.app.app_context():
            device = Device(
                device_id=2,
                asset_serial="LOG-OLD-001",
                manufacturer_serial="LOG-OLD-SN-001",
                display_name="Old Device",
                device_type="actuator",
                protocol="ika_eurostar_60",
                is_active=True,
            )
            command = ControlCommand(
                command_id=2,
                device_id=2,
                request_uuid="00000000-0000-0000-0000-000000000002",
                requested_by="operator",
                command_name="manual_text",
                command_payload={"text": "STOP_4"},
                status="acked",
                requested_at=old,
            )
            run = RecipeProgramRun(
                recipe_program_run_id=2,
                status="completed",
                requested_by="operator",
                recipe_title="Old Recipe",
                started_at=old,
                finished_at=old,
                last_progress_at=old,
            )
            db.session.add_all([device, command, run])
            db.session.flush()
            db.session.add_all(
                [
                    ControlCommandEvent(
                        command_event_id=10,
                        command_id=2,
                        event_type="queued",
                        event_payload={},
                        created_at=old,
                    ),
                    RecipeProgramEvent(
                        recipe_program_event_id=2,
                        recipe_program_run_id=2,
                        event_type="completed",
                        event_payload={},
                        created_at=old,
                    ),
                ]
            )
            db.session.commit()

            self.app.config["ACTIVITY_LOG_RETENTION_DRY_RUN"] = True
            result = run_activity_log_retention(self.app)
            self.app.config["ACTIVITY_LOG_RETENTION_DRY_RUN"] = False

        self.assertTrue(result.dry_run)
        self.assertEqual(result.rows_deleted, 4)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
