import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import ControlCommand, ControlCommandEvent
from reactor_app.services.command_dispatcher import (
    dispatch_device_command,
    get_runtime_command_scheduler,
    start_runtime_command_scheduler,
    stop_runtime_command_scheduler,
)
from reactor_app.services.command_model import CommandPriority, CommandSource, DeviceCommand
from reactor_app.services.device_runtime import create_control_command_record
from reactor_app.services.runtime_status import RuntimeStatus


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Phase5RuntimePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED
        cls._original_runtime_worker_count = getattr(app_config.Config, "RUNTIME_COMMAND_WORKER_COUNT", 2)

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "phase5_runtime.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False
        app_config.Config.RUNTIME_COMMAND_WORKER_COUNT = 1

        cls.app = create_app()
        with cls.app.app_context():
            db.session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS control_command (
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
                    CREATE TABLE IF NOT EXISTS control_command_event (
                        command_event_id INTEGER PRIMARY KEY,
                        command_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        event_payload TEXT,
                        created_at TEXT
                    )
                    """
                )
            )
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        try:
            stop_runtime_command_scheduler(cls.app, cancel_pending=True, timeout_s=1.0)
        except Exception:
            pass
        with cls.app.app_context():
            try:
                db.session.remove()
            except Exception:
                pass
            try:
                db.engine.dispose()
            except Exception:
                pass

        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = cls._original_manual_reconciler_enabled
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = cls._original_program_reconciler_enabled
        app_config.Config.RUNTIME_COMMAND_WORKER_COUNT = cls._original_runtime_worker_count
        try:
            cls._tmpdir.cleanup()
        except PermissionError:
            pass

    def setUp(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            ControlCommandEvent.query.delete()
            ControlCommand.query.delete()
            db.session.commit()
        start_runtime_command_scheduler(self.app)
        self.device_ref = SimpleNamespace(device_id=1)

    def tearDown(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            db.session.remove()

    def _command(self, **overrides) -> DeviceCommand:
        defaults = dict(
            device_id=1,
            command_type="phase5_test",
            payload={},
            priority=CommandPriority.MANUAL,
            source=CommandSource.API,
            requested_by="phase5_test",
        )
        defaults.update(overrides)
        return DeviceCommand(**defaults)

    def _command_row(self, request_uuid: str) -> ControlCommand:
        with self.app.app_context():
            row = ControlCommand.query.filter_by(request_uuid=request_uuid).one()
            _ = list(row.events)
            return row

    def _event_types(self, request_uuid: str) -> list[str]:
        with self.app.app_context():
            row = ControlCommand.query.filter_by(request_uuid=request_uuid).one()
            return [event.event_type for event in row.events]

    def _wait_for_status(self, request_uuid: str, expected_status: str, *, timeout_s: float = 2.0) -> ControlCommand:
        deadline = time.monotonic() + timeout_s
        while True:
            row = self._command_row(request_uuid)
            if row.status == expected_status:
                return row
            if time.monotonic() >= deadline:
                self.fail(f"Command {request_uuid} never reached status {expected_status}; last status={row.status}")
            time.sleep(0.05)

    def test_dispatch_persists_pending_running_and_completed_statuses(self):
        command = self._command()
        fake_result = SimpleNamespace(result=SimpleNamespace(response_text="OK"), command=None, measurement=None)

        with patch("reactor_app.services.command_dispatcher._execute_with_worker_app", return_value=fake_result):
            result = dispatch_device_command(self.device_ref, command, app=self.app)

        self.assertIs(result, fake_result)
        row = self._wait_for_status(command.command_id, RuntimeStatus.COMPLETED)
        self.assertEqual(row.status, RuntimeStatus.COMPLETED)
        self.assertIsNotNone(row.started_at)
        self.assertIsNotNone(row.finished_at)
        self.assertTrue(str(row.worker_id or "").strip())
        self.assertEqual(self._event_types(command.command_id), ["pending", "running", "completed"])

    def test_failed_command_writes_failed_event(self):
        command = self._command(command_type="phase5_fail")

        with patch("reactor_app.services.command_dispatcher._execute_with_worker_app", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                dispatch_device_command(self.device_ref, command, app=self.app)

        row = self._wait_for_status(command.command_id, RuntimeStatus.FAILED)
        self.assertEqual(row.status, RuntimeStatus.FAILED)
        self.assertIn(RuntimeStatus.FAILED, self._event_types(command.command_id))

    def test_queue_timeout_marks_command_timeout_before_execution(self):
        command = self._command(queue_timeout_s=0.0, total_deadline_at=_now_utc() + timedelta(seconds=30))

        with self.assertRaises(Exception):
            dispatch_device_command(self.device_ref, command, app=self.app)

        row = self._wait_for_status(command.command_id, RuntimeStatus.TIMEOUT)
        self.assertEqual(row.status, RuntimeStatus.TIMEOUT)
        self.assertEqual(self._event_types(command.command_id), ["pending", "timeout"])

    def test_execution_timeout_marks_command_timeout_before_worker_executes(self):
        command = self._command(execution_timeout_s=0.0, total_deadline_at=_now_utc() + timedelta(seconds=30))

        with self.assertRaises(Exception):
            dispatch_device_command(self.device_ref, command, app=self.app)

        row = self._wait_for_status(command.command_id, RuntimeStatus.TIMEOUT)
        self.assertEqual(row.status, RuntimeStatus.TIMEOUT)
        self.assertEqual(self._event_types(command.command_id), ["pending", "running", "timeout"])

    def test_total_deadline_marks_command_expired_before_execution(self):
        command = self._command(
            queue_timeout_s=10.0,
            total_deadline_at=_now_utc() - timedelta(seconds=1),
        )

        with self.assertRaises(Exception):
            dispatch_device_command(self.device_ref, command, app=self.app)

        row = self._wait_for_status(command.command_id, RuntimeStatus.EXPIRED)
        self.assertEqual(row.status, RuntimeStatus.EXPIRED)
        self.assertEqual(self._event_types(command.command_id), ["pending", "expired"])

    def test_recovery_skips_pending_polling_command(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            create_control_command_record(
                device_id=1,
                request_uuid="recovery-poll-1",
                requested_by="poller",
                command_name="poll_status",
                command_payload={},
                status=RuntimeStatus.PENDING,
                requested_at=_now_utc() - timedelta(seconds=5),
                scheduled_for=_now_utc() - timedelta(seconds=5),
                command_source=CommandSource.POLLER,
                command_priority=int(CommandPriority.POLLING),
                queue_timeout_s=30.0,
                execution_timeout_s=5.0,
                total_deadline_at=_now_utc() + timedelta(seconds=30),
                event_payload={"requested_by": "poller"},
            )

        start_runtime_command_scheduler(self.app)

        row = self._wait_for_status("recovery-poll-1", RuntimeStatus.SKIPPED)
        self.assertEqual(row.status, RuntimeStatus.SKIPPED)
        self.assertEqual(self._event_types("recovery-poll-1"), ["pending", "recovering", "skipped"])

    def test_recovery_marks_running_command_without_worker_interrupted(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            create_control_command_record(
                device_id=1,
                request_uuid="recovery-running-1",
                requested_by="phase5_test",
                command_name="set_state",
                command_payload={},
                status=RuntimeStatus.RUNNING,
                requested_at=_now_utc() - timedelta(seconds=3),
                scheduled_for=_now_utc() - timedelta(seconds=3),
                command_source=CommandSource.API,
                command_priority=int(CommandPriority.MANUAL),
                worker_id="dead-worker",
                started_at=_now_utc() - timedelta(seconds=2),
                queue_timeout_s=10.0,
                execution_timeout_s=10.0,
                total_deadline_at=_now_utc() + timedelta(seconds=30),
                event_payload={"requested_by": "phase5_test"},
            )

        start_runtime_command_scheduler(self.app)

        row = self._wait_for_status("recovery-running-1", RuntimeStatus.INTERRUPTED)
        self.assertEqual(row.status, RuntimeStatus.INTERRUPTED)
        self.assertEqual(self._event_types("recovery-running-1"), ["running", "recovering", "interrupted"])

    def test_recovery_ignores_acked_command(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            create_control_command_record(
                device_id=1,
                request_uuid="recovery-acked-1",
                requested_by="phase5_test",
                command_name="set_state",
                command_payload={},
                status=RuntimeStatus.ACKED,
                requested_at=_now_utc() - timedelta(seconds=3),
                scheduled_for=_now_utc() - timedelta(seconds=3),
                command_source=CommandSource.API,
                command_priority=int(CommandPriority.MANUAL),
                event_payload={"requested_by": "phase5_test"},
            )

        start_runtime_command_scheduler(self.app)
        time.sleep(0.2)

        row = self._command_row("recovery-acked-1")
        self.assertEqual(row.status, RuntimeStatus.ACKED)
        self.assertEqual(self._event_types("recovery-acked-1"), [RuntimeStatus.ACKED])

    def test_recovery_ignores_completed_command(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            create_control_command_record(
                device_id=1,
                request_uuid="recovery-completed-1",
                requested_by="phase5_test",
                command_name="set_state",
                command_payload={},
                status=RuntimeStatus.COMPLETED,
                requested_at=_now_utc() - timedelta(seconds=3),
                scheduled_for=_now_utc() - timedelta(seconds=3),
                command_source=CommandSource.API,
                command_priority=int(CommandPriority.MANUAL),
                event_payload={"requested_by": "phase5_test"},
            )

        start_runtime_command_scheduler(self.app)
        time.sleep(0.2)

        row = self._command_row("recovery-completed-1")
        self.assertEqual(row.status, RuntimeStatus.COMPLETED)
        self.assertEqual(self._event_types("recovery-completed-1"), [RuntimeStatus.COMPLETED])

    def test_recovery_bulk_acked_commands_do_not_generate_events(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            for index in range(25):
                create_control_command_record(
                    device_id=1,
                    request_uuid=f"recovery-acked-bulk-{index}",
                    requested_by="phase5_test",
                    command_name="set_state",
                    command_payload={},
                    status=RuntimeStatus.ACKED,
                    requested_at=_now_utc() - timedelta(seconds=3),
                    scheduled_for=_now_utc() - timedelta(seconds=3),
                    command_source=CommandSource.API,
                    command_priority=int(CommandPriority.MANUAL),
                    event_payload={"requested_by": "phase5_test"},
                )

        start_runtime_command_scheduler(self.app)
        time.sleep(0.2)

        with self.app.app_context():
            rows = ControlCommand.query.filter(ControlCommand.request_uuid.like("recovery-acked-bulk-%")).all()
            self.assertEqual(len(rows), 25)
            self.assertTrue(all(row.status == RuntimeStatus.ACKED for row in rows))
            event_rows = (
                ControlCommandEvent.query.join(ControlCommand, ControlCommand.command_id == ControlCommandEvent.command_id)
                .filter(ControlCommand.request_uuid.like("recovery-acked-bulk-%"))
                .all()
            )
            self.assertEqual(len(event_rows), 25)
            self.assertTrue(all(event.event_type == RuntimeStatus.ACKED for event in event_rows))

    def test_recovery_requeues_recent_manual_command_and_scheduler_runs_normally(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        with self.app.app_context():
            create_control_command_record(
                device_id=1,
                request_uuid="recovery-manual-1",
                requested_by="operator",
                command_name="set_state",
                command_payload={},
                status=RuntimeStatus.PENDING,
                requested_at=_now_utc(),
                scheduled_for=_now_utc(),
                command_source=CommandSource.API,
                command_priority=int(CommandPriority.MANUAL),
                queue_timeout_s=10.0,
                execution_timeout_s=5.0,
                total_deadline_at=_now_utc() + timedelta(seconds=30),
                event_payload={"requested_by": "operator"},
            )

        fake_result = SimpleNamespace(result=SimpleNamespace(response_text="OK"), command=None, measurement=None)
        with patch("reactor_app.services.command_dispatcher._execute_with_worker_app", return_value=fake_result):
            start_runtime_command_scheduler(self.app)
            row = self._wait_for_status("recovery-manual-1", RuntimeStatus.COMPLETED)

        self.assertEqual(row.status, RuntimeStatus.COMPLETED)
        self.assertEqual(
            self._event_types("recovery-manual-1"),
            ["pending", "recovering", "pending", "running", "completed"],
        )

    def test_scheduler_reuses_existing_instance_without_second_recovery_pass(self):
        stop_runtime_command_scheduler(self.app, cancel_pending=True, timeout_s=1.0)
        import reactor_app.services.command_dispatcher as dispatcher_module

        with patch.object(
            dispatcher_module,
            "_recover_runtime_commands",
            wraps=dispatcher_module._recover_runtime_commands,
        ) as recover_mock:
            first = start_runtime_command_scheduler(self.app)
            second = start_runtime_command_scheduler(self.app)

        self.assertIs(first, second)
        self.assertEqual(recover_mock.call_count, 1)
        self.assertIs(get_runtime_command_scheduler(self.app, start=False), first)


if __name__ == "__main__":
    unittest.main()
