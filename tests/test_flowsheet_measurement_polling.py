"""Regression coverage for flowsheet-scoped background measurement polling.

Bug being covered: while a recipe is running, the device-manual reconciler
used to restrict background polling to the recipe's own device bindings
(``_active_recipe_program_device_ids``). A device that is present in the
active flowsheet but not referenced by any recipe step (e.g. a Mettler
Toledo ICS435 scale with no recipe step) was silently excluded from the
polling candidate list, so it stopped being polled and its Process Trend
history went flat for the duration of the recipe.

The fix scopes background polling to ``_active_flowsheet_device_ids`` — every
actuator/sensor the active flowsheet resolves to a bound device — instead of
the recipe's own (narrower) device set. Recipe bindings remain authoritative
for command *priority* ordering and for the recipe write-sequence lock, but
never for whether a device is polled at all.

Test classes:
    ActiveFlowsheetDeviceIdsUnitTests
        Unit-level coverage of ``_active_flowsheet_device_ids`` itself
        (covers requirement #3: a recipe-only device must not be blindly
        treated as a flowsheet channel).
    FlowsheetScopedPollingIntegrationTests
        Real SQLite-backed integration coverage of the reconciler's
        candidate-selection query and the measurements API
        (covers requirements #1, #2, #7, #8).
    PollerRobustnessTests
        Mocked-session coverage of per-device fault isolation
        (covers requirements #4, #5, #6).
"""
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config as app_config
from flask import Flask
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import (
    Device,
    DeviceBindingCurrent,
    DeviceConnection,
    DeviceManualState,
    DeviceServer,
    Measurement,
    ReactorBuild,
    RecipeProgramState,
)
from reactor_app.services import device_manual_runtime
from reactor_app.services.device_runtime import DeviceCommandError


# ---------------------------------------------------------------------------
# Class 1: unit-level coverage of _active_flowsheet_device_ids
# ---------------------------------------------------------------------------

class _FakeSessionForFlowsheetScope:
    """Minimal db.session stand-in: only RecipeProgramState/ReactorBuild lookups."""

    def __init__(self, *, program_state=None, reactor_build=None):
        self._program_state = program_state
        self._reactor_build = reactor_build

    def get(self, model, _item_id):
        if model is RecipeProgramState:
            return self._program_state
        if model is ReactorBuild:
            return self._reactor_build
        raise AssertionError(f"Unexpected model lookup: {model}")


