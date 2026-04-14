import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import Device, DeviceManualState, Measurement, MeasurementChannel
from reactor_app.services import device_manual_runtime


class DeviceManualMeasurementPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "device_manual_measurements.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False

        cls.app = create_app()

        with cls.app.app_context():
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
                        updated_at TEXT
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
            db.session.execute(
                text(
                    """
                    CREATE TABLE measurement (
                        measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.session.execute(text("DROP TABLE IF EXISTS measurement"))
            db.session.execute(text("DROP TABLE IF EXISTS measurement_channel"))
            db.session.execute(text("DROP TABLE IF EXISTS device_manual_state"))
            db.session.execute(text("DROP TABLE IF EXISTS device"))
            db.session.commit()

        cls._tmpdir.cleanup()
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = cls._original_manual_reconciler_enabled
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = cls._original_program_reconciler_enabled

    def setUp(self):
        with self.app.app_context():
            Measurement.query.delete()
            MeasurementChannel.query.delete()
            DeviceManualState.query.delete()
            Device.query.delete()
            db.session.commit()

    def test_process_manual_state_persists_telemetry_measurements_and_reuses_channels(self):
        with self.app.app_context():
            device = Device(
                asset_serial="IKA-PERSIST-001",
                manufacturer_serial="SN-PERSIST-001",
                display_name="IKA Persist Test",
                device_type="actuator",
                protocol="ika_eurostar_60",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()

            state = DeviceManualState(
                device_id=device.device_id,
                queue_status="running",
                desired_version=0,
                applied_version=0,
                lease_owner="worker-1",
            )
            state.watch_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
            state.next_poll_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.session.add(state)
            db.session.commit()

            telemetry_samples = iter(
                [
                    {"setpoint_rpm": 300.0, "actual_rpm": 280.0, "torque_ncm": 1.2},
                    {"setpoint_rpm": 320.0, "actual_rpm": 300.0, "torque_ncm": 1.5},
                ]
            )

            def fake_read_status(_device):
                return next(telemetry_samples)

            with patch.object(device_manual_runtime, "_read_ika_status", fake_read_status):
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="worker-1")

            state = db.session.get(DeviceManualState, device.device_id)
            state.queue_status = "running"
            state.lease_owner = "worker-1"
            state.watch_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
            state.next_poll_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.session.commit()

            with patch.object(device_manual_runtime, "_read_ika_status", fake_read_status):
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="worker-1")

            channels = MeasurementChannel.query.order_by(MeasurementChannel.channel_code.asc()).all()
            measurements = (
                Measurement.query.order_by(
                    Measurement.channel_code.asc(),
                    Measurement.measured_at.asc(),
                    Measurement.measurement_id.asc(),
                ).all()
            )

            self.assertEqual(
                [channel.channel_code for channel in channels],
                ["ika_actual_rpm", "ika_setpoint_rpm", "ika_torque_ncm"],
            )
            self.assertEqual(len(measurements), 6)
            self.assertEqual(
                [item.numeric_value for item in measurements if item.channel_code == "ika_actual_rpm"],
                [280.0, 300.0],
            )
            self.assertEqual(
                [item.numeric_value for item in measurements if item.channel_code == "ika_setpoint_rpm"],
                [300.0, 320.0],
            )
            self.assertEqual(
                [item.numeric_value for item in measurements if item.channel_code == "ika_torque_ncm"],
                [1.2, 1.5],
            )


if __name__ == "__main__":
    unittest.main()
