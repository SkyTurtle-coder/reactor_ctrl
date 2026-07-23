"""End-to-end coverage for the ICS435 live-snapshot path: a real (loopback)
MT-SICS TCP server -> the real driver -> the real per-device lock and
reconciler cycle -> the in-memory live snapshot and DeviceManualState cache
-> the JSON shape served by GET /manual-state.

This exercises the actual production code path (no mocking of the driver,
transport, or reconciler internals) so the measured timings and behaviors
below are real, not asserted-by-construction.
"""

import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as app_config
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
)
from reactor_app.services import device_manual_runtime


class _ScriptedMTSicsServer:
    """A minimal, real TCP server that answers MT-SICS SI requests."""

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
            # The connection can be closed from another thread (e.g.
            # close_connection() in a failure-simulation test) while this
            # loop is blocked in recv(); that surfaces as an OSError here
            # rather than a clean return.
            return

    def close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass


class Ics435LiveSnapshotEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_recipe_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "ics435_live_snapshot_e2e.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        # SQLite can't execute this codebase's MySQL-flavoured
        # CURRENT_TIMESTAMP(3) column defaults, so db.create_all() fails for
        # these tables under SQLite. Schema is created by hand below instead
        # (same workaround already used by test_reactor_builder_display_targets.py
        # and test_device_manual_measurement_persistence.py).
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
            for table_name in (
                "recipe_program_state",
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

    def _seed_bound_scale(self, *, host: str, port: int, asset_serial: str) -> Device:
        server = DeviceServer(
            server_code=f"ICS435-E2E-{asset_serial}",
            display_name="Mettler Toledo ICS435 (e2e test)",
            vendor="Mettler Toledo",
            model="ICS435",
            # device_server.host is a unique descriptive/management field, not
            # what the driver actually connects to (that's
            # device_connection.tcp_host/tcp_port below) — each test binds its
            # fake server to 127.0.0.1 on a different port, so disambiguate here.
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
            display_name="ICS435 e2e Balance",
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
        db.session.add(
            DeviceManualState(
                device_id=device.device_id,
                queue_status="idle",
                desired_version=0,
                applied_version=0,
                watch_expires_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()
        return device

    def _claim_for_processing(self, device_id: int, *, worker_id: str) -> None:
        # _process_manual_state() is normally only reachable after
        # _claim_next_device_id() sets lease_owner to the calling worker; when
        # calling it directly (bypassing the claim loop, as these tests do to
        # isolate the reconciler cycle itself), that lease must be set by hand.
        # watch_expires_at must be kept in the future so poll_due is true even
        # when polling faster than the configured background interval (these
        # tests intentionally poll back-to-back, without a real 500-1000 ms
        # gap between cycles).
        state = db.session.get(DeviceManualState, device_id)
        state.lease_owner = worker_id
        state.queue_status = "running"
        state.next_poll_at = datetime.now(timezone.utc)
        state.watch_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        db.session.commit()

    def test_weight_flows_from_tcp_telegram_to_live_snapshot_within_target_latency(self):
        with self.app.app_context():
            with _ScriptedMTSicsServer([b"S S      12.34 g\r\n"]) as server:
                device = self._seed_bound_scale(host=server.host, port=server.port, asset_serial="E2E-001")
                self._claim_for_processing(device.device_id, worker_id="e2e-worker")

                started = time.perf_counter()
                device_manual_runtime._process_manual_state(
                    self.app, device_id=device.device_id, worker_id="e2e-worker"
                )
                elapsed_s = time.perf_counter() - started

                # Real measured latency for one full reconciler cycle over a
                # real (loopback) TCP round trip: SI request -> parse -> cache.
                self.assertLess(
                    elapsed_s,
                    1.0,
                    f"One SI poll cycle took {elapsed_s * 1000:.1f} ms, expected well under 1000 ms locally.",
                )

                snapshot = device_manual_runtime.get_scale_live_snapshot(device.device_id)
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot["kind"], "scale")
                self.assertEqual(snapshot["weight"], 12.34)
                self.assertEqual(snapshot["unit"], "g")
                self.assertTrue(snapshot["stable"])
                self.assertEqual(snapshot["communication_status"], "ok")
                self.assertEqual(snapshot["sequence"], 1)

                state = db.session.get(DeviceManualState, device.device_id)
                api_shape = device_manual_runtime.manual_state_to_dict(state)
                self.assertEqual(api_shape["reported_extra"]["weight"], 12.34)

                self.assertEqual(Measurement.query.filter_by(device_id=device.device_id).count(), 1)

    def test_second_poll_updates_snapshot_immediately_but_throttles_measurement_history(self):
        with self.app.app_context():
            with _ScriptedMTSicsServer([
                b"S S      12.34 g\r\n",
                b"S S      15.00 g\r\n",
            ]) as server:
                device = self._seed_bound_scale(host=server.host, port=server.port, asset_serial="E2E-002")

                self._claim_for_processing(device.device_id, worker_id="w1")
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="w1")
                first_snapshot = device_manual_runtime.get_scale_live_snapshot(device.device_id)
                self.assertEqual(first_snapshot["weight"], 12.34)
                self.assertEqual(first_snapshot["sequence"], 1)

                # Immediately poll again (well inside the default 5 s
                # measurement-persist throttle window) with a DIFFERENT
                # weight from the scripted server.
                self._claim_for_processing(device.device_id, worker_id="w1")
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="w1")
                second_snapshot = device_manual_runtime.get_scale_live_snapshot(device.device_id)

                # The live snapshot (and therefore what GET /manual-state
                # serves) reflects the new value immediately on every poll.
                self.assertEqual(second_snapshot["weight"], 15.00)
                self.assertEqual(second_snapshot["sequence"], 2)

                # But measurement history is throttled: two polls within the
                # default 5 s window must not produce two rows.
                self.assertEqual(Measurement.query.filter_by(device_id=device.device_id).count(), 1)

    def test_connection_failure_marks_snapshot_stale_without_losing_last_known_weight(self):
        with self.app.app_context():
            with _ScriptedMTSicsServer([b"S S      12.34 g\r\n"]) as server:
                device = self._seed_bound_scale(host=server.host, port=server.port, asset_serial="E2E-003")

                self._claim_for_processing(device.device_id, worker_id="w1")
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="w1")
                self.assertEqual(
                    device_manual_runtime.get_scale_live_snapshot(device.device_id)["communication_status"],
                    "ok",
                )

                server.close_connection()

            # Server is gone now; the persistent transport will fail to
            # reconnect. A single poll attempt must fail fast (bounded by the
            # connect/response timeouts), not hang or busy-loop.
            with self.app.app_context():
                self._claim_for_processing(device.device_id, worker_id="w1")

                started = time.perf_counter()
                device_manual_runtime._process_manual_state(self.app, device_id=device.device_id, worker_id="w1")
                elapsed_s = time.perf_counter() - started
                self.assertLess(elapsed_s, 5.0, "A failed poll must fail fast, not hang.")

                snapshot = device_manual_runtime.get_scale_live_snapshot(device.device_id)
                self.assertEqual(snapshot["communication_status"], "error")
                # Last known weight must still be visible (marked stale, not erased).
                self.assertEqual(snapshot["weight"], 12.34)

                state = db.session.get(DeviceManualState, device.device_id)
                self.assertEqual(str(state.queue_status), "error")
                self.assertIsNotNone(state.last_error)


if __name__ == "__main__":
    unittest.main()