class ActiveFlowsheetDeviceIdsUnitTests(unittest.TestCase):
    def setUp(self):
        # The resolved scope is cached for a few seconds (see
        # _FLOWSHEET_DEVICE_IDS_CACHE_TTL_SECONDS) to avoid re-resolving the
        # flowsheet on every reconciler tick. Clear it between tests so one
        # test's mocked resolution can never leak into another.
        device_manual_runtime._flowsheet_device_ids_cache.clear()
        device_manual_runtime._flowsheet_device_ids_last_logged.clear()

    def test_returns_none_when_no_recipe_is_running(self):
        program_state = RecipeProgramState(status="idle")
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            result = device_manual_runtime._active_flowsheet_device_ids()

        self.assertIsNone(result)

    def test_scope_covers_full_flowsheet_not_just_recipe_bindings(self):
        # Recipe only binds the stirrer (device_id=10); the flowsheet also
        # contains a scale (device_id=20) with no recipe step at all.
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {
            "reactor_build_id": 3,
            "bindings": [{"actor": "Stirrer_01", "device_id": 10}],
        }
        reactor_build = ReactorBuild(reactor_build_id=3, build_name="Build", definition_json={})
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=reactor_build)
        flowsheet_targets = {
            "node-stirrer": {"is_resolved": True, "device_id": 10},
            "node-scale": {"is_resolved": True, "device_id": 20},
        }

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "resolve_process_device_targets", return_value=flowsheet_targets):
                result = device_manual_runtime._active_flowsheet_device_ids()
            # The narrower recipe-only view must indeed have excluded the
            # scale, otherwise this test would not be distinguishing the two
            # functions.
            recipe_only = device_manual_runtime._active_recipe_program_device_ids()

        self.assertEqual(result, {10, 20})
        self.assertEqual(recipe_only, {10})

    def test_recipe_device_absent_from_resolved_flowsheet_is_not_blindly_included(self):
        # A device referenced by a recipe binding but that the flowsheet
        # itself cannot resolve to must not leak into the flowsheet scope.
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {
            "reactor_build_id": 7,
            "bindings": [{"actor": "Ghost", "device_id": 99}],
        }
        reactor_build = ReactorBuild(reactor_build_id=7, build_name="Build", definition_json={})
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=reactor_build)
        flowsheet_targets = {"node-scale": {"is_resolved": True, "device_id": 1}}

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "resolve_process_device_targets", return_value=flowsheet_targets):
                result = device_manual_runtime._active_flowsheet_device_ids()

        self.assertEqual(result, {1})
        self.assertNotIn(99, result)

    def test_falls_back_to_recipe_scope_when_snapshot_has_no_reactor_build_id(self):
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {"bindings": [{"device_id": 10}]}
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=None)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            result = device_manual_runtime._active_flowsheet_device_ids()

        self.assertEqual(result, {10})

    def test_falls_back_to_recipe_scope_when_reactor_build_cannot_be_loaded(self):
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {"reactor_build_id": 5, "bindings": [{"device_id": 10}]}
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=None)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            result = device_manual_runtime._active_flowsheet_device_ids()

        self.assertEqual(result, {10})

    def test_falls_back_to_recipe_scope_when_resolution_raises(self):
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {"reactor_build_id": 6, "bindings": [{"device_id": 11}]}
        reactor_build = ReactorBuild(reactor_build_id=6, build_name="Build", definition_json={})
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=reactor_build)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(
                device_manual_runtime,
                "resolve_process_device_targets",
                side_effect=RuntimeError("flowsheet resolution boom"),
            ):
                result = device_manual_runtime._active_flowsheet_device_ids()

        self.assertEqual(result, {11})

    def test_resolution_is_cached_for_the_ttl_window(self):
        program_state = RecipeProgramState(status="running")
        program_state.snapshot_json = {"reactor_build_id": 9, "bindings": []}
        reactor_build = ReactorBuild(reactor_build_id=9, build_name="Build", definition_json={})
        fake_session = _FakeSessionForFlowsheetScope(program_state=program_state, reactor_build=reactor_build)
        resolve_mock = MagicMock(return_value={"node-scale": {"is_resolved": True, "device_id": 1}})

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "resolve_process_device_targets", resolve_mock):
                first = device_manual_runtime._active_flowsheet_device_ids()
                second = device_manual_runtime._active_flowsheet_device_ids()

        self.assertEqual(first, {1})
        self.assertEqual(second, {1})
        self.assertEqual(resolve_mock.call_count, 1, "flowsheet resolution must be cached within the TTL window")


# ---------------------------------------------------------------------------
# Class 2: real SQLite-backed integration coverage
# ---------------------------------------------------------------------------

class _ScriptedMTSicsServer:
    """A minimal, real TCP server that answers MT-SICS SI requests.

    Copied (trimmed) from test_ics435_live_snapshot_e2e.py: this exercises
    the real driver/transport, not a mock, so the measurement that ends up
    in the database is the same one a real scale poll would produce.
    """

    def __init__(self, weight_responses: list[bytes]):
        self._responses = list(weight_responses)
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.host, self.port = self._server.getsockname()
        self._server.listen()
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._conn: socket.socket | None = None

    def __enter__(self) -> "_ScriptedMTSicsServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)
        self._server.close()

    def _serve(self) -> None:
        try:
            conn, _addr = self._server.accept()
        except socket.timeout:
            return
        self._conn = conn
        try:
            with conn:
                while not self._stop.is_set():
                    try:
                        data = bytearray()
                        conn.settimeout(0.2)
                        while not data.endswith(b"\n"):
                            chunk = conn.recv(1)
                            if not chunk:
                                return
                            data.extend(chunk)
                    except socket.timeout:
                        continue
                    with self._lock:
                        if not self._responses:
                            return
                        response = self._responses.pop(0)
                    conn.sendall(response)
        except OSError:
            return


