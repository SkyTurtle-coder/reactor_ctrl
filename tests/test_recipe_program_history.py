import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import (
    Device,
    DeviceBindingCurrent,
    DeviceConnection,
    DeviceServer,
    ReactorBuild,
    Recipe,
    RecipeProgramEvent,
    RecipeProgramRun,
    RecipeProgramState,
)
from reactor_app.services import recipe_program_runtime


class RecipeProgramHistoryPersistenceTests(unittest.TestCase):
    @staticmethod
    def _naive_utc(value: datetime) -> datetime:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "recipe_program_history.sqlite"
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
                    CREATE TABLE reactor_build (
                        reactor_build_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        build_name TEXT NOT NULL,
                        build_date TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        updated_by TEXT,
                        definition_json TEXT NOT NULL,
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
                    CREATE TABLE recipe (
                        recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        operator_name TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'draft',
                        reactor_build_id INTEGER,
                        steps_json TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        updated_by TEXT,
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
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_run (
                        recipe_program_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipe_id INTEGER,
                        reactor_build_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'running',
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        recipe_title TEXT,
                        operator_name TEXT,
                        snapshot_json TEXT,
                        started_at TEXT NOT NULL,
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
                        recipe_program_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            db.session.execute(text("DROP TABLE IF EXISTS recipe_program_event"))
            db.session.execute(text("DROP TABLE IF EXISTS recipe_program_run"))
            db.session.execute(text("DROP TABLE IF EXISTS recipe_program_state"))
            db.session.execute(text("DROP TABLE IF EXISTS recipe"))
            db.session.execute(text("DROP TABLE IF EXISTS reactor_build"))
            db.session.execute(text("DROP TABLE IF EXISTS device_manual_state"))
            db.session.execute(text("DROP TABLE IF EXISTS device_binding_current"))
            db.session.execute(text("DROP TABLE IF EXISTS device"))
            db.session.execute(text("DROP TABLE IF EXISTS device_connection"))
            db.session.execute(text("DROP TABLE IF EXISTS device_server"))
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
            db.session.execute(text("DELETE FROM recipe_program_event"))
            db.session.execute(text("DELETE FROM recipe_program_run"))
            db.session.execute(text("DELETE FROM recipe_program_state"))
            db.session.execute(text("DELETE FROM recipe"))
            db.session.execute(text("DELETE FROM reactor_build"))
            db.session.execute(text("DELETE FROM device_manual_state"))
            db.session.execute(text("DELETE FROM device_binding_current"))
            db.session.execute(text("DELETE FROM device"))
            db.session.execute(text("DELETE FROM device_connection"))
            db.session.execute(text("DELETE FROM device_server"))
            db.session.commit()

    def _seed_recipe(self, *, steps_json: list[dict]) -> Recipe:
        server = DeviceServer(
            server_code="MOXA-01",
            display_name="Moxa Test",
            host="127.0.0.1",
        )
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
            asset_serial="IKA-001",
            manufacturer_serial="SN-IKA-001",
            display_name="IKA Stirrer",
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

        build = ReactorBuild(
            build_name="History Test Build",
            build_date=date(2026, 4, 14),
            created_by="tester",
            updated_by="tester",
            definition_json={
                "canvas": {"width": 1200, "height": 800},
                "nodes": [
                    {
                        "id": "node-motor-1",
                        "instance_id": "Stirrer_01",
                        "label": "Stirrer 1",
                        "symbol_id": "motor",
                        "category": "actuators",
                        "communication": {
                            "device_server_code": "MOXA-01",
                            "connection_label": "Port 1",
                            "protocol": "ika_eurostar_60",
                        },
                        "control": {
                            "profile_id": "motor_rpm",
                            "config": {},
                        },
                    }
                ],
                "edges": [],
            },
            is_active=True,
        )
        db.session.add(build)
        db.session.flush()

        recipe = Recipe(
            title="History Test Recipe",
            operator_name="Operator",
            status="released",
            reactor_build_id=build.reactor_build_id,
            steps_json=steps_json,
            created_by="tester",
            updated_by="tester",
            is_active=True,
        )
        db.session.add(recipe)
        db.session.commit()
        return recipe

    def _acquire_program_lease(self, *, worker_id: str, lease_until: datetime) -> None:
        state = db.session.get(RecipeProgramState, 1)
        state.lease_owner = worker_id
        state.lease_expires_at = lease_until
        db.session.commit()

    def test_start_creates_persistent_run_and_started_event(self):
        started_at = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)

        with self.app.app_context():
            recipe = self._seed_recipe(
                steps_json=[
                    {"actor": "Stirrer_01", "task": "Ramp to 300", "delta_time": 1, "rpm": 300},
                    {"actor": "Stirrer_01", "task": "Hold 300", "delta_time": 1, "rpm": 300},
                ]
            )

            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at):
                recipe_program_runtime.start_recipe_program(self.app, recipe, requested_by="integration_test")
                db.session.commit()

            run = RecipeProgramRun.query.one()
            events = RecipeProgramEvent.query.order_by(RecipeProgramEvent.recipe_program_event_id.asc()).all()
            state = db.session.get(RecipeProgramState, 1)

            self.assertEqual(run.status, "running")
            self.assertEqual(run.requested_by, "integration_test")
            self.assertEqual(run.started_at, self._naive_utc(started_at))
            self.assertEqual(run.last_progress_at, self._naive_utc(started_at))
            self.assertEqual(state.active_step_index, 0)
            self.assertEqual([event.event_type for event in events], ["started"])
            self.assertEqual(events[0].event_payload["bindings"][0]["actor"], "Stirrer_01")
            self.assertEqual(events[0].event_payload["active_step"]["task"], "Ramp to 300")

    def test_reconciler_appends_step_target_and_completion_events(self):
        started_at = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)
        worker_id = "worker-1"

        with self.app.app_context():
            recipe = self._seed_recipe(
                steps_json=[
                    {"actor": "Stirrer_01", "task": "Ramp to 300", "delta_time": 1, "rpm": 300},
                    {"actor": "Stirrer_01", "task": "Ramp to 600", "delta_time": 1, "rpm": 600},
                ]
            )

            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at):
                recipe_program_runtime.start_recipe_program(self.app, recipe, requested_by="integration_test")
                db.session.commit()

            self._acquire_program_lease(worker_id=worker_id, lease_until=started_at + timedelta(seconds=15))
            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at + timedelta(seconds=30)):
                recipe_program_runtime._process_recipe_program_state(self.app, worker_id=worker_id)

            self._acquire_program_lease(worker_id=worker_id, lease_until=started_at + timedelta(seconds=85))
            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at + timedelta(seconds=70)):
                recipe_program_runtime._process_recipe_program_state(self.app, worker_id=worker_id)

            self._acquire_program_lease(worker_id=worker_id, lease_until=started_at + timedelta(seconds=145))
            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at + timedelta(seconds=130)):
                recipe_program_runtime._process_recipe_program_state(self.app, worker_id=worker_id)

            run = RecipeProgramRun.query.one()
            events = RecipeProgramEvent.query.order_by(RecipeProgramEvent.recipe_program_event_id.asc()).all()
            event_types = [event.event_type for event in events]

            self.assertEqual(run.status, "completed")
            self.assertEqual(run.finished_at, self._naive_utc(started_at + timedelta(seconds=130)))
            self.assertIn("targets_applied", event_types)
            self.assertIn("step_started", event_types)
            self.assertEqual(event_types[-1], "completed")
            self.assertEqual(events[-1].event_payload["applied_targets"][0]["rpm"], 600)

            step_event = next(event for event in events if event.event_type == "step_started")
            self.assertEqual(step_event.event_payload["active_step"]["task"], "Ramp to 600")

            target_events = [event for event in events if event.event_type == "targets_applied"]
            self.assertEqual(target_events[0].event_payload["changes"][0]["current"]["rpm"], 150)
            self.assertEqual(target_events[-1].event_payload["changes"][0]["current"]["rpm"], 600)

    def test_stop_closes_run_and_logs_stop_event(self):
        started_at = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)
        stopped_at = started_at + timedelta(seconds=20)

        with self.app.app_context():
            recipe = self._seed_recipe(
                steps_json=[
                    {"actor": "Stirrer_01", "task": "Ramp to 300", "delta_time": 1, "rpm": 300},
                ]
            )

            with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at):
                recipe_program_runtime.start_recipe_program(self.app, recipe, requested_by="integration_test")
                db.session.commit()

            with patch.object(recipe_program_runtime, "_now_utc", return_value=stopped_at):
                recipe_program_runtime.stop_recipe_program(self.app, requested_by="integration_stop")
                db.session.commit()

            run = RecipeProgramRun.query.one()
            events = RecipeProgramEvent.query.order_by(RecipeProgramEvent.recipe_program_event_id.asc()).all()

            self.assertEqual(run.status, "stopped")
            self.assertEqual(run.finished_at, self._naive_utc(stopped_at))
            self.assertEqual(events[-1].event_type, "stopped")
            self.assertEqual(run.requested_by, "integration_stop")


if __name__ == "__main__":
    unittest.main()
