import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import Device, DeviceManualState, Measurement, MeasurementChannel, RecipeProgramState
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
        cls._original_background_huber_enabled = app_config.Config.MEASUREMENT_POLLER_BACKGROUND_HUBER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "device_manual_measurements.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False
        app_config.Config.MEASUREMENT_POLLER_BACKGROUND_HUBER_ENABLED = False

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
                        active_control_sensor TEXT,
                        reported_extra TEXT,
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
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_state (
                        recipe_program_state_id INTEGER PRIMARY KEY,
                        recipe_id INTEGER,
                        reactor_build_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'idle',
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        recipe_title TEXT,
                        operator_name TEXT,
                        snapshot_json TEXT,
                        last_applied_targets_json TEXT,
                        active_step_index INTEGER NOT NULL DEFAULT 0,
                        step_started_at TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        last_progress_at TEXT,
                        stop_requested INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.session.execute(text("DROP TABLE IF EXISTS recipe_program_state"))
            db.session.execute(text("DROP TABLE IF EXISTS measurement"))
            db.session.execute(text("DROP TABLE IF EXISTS measurement_channel"))
            db.session.execute(text("DROP TABLE IF EXISTS device_manual_state"))
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
        app_config.Config.MEASUREMENT_POLLER_BACKGROUND_HUBER_ENABLED = cls._original_background_huber_enabled

    def setUp(self):
        with self.app.app_context():
            RecipeProgramState.query.delete()
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
            self.assertTrue(all(item.source == "poller" for item in measurements))

    def test_measurement_failure_does_not_poison_live_manual_state_update(self):
        with self.app.app_context():
            device = Device(
                asset_serial="IKA-PERSIST-FAIL-001",
                manufacturer_serial="SN-PERSIST-FAIL-001",
                display_name="IKA Persist Failure Test",
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

            with patch.object(
                device_manual_runtime,
                "_read_ika_status",
                return_value={"setpoint_rpm": 612.0, "actual_rpm": 608.77, "torque_ncm": 1.4},
            ):
                with patch.object(
                    device_manual_runtime,
                    "_persist_ika_telemetry_as_measurements",
                    side_effect=RuntimeError("simulated measurement write failure"),
                ):
                    device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="worker-1")

            state = db.session.get(DeviceManualState, device.device_id)
            self.assertEqual(state.queue_status, "idle")
            self.assertIsNone(state.lease_owner)
            self.assertIsNone(state.last_error)
            self.assertEqual(state.reported_setpoint_rpm, 612)
            self.assertAlmostEqual(state.actual_rpm, 608.77)
            self.assertEqual(Measurement.query.count(), 0)

    def test_active_ika_discovery_seeds_manual_state_and_measurement_channels(self):
        with self.app.app_context():
            device = Device(
                asset_serial="IKA-DISCOVERY-001",
                manufacturer_serial="SN-DISCOVERY-001",
                display_name="IKA Discovery Test",
                device_type="actuator",
                protocol="ika_eurostar_60",
                is_active=True,
            )
            db.session.add(device)
            db.session.commit()

            device_manual_runtime._ensure_manual_states_for_active_ika_devices(self.app)

            state = db.session.get(DeviceManualState, device.device_id)
            channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )

            self.assertIsNotNone(state)
            self.assertEqual(
                [channel.channel_code for channel in channels],
                ["ika_actual_rpm", "ika_setpoint_rpm", "ika_torque_ncm"],
            )

    def test_active_ika_discovery_backfills_measurement_channels_for_existing_manual_state(self):
        with self.app.app_context():
            device = Device(
                asset_serial="IKA-DISCOVERY-002",
                manufacturer_serial="SN-DISCOVERY-002",
                display_name="IKA Discovery Existing State Test",
                device_type="actuator",
                protocol="ika_eurostar_60",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(
                DeviceManualState(
                    device_id=device.device_id,
                    queue_status="idle",
                    desired_version=0,
                    applied_version=0,
                )
            )
            db.session.commit()

            device_manual_runtime._ensure_manual_states_for_active_ika_devices(self.app)

            channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )
            self.assertEqual(
                [channel.channel_code for channel in channels],
                ["ika_actual_rpm", "ika_setpoint_rpm", "ika_torque_ncm"],
            )

    def test_active_huber_discovery_seeds_manual_state_and_measurement_channels(self):
        with self.app.app_context():
            device = Device(
                asset_serial="HUBER-DISCOVERY-001",
                manufacturer_serial="SN-HUBER-001",
                display_name="Huber Discovery Test",
                device_type="thermostat",
                protocol="huber_unistat_430",
                is_active=True,
            )
            db.session.add(device)
            db.session.commit()

            device_manual_runtime._ensure_manual_states_for_active_devices(self.app)

            state = db.session.get(DeviceManualState, device.device_id)
            channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )

            self.assertIsNotNone(state)
            self.assertEqual(
                [channel.channel_code for channel in channels],
                ["actual_temp_C", "external_temp_C", "setpoint_C"],
            )

    def test_active_ics435_discovery_seeds_manual_state_and_weight_channel(self):
        with self.app.app_context():
            device = Device(
                asset_serial="ICS435-DISCOVERY-001",
                manufacturer_serial="SN-ICS435-001",
                display_name="ICS435 Discovery Test",
                device_type="scale",
                protocol="mettler_toledo_ics435",
                is_active=True,
            )
            db.session.add(device)
            db.session.commit()

            device_manual_runtime._ensure_manual_states_for_active_devices(self.app)

            state = db.session.get(DeviceManualState, device.device_id)
            channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )

            self.assertIsNotNone(state)
            self.assertEqual(
                [(channel.channel_code, channel.display_name, channel.value_type) for channel in channels],
                [("weight", "Weight", "float")],
            )

    def test_ics435_telemetry_persists_weight_quality_and_raw_payload(self):
        with self.app.app_context():
            device = Device(
                asset_serial="ICS435-PERSIST-001",
                manufacturer_serial="SN-ICS435-PERSIST-001",
                display_name="ICS435 Persist Test",
                device_type="scale",
                protocol="mettler_toledo_ics435",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            measured_at = datetime.now(timezone.utc)

            device_manual_runtime._persist_scale_telemetry_as_measurements(
                device,
                {
                    "weight": 12.34,
                    "weight_unit": "g",
                    "weight_quality_score": 1.0,
                    "weight_raw_payload": {
                        "value_decimal": "12.34",
                        "unit": "g",
                        "stable": True,
                        "raw_response": "S S      12.34 g",
                    },
                },
                measured_at,
            )
            db.session.commit()

            channel = MeasurementChannel.query.filter_by(device_id=device.device_id, channel_code="weight").one()
            measurement = Measurement.query.filter_by(device_id=device.device_id, channel_code="weight").one()

            self.assertEqual(channel.unit, "g")
            self.assertEqual(measurement.numeric_value, 12.34)
            self.assertEqual(float(measurement.quality_score), 1.0)
            self.assertEqual(measurement.raw_payload["stable"], True)

    def test_scale_manual_state_caches_weight_without_a_live_device_read(self):
        # Regression test for the tab-switch slowdown: the reconciler already
        # reads the scale's weight every poll cycle for measurement
        # persistence. It must ALSO cache that value on DeviceManualState so
        # the Process view's periodic UI refresh can read a fast DB value
        # instead of issuing its own live device command (which would
        # contend with this same poll cycle for the per-device lock).
        with self.app.app_context():
            device = Device(
                asset_serial="ICS435-CACHE-001",
                manufacturer_serial="SN-ICS435-CACHE-001",
                display_name="ICS435 Cache Test",
                device_type="scale",
                protocol="mettler_toledo_ics435",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(
                DeviceManualState(
                    device_id=device.device_id,
                    queue_status="idle",
                    desired_version=0,
                    applied_version=0,
                )
            )
            db.session.commit()

            measured_at = datetime.now(timezone.utc)
            telemetry = {
                "weight": 12.34,
                "weight_unit": "g",
                "weight_stable": True,
                "weight_quality_score": 1.0,
                "weight_raw_payload": {"raw_response": "S S      12.34 g"},
            }

            with self.app.app_context():
                device_manual_runtime._commit_scale_manual_state_success(
                    self.app,
                    device_id=device.device_id,
                    telemetry=telemetry,
                    measured_at=measured_at,
                    watch_active=False,
                    bg_interval=timedelta(seconds=1),
                )
                db.session.commit()

                state = db.session.get(DeviceManualState, device.device_id)
                self.assertIsNotNone(state.reported_extra)
                self.assertEqual(state.reported_extra["kind"], "scale")
                self.assertEqual(state.reported_extra["weight"], 12.34)
                self.assertEqual(state.reported_extra["unit"], "g")
                self.assertTrue(state.reported_extra["stable"])

                snapshot = device_manual_runtime.manual_state_to_dict(state)
                self.assertEqual(snapshot["reported_extra"]["weight"], 12.34)

    def test_cc230_discovery_and_measurement_persistence_only_include_active_temperature_channels(self):
        with self.app.app_context():
            device = Device(
                asset_serial="CC230-DISCOVERY-001",
                manufacturer_serial="SN-CC230-001",
                display_name="CC230 Discovery Test",
                device_type="thermostat",
                protocol="huber_cc230",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            for code, value_type in (
                ("actual_temp_C", "float"),
                ("bath_temp_C", "float"),
                ("cc230_error", "text"),
                ("cc230_status", "text"),
                ("cc230_warning", "text"),
            ):
                db.session.add(
                    MeasurementChannel(
                        device_id=device.device_id,
                        channel_code=code,
                        display_name=code,
                        unit="",
                        value_type=value_type,
                        is_active=True,
                    )
                )
            db.session.commit()

            device_manual_runtime._ensure_manual_states_for_active_devices(self.app)

            state = db.session.get(DeviceManualState, device.device_id)
            channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )
            active_channels = [channel for channel in channels if bool(channel.is_active)]
            inactive_channel_codes = [channel.channel_code for channel in channels if not bool(channel.is_active)]

            self.assertIsNotNone(state)
            self.assertEqual(
                [(channel.channel_code, channel.value_type) for channel in active_channels],
                [
                    ("external_temp_C", "float"),
                    ("internal_temp_C", "float"),
                    ("setpoint_C", "float"),
                ],
            )
            self.assertEqual(
                inactive_channel_codes,
                ["actual_temp_C", "bath_temp_C", "cc230_error", "cc230_status", "cc230_warning"],
            )

            measured_at = datetime.now(timezone.utc)
            device_manual_runtime._persist_huber_telemetry_as_measurements(
                device,
                {
                    "setpoint_C": 25.0,
                    "actual_temp_C": 24.8,
                    "bath_temp_C": 24.7,
                    "internal_temp_C": 24.9,
                    "external_temp_C": 24.6,
                    "status": "ON REMOTE",
                    "error": "0",
                    "warning": "WARN 0",
                },
                measured_at,
            )
            db.session.commit()

            measurements = Measurement.query.order_by(Measurement.channel_code.asc()).all()
            self.assertEqual(
                [(item.channel_code, item.numeric_value, item.text_value) for item in measurements],
                [
                    ("external_temp_C", 24.6, None),
                    ("internal_temp_C", 24.9, None),
                    ("setpoint_C", 25.0, None),
                ],
            )

    def test_data_overview_queries_hide_inactive_channel_rows(self):
        from reactor_app import web as web_module

        with self.app.app_context():
            device = Device(
                asset_serial="DATA-FILTER-CC230-001",
                manufacturer_serial="SN-DATA-FILTER-001",
                display_name="Data Filter CC230",
                device_type="thermostat",
                protocol="huber_cc230",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            active_channel = MeasurementChannel(
                device_id=device.device_id,
                channel_code="setpoint_C",
                display_name="Setpoint",
                unit="degC",
                value_type="float",
                is_active=True,
            )
            inactive_channel = MeasurementChannel(
                device_id=device.device_id,
                channel_code="actual_temp_C",
                display_name="Actual Temperature",
                unit="degC",
                value_type="float",
                is_active=False,
            )
            db.session.add_all([active_channel, inactive_channel])
            db.session.flush()
            measured_at = datetime.now(timezone.utc)
            db.session.add_all(
                [
                    Measurement(
                        device_id=device.device_id,
                        channel_id=active_channel.channel_id,
                        channel_code="setpoint_C",
                        measured_at=measured_at,
                        numeric_value=25.0,
                        unit="degC",
                        source="poller",
                    ),
                    Measurement(
                        device_id=device.device_id,
                        channel_id=inactive_channel.channel_id,
                        channel_code="actual_temp_C",
                        measured_at=measured_at,
                        numeric_value=24.8,
                        unit="degC",
                        source="poller",
                    ),
                ]
            )
            db.session.commit()

            summary = web_module._load_data_summary()
            channel_stats = web_module._load_channel_stats()

            self.assertEqual(summary["total_count"], 1)
            self.assertEqual(summary["channel_count"], 1)
            self.assertEqual([row.channel_code for row in channel_stats], ["setpoint_C"])

    def test_background_huber_polling_is_claimed_without_watch_or_recipe(self):
        # Huber devices are always eligible for background polling (same as IKA),
        # so a device with stale last_reported_at must be claimed.
        with self.app.app_context():
            device = Device(
                asset_serial="HUBER-IDLE-001",
                manufacturer_serial="SN-HUBER-IDLE-001",
                display_name="Idle Huber",
                device_type="thermostat",
                protocol="huber_unistat_430",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(
                DeviceManualState(
                    device_id=device.device_id,
                    queue_status="idle",
                    desired_version=0,
                    applied_version=0,
                )
            )
            db.session.commit()

            claimed = device_manual_runtime._claim_next_device_id(self.app, "worker-1")

            self.assertEqual(claimed, device.device_id)

    def test_running_recipe_limits_polling_to_recipe_bound_devices(self):
        with self.app.app_context():
            recipe_device = Device(
                asset_serial="HUBER-RECIPE-001",
                manufacturer_serial="SN-HUBER-RECIPE-001",
                display_name="Recipe Huber",
                device_type="thermostat",
                protocol="huber_unistat_430",
                is_active=True,
            )
            unused_device = Device(
                asset_serial="HUBER-UNUSED-001",
                manufacturer_serial="SN-HUBER-UNUSED-001",
                display_name="Unused Huber",
                device_type="thermostat",
                protocol="huber_unistat_430",
                is_active=True,
            )
            db.session.add_all([recipe_device, unused_device])
            db.session.flush()
            db.session.add_all(
                [
                    DeviceManualState(
                        device_id=recipe_device.device_id,
                        queue_status="idle",
                        desired_version=0,
                        applied_version=0,
                    ),
                    DeviceManualState(
                        device_id=unused_device.device_id,
                        queue_status="idle",
                        desired_version=0,
                        applied_version=0,
                    ),
                    RecipeProgramState(
                        recipe_program_state_id=1,
                        status="running",
                        snapshot_json={"bindings": [{"device_id": recipe_device.device_id}]},
                    ),
                ]
            )
            db.session.commit()

            claimed = device_manual_runtime._claim_next_device_id(self.app, "worker-1")

            self.assertEqual(claimed, recipe_device.device_id)

    # ------------------------------------------------------------------ #
    # CC230 partial telemetry persistence                                 #
    # ------------------------------------------------------------------ #

    def test_cc230_partial_telemetry_persists_available_channels_skips_none(self):
        # external_temp_C = None (no external sensor) must not produce a row,
        # but internal_temp_C and setpoint_C must be written.
        with self.app.app_context():
            device = Device(
                asset_serial="CC230-PARTIAL-001",
                manufacturer_serial="SN-CC230-PARTIAL-001",
                display_name="CC230 Partial Telemetry Test",
                device_type="thermostat",
                protocol="huber_cc230",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(DeviceManualState(
                device_id=device.device_id,
                queue_status="idle",
                desired_version=0,
                applied_version=0,
            ))
            db.session.commit()

            device_manual_runtime._ensure_huber_measurement_channels(device)
            db.session.commit()

            measured_at = datetime.now(timezone.utc)
            device_manual_runtime._persist_huber_telemetry_as_measurements(
                device,
                {
                    "setpoint_C": 25.0,
                    "internal_temp_C": 24.5,
                    "external_temp_C": None,  # sensor not connected
                },
                measured_at,
            )
            db.session.commit()

            measurements = (
                Measurement.query
                .filter(Measurement.device_id == device.device_id)
                .order_by(Measurement.channel_code.asc())
                .all()
            )
            channel_codes = [m.channel_code for m in measurements]
            self.assertIn("setpoint_C", channel_codes)
            self.assertIn("internal_temp_C", channel_codes)
            self.assertNotIn("external_temp_C", channel_codes)
            self.assertEqual(len(measurements), 2)

    def test_cc230_ensure_channels_creates_active_channels_and_deactivates_obsolete(self):
        # Even when no channels exist yet, _ensure_huber_measurement_channels must
        # create the three active ones and deactivate any pre-existing obsolete ones.
        with self.app.app_context():
            device = Device(
                asset_serial="CC230-ENSURE-001",
                manufacturer_serial="SN-CC230-ENSURE-001",
                display_name="CC230 Ensure Channels Test",
                device_type="thermostat",
                protocol="huber_cc230",
                is_active=True,
            )
            db.session.add(device)
            db.session.flush()
            # Seed obsolete channels as if migration has not run yet.
            for code in ("actual_temp_C", "bath_temp_C", "cc230_error"):
                db.session.add(MeasurementChannel(
                    device_id=device.device_id,
                    channel_code=code,
                    display_name=code,
                    unit="",
                    value_type="float",
                    is_active=True,
                ))
            db.session.commit()

            channels = device_manual_runtime._ensure_huber_measurement_channels(device)
            db.session.commit()

            all_channels = (
                MeasurementChannel.query
                .filter(MeasurementChannel.device_id == device.device_id)
                .order_by(MeasurementChannel.channel_code.asc())
                .all()
            )
            active = [c.channel_code for c in all_channels if bool(c.is_active)]
            inactive = [c.channel_code for c in all_channels if not bool(c.is_active)]

            self.assertCountEqual(active, ["external_temp_C", "internal_temp_C", "setpoint_C"])
            self.assertCountEqual(inactive, ["actual_temp_C", "bath_temp_C", "cc230_error"])
            # Return value must contain the three active channels.
            self.assertCountEqual(list(channels.keys()), ["external_temp_C", "internal_temp_C", "setpoint_C"])


if __name__ == "__main__":
    unittest.main()