class FlowsheetScopedPollingIntegrationTests(unittest.TestCase):
    """Real Flask app + SQLite DB; exercises the actual reconciler queries."""

    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_recipe_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "flowsheet_measurement_polling.sqlite"
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
                    CREATE TABLE control_command (
                        command_id INTEGER PRIMARY KEY,
                        device_id INTEGER NOT NULL,
                        request_uuid TEXT NOT NULL UNIQUE,
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        command_name TEXT NOT NULL,
                        command_payload TEXT,
                        command_source TEXT,
                        command_priority INTEGER,
                        correlation_id TEXT,
                        worker_id TEXT,
                        status TEXT NOT NULL DEFAULT 'queued',
                        requested_at TEXT,
                        scheduled_for TEXT,
                        started_at TEXT,
                        sent_at TEXT,
                        ack_at TEXT,
                        finished_at TEXT,
                        queue_timeout_s REAL,
                        execution_timeout_s REAL,
                        total_deadline_at TEXT,
                        cancel_requested_at TEXT,
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
                        paused_at TEXT,
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
            for table_name in (
                "recipe_program_state",
                "reactor_build",
                "control_command_event",
                "control_command",
                "device_manual_state",
                "measurement",
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
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = cls._original_recipe_reconciler_enabled
        # See setUp: hand back a clean module-level scale snapshot cache so
        # this file's device_ids (from its own throwaway SQLite DB) cannot
        # leak a stale "sequence" counter into a later test file/class that
        # happens to reuse the same small device_id integers.
        device_manual_runtime._SCALE_LIVE_SNAPSHOTS.clear()
        device_manual_runtime._flowsheet_device_ids_cache.clear()
        device_manual_runtime._flowsheet_device_ids_last_logged.clear()

    def setUp(self):
        device_manual_runtime._flowsheet_device_ids_cache.clear()
        device_manual_runtime._flowsheet_device_ids_last_logged.clear()
        # The scale live-snapshot cache is a module-level dict keyed by plain
        # device_id ints (see device_manual_runtime._SCALE_LIVE_SNAPSHOTS).
        # Different test classes each run their own SQLite DB whose
        # autoincrement device_ids can collide (e.g. both assign device_id=2),
        # so this must be cleared between tests to avoid leaking a stale
        # "sequence" counter into an unrelated test/device.
        device_manual_runtime._SCALE_LIVE_SNAPSHOTS.clear()
        with self.app.app_context():
            for table_name in (
                "recipe_program_state",
                "reactor_build",
                "control_command_event",
                "control_command",
                "device_manual_state",
                "measurement",
                "measurement_channel",
                "device_binding_current",
                "device",
                "device_connection",
                "device_server",
            ):
                db.session.execute(text(f"DELETE FROM {table_name}"))
            db.session.commit()

    # -- fixture helpers ----------------------------------------------------

    def _seed_scale(self, *, host: str, port: int, asset_serial: str, with_manual_state: bool = True) -> Device:
        server = DeviceServer(
            server_code=f"ICS435-{asset_serial}",
            display_name="Mettler Toledo ICS435",
            vendor="Mettler Toledo",
            model="ICS435",
            host=f"{host}:{port}",
            serial_standard="ethernet",
            port_count=1,
        )
        db.session.add(server)
        db.session.flush()

        connection = DeviceConnection(
            device_server_id=server.device_server_id,
            port_number=1,
            connection_label="COM2 Ethernet",
            transport_type="tcp_socket",
            tcp_host=host,
            tcp_port=port,
            read_timeout_ms=1200,
            write_timeout_ms=1200,
            reconnect_delay_ms=250,
            is_enabled=True,
        )
        db.session.add(connection)
        db.session.flush()

        device = Device(
            asset_serial=asset_serial,
            manufacturer_serial=f"SN-{asset_serial}",
            display_name="ICS435 Balance",
            device_type="scale",
            protocol="mettler_toledo_ics435",
            is_active=True,
        )
        db.session.add(device)
        db.session.flush()

        db.session.add(
            DeviceBindingCurrent(
                device_id=device.device_id,
                connection_id=connection.connection_id,
                quality_state="configured",
                is_online=True,
            )
        )
        if with_manual_state:
            db.session.add(
                DeviceManualState(
                    device_id=device.device_id,
                    queue_status="idle",
                    desired_version=0,
                    applied_version=0,
                )
            )
        db.session.commit()
        return device, server, connection

    def _seed_stirrer(self, *, asset_serial: str, with_manual_state: bool = False) -> Device:
        server = DeviceServer(server_code=f"MOXA-{asset_serial}", display_name="Moxa NPort", host=f"10.0.0.{asset_serial[-1]}")
        db.session.add(server)
        db.session.flush()

        connection = DeviceConnection(
            device_server_id=server.device_server_id,
            port_number=1,
            connection_label="Port 1",
            transport_type="tcp_socket",
            tcp_host="127.0.0.1",
            tcp_port=4001,
            is_enabled=True,
        )
        db.session.add(connection)
        db.session.flush()

        device = Device(
            asset_serial=asset_serial,
            manufacturer_serial=f"SN-{asset_serial}",
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
                quality_state="configured",
                is_online=True,
            )
        )
        if with_manual_state:
            db.session.add(
                DeviceManualState(
                    device_id=device.device_id,
                    queue_status="idle",
                    desired_version=0,
                    applied_version=0,
                )
            )
        db.session.commit()
        return device, server, connection

    def _seed_flowsheet(self, *, stirrer_server, stirrer_connection, scale_server, scale_connection) -> ReactorBuild:
        build = ReactorBuild(
            build_name="Test Flowsheet",
            build_date=datetime.now(timezone.utc).date(),
            created_by="tester",
            definition_json={
                "canvas": {"width": 1200, "height": 800},
                "nodes": [
                    {
                        "id": "node-stirrer",
                        "symbol_id": "motor",
                        "instance_id": "Stirrer_01",
                        "label": "Stirrer",
                        "category": "actuators",
                        "communication": {
                            "device_server_code": stirrer_server.server_code,
                            "connection_label": stirrer_connection.connection_label,
                            "protocol": "ika_eurostar_60",
                        },
                    },
                    {
                        "id": "node-scale",
                        "symbol_id": "scale",
                        "instance_id": "Scale_01",
                        "label": "Scale",
                        "category": "sensors",
                        "communication": {
                            "device_server_code": scale_server.server_code,
                            "connection_label": scale_connection.connection_label,
                            "protocol": "mettler_toledo_ics435",
                        },
                    },
                ],
                "edges": [],
            },
        )
        db.session.add(build)
        db.session.commit()
        return build

    def _seed_running_recipe(self, *, reactor_build_id: int, stirrer_device_id: int) -> None:
        # The recipe references ONLY the stirrer — the scale has no recipe
        # step, matching the reported bug scenario.
        state = RecipeProgramState(
            recipe_program_state_id=1,
            reactor_build_id=reactor_build_id,
            status="running",
        )
        state.snapshot_json = {
            "reactor_build_id": reactor_build_id,
            "bindings": [
                {
                    "actor": "Stirrer_01",
                    "device_id": stirrer_device_id,
                    "device_display_name": "IKA Stirrer",
                    "profile_id": "motor_rpm",
                    "protocol": "ika_eurostar_60",
                }
            ],
        }
        db.session.add(state)
        db.session.commit()

    # -- requirement #1 + #8 -------------------------------------------------

    def test_scale_outside_recipe_is_polled_recorded_and_served_by_api_during_recipe(self):
        with self.app.app_context():
            with _ScriptedMTSicsServer([b"S S      12.34 g\r\n"]) as server:
                scale, scale_server, scale_connection = self._seed_scale(
                    host=server.host, port=server.port, asset_serial="SCALE-01"
                )
                stirrer, stirrer_server, stirrer_connection = self._seed_stirrer(
                    asset_serial="STIR-01", with_manual_state=False
                )
                build = self._seed_flowsheet(
                    stirrer_server=stirrer_server,
                    stirrer_connection=stirrer_connection,
                    scale_server=scale_server,
                    scale_connection=scale_connection,
                )
                self._seed_running_recipe(reactor_build_id=build.reactor_build_id, stirrer_device_id=stirrer.device_id)

                # The recipe binds only the stirrer, but the flowsheet scope
                # must still include the scale.
                scope = device_manual_runtime._active_flowsheet_device_ids()
                self.assertIn(scale.device_id, scope)
                recipe_only_scope = device_manual_runtime._active_recipe_program_device_ids()
                self.assertNotIn(scale.device_id, recipe_only_scope)

                # The scale (with no UI watch active) must still be claimable
                # as a background-poll candidate while the recipe is running.
                claimed_device_id = device_manual_runtime._claim_next_device_id(self.app, "worker-1")
                self.assertEqual(claimed_device_id, scale.device_id)

                device_manual_runtime._process_manual_state(
                    self.app, device_id=scale.device_id, worker_id="worker-1"
                )

                weight_rows = Measurement.query.filter_by(device_id=scale.device_id, channel_code="weight").all()
                self.assertEqual(len(weight_rows), 1)
                self.assertAlmostEqual(weight_rows[0].numeric_value, 12.34)
                scale_device_id = scale.device_id

        # Requirement #8: the recorded weight is served by the existing
        # measurements API, unchanged, for the Process Trend to consume.
        response = self.client.get(f"/api/devices/{scale_device_id}/measurements")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertAlmostEqual(payload["items"][0]["numeric_value"], 12.34)
        self.assertEqual(payload["items"][0]["channel_code"], "weight")

    # -- requirement #2 -------------------------------------------------------

    def test_all_flowsheet_sensors_are_scoped_even_when_recipe_uses_a_single_actor(self):
        with self.app.app_context():
            scale_a, scale_a_server, scale_a_connection = self._seed_scale(
                host="127.0.0.1", port=9001, asset_serial="SCALE-A", with_manual_state=False
            )
            scale_b, scale_b_server, scale_b_connection = self._seed_scale(
                host="127.0.0.1", port=9002, asset_serial="SCALE-B", with_manual_state=False
            )
            stirrer, stirrer_server, stirrer_connection = self._seed_stirrer(asset_serial="STIR-02")

            build = ReactorBuild(
                build_name="Multi-sensor Flowsheet",
                build_date=datetime.now(timezone.utc).date(),
                created_by="tester",
                definition_json={
                    "canvas": {"width": 1200, "height": 800},
                    "nodes": [
                        {
                            "id": "node-stirrer",
                            "symbol_id": "motor",
                            "instance_id": "Stirrer_01",
                            "label": "Stirrer",
                            "category": "actuators",
                            "communication": {
                                "device_server_code": stirrer_server.server_code,
                                "connection_label": stirrer_connection.connection_label,
                                "protocol": "ika_eurostar_60",
                            },
                        },
                        {
                            "id": "node-scale-a",
                            "symbol_id": "scale",
                            "instance_id": "Scale_A",
                            "label": "Scale A",
                            "category": "sensors",
                            "communication": {
                                "device_server_code": scale_a_server.server_code,
                                "connection_label": scale_a_connection.connection_label,
                                "protocol": "mettler_toledo_ics435",
                            },
                        },
                        {
                            "id": "node-scale-b",
                            "symbol_id": "scale",
                            "instance_id": "Scale_B",
                            "label": "Scale B",
                            "category": "sensors",
                            "communication": {
                                "device_server_code": scale_b_server.server_code,
                                "connection_label": scale_b_connection.connection_label,
                                "protocol": "mettler_toledo_ics435",
                            },
                        },
                    ],
                    "edges": [],
                },
            )
            db.session.add(build)
            db.session.commit()
            self._seed_running_recipe(reactor_build_id=build.reactor_build_id, stirrer_device_id=stirrer.device_id)

            scope = device_manual_runtime._active_flowsheet_device_ids()
            expected = {stirrer.device_id, scale_a.device_id, scale_b.device_id}

        self.assertEqual(scope, expected)

    # -- requirement #7 -------------------------------------------------------

    def test_exactly_one_lease_per_device_after_recipe_ends(self):
        with self.app.app_context():
            scale, scale_server, scale_connection = self._seed_scale(
                host="127.0.0.1", port=9101, asset_serial="SCALE-END"
            )
            stirrer, stirrer_server, stirrer_connection = self._seed_stirrer(
                asset_serial="STIR-END", with_manual_state=True
            )
            build = self._seed_flowsheet(
                stirrer_server=stirrer_server,
                stirrer_connection=stirrer_connection,
                scale_server=scale_server,
                scale_connection=scale_connection,
            )
            # Recipe has finished: background polling must return to its
            # unrestricted (flowsheet-independent) behaviour.
            state = RecipeProgramState(
                recipe_program_state_id=1,
                reactor_build_id=build.reactor_build_id,
                status="completed",
            )
            state.snapshot_json = {"reactor_build_id": build.reactor_build_id, "bindings": []}
            db.session.add(state)
            db.session.commit()

            self.assertIsNone(device_manual_runtime._active_flowsheet_device_ids())

            first_claim = device_manual_runtime._claim_next_device_id(self.app, "worker-1")
            self.assertIsNotNone(first_claim)
            self.assertIn(first_claim, {scale.device_id, stirrer.device_id})

            second_claim = device_manual_runtime._claim_next_device_id(self.app, "worker-2")
            self.assertIsNotNone(second_claim)
            self.assertNotEqual(
                second_claim, first_claim, "the same device must not be leased to two workers at once"
            )

            third_claim = device_manual_runtime._claim_next_device_id(self.app, "worker-3")
            self.assertIsNone(third_claim, "both devices already have an active lease; nothing left to claim")


# ---------------------------------------------------------------------------
# Class 3: per-device fault isolation (mocked session, no real DB)
# ---------------------------------------------------------------------------

class _FakeSessionForProcess:
    def __init__(self, *, state, device, program_state=None):
        self._state = state
        self._device = device
        self._program_state = program_state
        self.commit_calls = 0
        self.rollback_calls = 0

    def get(self, model, _device_id):
        if model is DeviceManualState:
            return self._state
        if model is Device:
            return self._device
        if model is RecipeProgramState:
            return self._program_state
        raise AssertionError(f"Unexpected model lookup: {model}")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class PollerRobustnessTests(unittest.TestCase):
    def setUp(self):
        # See FlowsheetScopedPollingIntegrationTests.setUp: this module-level
        # cache is keyed by plain device_id ints, which are reused across
        # unrelated tests/files — clear it so no stale "sequence" leaks in
        # either direction.
        device_manual_runtime._SCALE_LIVE_SNAPSHOTS.clear()

    def tearDown(self):
        device_manual_runtime._SCALE_LIVE_SNAPSHOTS.clear()

    def _make_app(self) -> Flask:
        return Flask(__name__)

    def _due_state(self, device_id: int) -> DeviceManualState:
        now = datetime.now(timezone.utc)
        state = DeviceManualState(
            device_id=device_id,
            queue_status="running",
            desired_version=0,
            applied_version=0,
            lease_owner="worker-1",
        )
        state.watch_expires_at = now + timedelta(seconds=30)
        state.next_poll_at = now - timedelta(seconds=1)
        state.last_reported_at = None
        return state

    # -- requirement #4 -------------------------------------------------------

    def test_device_without_measurement_capability_does_not_crash_the_poller(self):
        app = self._make_app()
        state = self._due_state(device_id=1)
        device = Device(
            device_id=1,
            asset_serial="VALVE-1",
            display_name="Manual Valve",
            device_type="actuator",
            protocol="manual_valve",  # not in _BACKGROUND_POLL_PROTOCOLS: no read function
        )
        fake_session = _FakeSessionForProcess(state=state, device=device)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            # Must return normally — no exception — even though this device
            # cannot be polled for measurements.
            device_manual_runtime._process_manual_state(app, device_id=1, worker_id="worker-1")

        self.assertEqual(state.queue_status, "error")
        self.assertIn("not supported", (state.last_error or "").lower())
        self.assertIsNone(state.lease_owner)

    # -- requirement #5 -------------------------------------------------------

    def test_one_sensor_error_does_not_block_processing_of_another_sensor(self):
        app = self._make_app()

        failing_state = self._due_state(device_id=1)
        failing_device = Device(
            device_id=1, asset_serial="SCALE-A", display_name="Scale A",
            device_type="scale", protocol="mettler_toledo_ics435",
        )

        healthy_state = self._due_state(device_id=2)
        healthy_device = Device(
            device_id=2, asset_serial="SCALE-B", display_name="Scale B",
            device_type="scale", protocol="mettler_toledo_ics435",
        )

        def flaky_read_scale_status(_app, device):
            if device.device_id == 1:
                raise TimeoutError("scale did not respond in time")
            return {
                "weight": 42.0,
                "weight_unit": "g",
                "weight_stable": True,
                "weight_quality_score": 1.0,
                "weight_raw_payload": {},
            }

        session_for_failure = _FakeSessionForProcess(state=failing_state, device=failing_device)
        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=session_for_failure)):
            with patch.object(device_manual_runtime, "_read_scale_status", flaky_read_scale_status):
                # Must not raise, even though the underlying read times out.
                device_manual_runtime._process_manual_state(app, device_id=1, worker_id="worker-1")

        self.assertEqual(failing_state.queue_status, "error")
        self.assertIn("did not respond", failing_state.last_error or "")

        session_for_success = _FakeSessionForProcess(state=healthy_state, device=healthy_device)
        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=session_for_success)):
            with patch.object(device_manual_runtime, "_read_scale_status", flaky_read_scale_status):
                device_manual_runtime._process_manual_state(app, device_id=2, worker_id="worker-1")

        self.assertEqual(healthy_state.queue_status, "idle")
        self.assertIsNone(healthy_state.last_error)

    # -- requirement #6 -------------------------------------------------------

    def test_recipe_command_and_poll_contention_reschedules_without_failing(self):
        app = self._make_app()
        state = self._due_state(device_id=3)
        device = Device(
            device_id=3, asset_serial="SCALE-C", display_name="Scale C",
            device_type="scale", protocol="mettler_toledo_ics435",
        )
        fake_session = _FakeSessionForProcess(state=state, device=device)
        busy_exc = DeviceCommandError("Device 3 is busy executing another command.", status_code=409)

        def raise_busy(_app, _device):
            raise busy_exc

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "_read_scale_status", raise_busy):
                # Flowsheet scope includes this device; the recipe does not
                # bind it (it is a sensor), so the sequence lock is not
                # required — only the device-command lock contention matters.
                with patch.object(device_manual_runtime, "_active_recipe_program_device_ids", return_value=set()):
                    device_manual_runtime._process_manual_state(app, device_id=3, worker_id="worker-1")

        self.assertEqual(state.queue_status, "queued", "device-busy must reschedule the poll, not fail it")
        self.assertIsNone(state.lease_owner)
        self.assertIsNotNone(state.next_poll_at)
        self.assertLess(
            state.next_poll_at, datetime.now(timezone.utc) + timedelta(seconds=5),
            "the poller must resume automatically, shortly after the conflict",
        )


if __name__ == "__main__":
    unittest.main()
