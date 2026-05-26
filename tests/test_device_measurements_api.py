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
            second_device = Device(
                asset_serial="TEST-DEVICE-002",
                manufacturer_serial="SN-002",
                display_name="Second Sensor",
                device_type="sensor",
                protocol="generic_text",
            )
            db.session.add(second_device)
            db.session.commit()
            cls.device_id = device.device_id
            cls.second_device_id = second_device.device_id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.session.execute(text("DROP TABLE IF EXISTS measurement"))
            db.session.execute(text("DROP TABLE IF EXISTS device"))
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
            Measurement.query.delete()
            db.session.commit()

    def _insert_measurement_at(
        self,
        *,
        measured_at: datetime,
        value: float,
        channel_code: str = "temp",
        device_id: int | None = None,
    ) -> None:
        with self.app.app_context():
            db.session.add(
                Measurement(
                    device_id=device_id or self.device_id,
                    channel_code=channel_code,
                    measured_at=measured_at,
                    numeric_value=value,
                    unit="C",
                    source="poller",
                )
            )
            db.session.commit()

    def _insert_measurement(self, *, minutes_ago: int, value: float, channel_code: str = "temp") -> None:
        measured_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self._insert_measurement_at(measured_at=measured_at, value=value, channel_code=channel_code)

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
        self.assertLessEqual(len(items), 4)
        self.assertGreaterEqual(len(items), 3)
        self.assertEqual(items[-1]["numeric_value"], 11.0)

    def test_measurements_endpoint_rejects_invalid_since_minutes(self):
        response = self.client.get(
            f"/api/devices/{self.device_id}/measurements?channel_code=temp&since_minutes=0"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("since_minutes", response.get_json()["error"])

    def test_plot_series_endpoint_returns_multiple_channels_in_one_response(self):
        self._insert_measurement(minutes_ago=55, value=22.0, channel_code="temp")
        self._insert_measurement(minutes_ago=25, value=23.5, channel_code="temp")
        self._insert_measurement(minutes_ago=40, value=1.2, channel_code="pressure")
        self._insert_measurement(minutes_ago=5, value=1.5, channel_code="pressure")

        response = self.client.get(
            f"/api/devices/{self.device_id}/plot-series?channel_code=temp&channel_code=pressure&since_minutes=60&max_points=20"
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        series = payload["series"]
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]["channel_code"], "temp")
        self.assertEqual(series[1]["channel_code"], "pressure")
        self.assertEqual(len(series[0]["items"]), 2)
        self.assertEqual(len(series[1]["items"]), 2)

    def test_plot_series_endpoint_downsamples_bucketed_points(self):
        for index in range(18):
            self._insert_measurement(minutes_ago=(18 - index) * 5, value=float(index), channel_code="rpm")

        response = self.client.get(
            f"/api/devices/{self.device_id}/plot-series?channel_code=rpm&since_minutes=180&max_points=6"
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        series = payload["series"]
        self.assertEqual(len(series), 1)
        self.assertLessEqual(len(series[0]["items"]), 7)
        self.assertGreaterEqual(len(series[0]["items"]), 4)

    def test_plot_series_endpoint_uses_explicit_window_end_for_all_points(self):
        window_end = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        self._insert_measurement_at(measured_at=window_end - timedelta(minutes=10), value=42.0)
        self._insert_measurement_at(measured_at=window_end + timedelta(seconds=1), value=99.0)
        self._insert_measurement_at(measured_at=window_end - timedelta(minutes=70), value=12.0)

        response = self.client.get(
            f"/api/devices/{self.device_id}/plot-series",
            query_string={
                "channel_code": "temp",
                "since_minutes": "60",
                "max_points": "20",
                "window_end": window_end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["window_end"], window_end.isoformat())
        self.assertEqual(payload["window_start"], (window_end - timedelta(minutes=60)).isoformat())
        self.assertIn("bucket_seconds", payload)
        items = payload["series"][0]["items"]
        self.assertEqual([item["numeric_value"] for item in items], [42.0])
        self.assertEqual(payload["series"][0]["latest_measurement_at"], (window_end - timedelta(minutes=10)).isoformat())

    def test_plot_series_endpoint_rejects_invalid_window_end(self):
        response = self.client.get(
            f"/api/devices/{self.device_id}/plot-series?channel_code=temp&since_minutes=60&max_points=20&window_end=not-a-date"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("window_end", response.get_json()["error"])

    def test_live_plot_series_endpoint_batches_multiple_devices_with_shared_window(self):
        window_end = datetime(2026, 5, 20, 13, 0, 0, tzinfo=timezone.utc)
        self._insert_measurement_at(
            measured_at=window_end - timedelta(seconds=30),
            value=101.0,
            channel_code="rpm",
            device_id=self.device_id,
        )
        self._insert_measurement_at(
            measured_at=window_end - timedelta(seconds=20),
            value=25.5,
            channel_code="temp",
            device_id=self.second_device_id,
        )
        self._insert_measurement_at(
            measured_at=window_end + timedelta(seconds=1),
            value=999.0,
            channel_code="temp",
            device_id=self.second_device_id,
        )

        response = self.client.get(
            "/api/plot-series/live",
            query_string=[
                ("series", f"{self.device_id}:rpm"),
                ("series", f"{self.second_device_id}:temp"),
                ("since_minutes", "5"),
                ("max_points", "120"),
                ("window_end", window_end.isoformat()),
                ("cache_seconds", "1"),
            ],
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["window_start"], (window_end - timedelta(minutes=5)).isoformat())
        self.assertEqual(payload["window_end"], window_end.isoformat())
        self.assertEqual(payload["cache_seconds"], 1.0)
        self.assertFalse(payload["cache_hit"])
        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(payload["series"][0]["device_id"], self.device_id)
        self.assertEqual(payload["series"][0]["channel_code"], "rpm")
        self.assertEqual(payload["series"][0]["items"][0]["numeric_value"], 101.0)
        self.assertEqual(payload["series"][1]["device_id"], self.second_device_id)
        self.assertEqual(payload["series"][1]["channel_code"], "temp")
        self.assertEqual([item["numeric_value"] for item in payload["series"][1]["items"]], [25.5])

    def test_live_plot_series_endpoint_accepts_seconds_window(self):
        window_end = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_measurement_at(
            measured_at=window_end - timedelta(seconds=20),
            value=101.0,
            channel_code="rpm",
            device_id=self.device_id,
        )
        self._insert_measurement_at(
            measured_at=window_end - timedelta(seconds=70),
            value=99.0,
            channel_code="rpm",
            device_id=self.device_id,
        )

        response = self.client.get(
            "/api/plot-series/live",
            query_string=[
                ("series", f"{self.device_id}:rpm"),
                ("since_seconds", "30"),
                ("max_points", "120"),
                ("window_end", window_end.isoformat()),
                ("cache_seconds", "0"),
            ],
        )
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["since_seconds"], 30)
        self.assertEqual(payload["window_start"], (window_end - timedelta(seconds=30)).isoformat())
        self.assertEqual([item["numeric_value"] for item in payload["series"][0]["items"]], [101.0])

    def test_live_plot_series_endpoint_rejects_missing_series(self):
        response = self.client.get("/api/plot-series/live?since_minutes=5&max_points=20")
        self.assertEqual(response.status_code, 400)
        self.assertIn("series", response.get_json()["error"])

    def test_plot_series_endpoint_rejects_missing_channel_code(self):
        response = self.client.get(
            f"/api/devices/{self.device_id}/plot-series?since_minutes=60&max_points=20"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("channel_code", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
