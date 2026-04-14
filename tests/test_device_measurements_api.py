import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import Device, Measurement


class DeviceMeasurementsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "measurements_api.sqlite"
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
            device = Device(
                asset_serial="TEST-DEVICE-001",
                manufacturer_serial="SN-001",
                display_name="Test Sensor",
                device_type="sensor",
                protocol="generic_text",
            )
            db.session.add(device)
            db.session.commit()
            cls.device_id = device.device_id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.session.execute(text("DROP TABLE IF EXISTS measurement"))
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
            db.session.commit()

    def _insert_measurement(self, *, minutes_ago: int, value: float, channel_code: str = "temp") -> None:
        measured_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        with self.app.app_context():
            db.session.add(
                Measurement(
                    device_id=self.device_id,
                    channel_code=channel_code,
                    measured_at=measured_at,
                    numeric_value=value,
                    unit="C",
                    source="poller",
                )
            )
            db.session.commit()

    def test_measurements_endpoint_filters_by_since_minutes(self):
        self._insert_measurement(minutes_ago=10, value=22.5)
        self._insert_measurement(minutes_ago=90, value=21.0)
        self._insert_measurement(minutes_ago=24 * 60, value=20.0)

        response = self.client.get(
            f"/api/devices/{self.device_id}/measurements?channel_code=temp&since_minutes=60&max_points=50"
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        items = payload["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["numeric_value"], 22.5)

    def test_measurements_endpoint_downsamples_when_max_points_is_set(self):
        for index in range(12):
            self._insert_measurement(minutes_ago=(12 - index) * 10, value=float(index), channel_code="pressure")

        response = self.client.get(
            f"/api/devices/{self.device_id}/measurements?channel_code=pressure&since_minutes=180&max_points=4"
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        items = payload["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["numeric_value"], 0.0)
        self.assertEqual(items[-1]["numeric_value"], 11.0)

    def test_measurements_endpoint_rejects_invalid_since_minutes(self):
        response = self.client.get(
            f"/api/devices/{self.device_id}/measurements?channel_code=temp&since_minutes=0"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("since_minutes", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
