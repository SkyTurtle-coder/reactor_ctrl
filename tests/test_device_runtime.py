import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from reactor_app.models import ControlCommand, ControlCommandEvent
from reactor_app.services import device_runtime
from reactor_app.services.drivers import DeviceCommandResult


class _FakeSession:
    def __init__(self):
        self.executions = []

    def execute(self, statement, params):
        self.executions.append((str(statement), dict(params)))
        return SimpleNamespace(rowcount=1)


class _FakeCommandEventSession:
    def __init__(self):
        self.objects = []
        self.operations = []

    def add(self, item):
        self.objects.append(item)
        if isinstance(item, ControlCommand):
            self.operations.append(("add_command", item.command_id))
        elif isinstance(item, ControlCommandEvent):
            self.operations.append(("add_event", item.command_id))
        else:
            self.operations.append(("add", type(item).__name__))

    def flush(self, *args, **kwargs):
        targets = args[0] if args else self.objects
        if targets is None:
            targets = self.objects
        for item in targets:
            if isinstance(item, ControlCommand) and item.command_id is None:
                item.command_id = 123
        self.operations.append(("flush", None))


class _FakeExecuteCommandSession:
    def __init__(self):
        self.objects = []
        self.operations = []
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, item):
        self.objects.append(item)
        if isinstance(item, ControlCommandEvent):
            self.operations.append(("add_event", item.event_type, item.command_id))
        elif isinstance(item, ControlCommand):
            self.operations.append(("add_command", item.command_id))

    def flush(self, *args, **kwargs):
        targets = args[0] if args else self.objects
        if targets is None:
            targets = self.objects
        for item in targets:
            if isinstance(item, ControlCommand) and item.command_id is None:
                item.command_id = 5728
        self.operations.append(("flush", None))

    def commit(self):
        self.commit_calls += 1
        self.operations.append(("commit", self.commit_calls))

    def rollback(self):
        self.rollback_calls += 1
        self.operations.append(("rollback", self.rollback_calls))

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def execute(self, *_args, **_kwargs):
        self.operations.append(("execute", None))
        return SimpleNamespace(rowcount=1)

    def begin_nested(self):
        return self

    @property
    def no_autoflush(self):
        return self

    def expire(self, *_args, **_kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeDriver:
    uses_transport = False

    def __init__(self, session):
        self.session = session
        self.commit_calls_at_execute = None

    def execute(self, *, transport, request):
        self.commit_calls_at_execute = self.session.commit_calls
        return DeviceCommandResult(
            acknowledged=True,
            response_text="OK",
            response_hex="4f4b",
            metadata={"value": "OK"},
        )


class DeviceRuntimeTelemetryUpdateTests(unittest.TestCase):
    def test_success_telemetry_updates_connection_timestamp_and_guard_binding_by_connection(self):
        session = _FakeSession()
        timestamp = datetime(2026, 5, 13, 8, 15, 0, tzinfo=timezone.utc)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            device_runtime._mark_connection_success(3, timestamp=timestamp)
            device_runtime._mark_binding_online(7, connection_id=3, timestamp=timestamp)

        connection_sql, connection_params = session.executions[0]
        self.assertIn("last_seen_at=:ts", connection_sql)
        self.assertIn("last_error=NULL", connection_sql)
        self.assertIn("updated_at=:ts", connection_sql)
        self.assertIn("WHERE connection_id=:cid", connection_sql)
        self.assertEqual(connection_params, {"ts": timestamp, "cid": 3})

        binding_sql, binding_params = session.executions[1]
        self.assertIn("SET last_seen_at=:ts, is_online=1", binding_sql)
        self.assertIn("WHERE device_id=:did AND connection_id=:cid", binding_sql)
        self.assertEqual(binding_params, {"ts": timestamp, "did": 7, "cid": 3})

    def test_failure_telemetry_updates_error_timestamp_and_guard_binding_by_connection(self):
        session = _FakeSession()
        timestamp = datetime(2026, 5, 13, 8, 20, 0, tzinfo=timezone.utc)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            device_runtime._mark_connection_failure(3, message="Connection lost", timestamp=timestamp)
            device_runtime._mark_binding_offline(7, connection_id=3)

        connection_sql, connection_params = session.executions[0]
        self.assertIn("SET last_error=:msg, updated_at=:ts", connection_sql)
        self.assertIn("WHERE connection_id=:cid", connection_sql)
        self.assertEqual(connection_params, {"msg": "Connection lost", "ts": timestamp, "cid": 3})

        binding_sql, binding_params = session.executions[1]
        self.assertIn("SET is_online=0", binding_sql)
        self.assertIn("WHERE device_id=:did AND connection_id=:cid", binding_sql)
        self.assertEqual(binding_params, {"did": 7, "cid": 3})

    def test_add_command_event_flushes_parent_before_event_when_id_already_assigned(self):
        session = _FakeCommandEventSession()
        command = ControlCommand(
            device_id=7,
            request_uuid="request-1",
            requested_by="test",
            command_name="manual_text",
            status="queued",
        )
        command.command_id = 123

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            device_runtime._add_command_event(command, "queued", {"requested_by": "test"})

        event = next(item for item in session.objects if isinstance(item, ControlCommandEvent))
        self.assertEqual(event.command_id, 123)
        self.assertEqual(
            session.operations,
            [
                ("add_command", 123),
                ("flush", None),
                ("add_event", 123),
                ("flush", None),
            ],
        )

    def test_add_command_event_flushes_command_id_when_not_assigned(self):
        session = _FakeCommandEventSession()
        command = ControlCommand(
            device_id=7,
            request_uuid="request-2",
            requested_by="test",
            command_name="manual_text",
            status="queued",
        )

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            device_runtime._add_command_event(command, "queued", {"requested_by": "test"})

        event = next(item for item in session.objects if isinstance(item, ControlCommandEvent))
        self.assertEqual(event.command_id, 123)
        self.assertEqual(
            session.operations,
            [
                ("add_command", None),
                ("flush", None),
                ("add_event", 123),
                ("flush", None),
            ],
        )

    def test_describe_device_command_error_prefers_persisted_device_detail(self):
        command = ControlCommand(
            command_id=391267,
            device_id=3,
            request_uuid="request-391267",
            requested_by="process_manual",
            command_name="get_status",
            status="failed",
            error_message="Huber PB address mismatch: sent 0A, got 00.",
        )
        exc = device_runtime.DeviceCommandError(
            "Device command execution failed.",
            status_code=502,
            command=command,
        )

        message = device_runtime.describe_device_command_error(exc)

        self.assertIn("command 'get_status'", message)
        self.assertIn("command_id=391267", message)
        self.assertIn("device_id=3", message)
        self.assertIn("Huber PB address mismatch: sent 0A, got 00.", message)
        self.assertNotEqual(message, "Device command execution failed.")

    def test_execute_device_command_commits_queued_and_sent_before_driver_io(self):
        session = _FakeExecuteCommandSession()
        driver = _FakeDriver(session)
        connection = SimpleNamespace(
            connection_id=3,
            is_enabled=True,
            transport_type="tcp_socket",
        )
        binding = SimpleNamespace(
            device_id=7,
            connection_id=3,
            connection=connection,
        )
        device = SimpleNamespace(
            device_id=7,
            protocol="fake_protocol",
            current_binding=binding,
        )

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with patch.object(device_runtime, "get_driver", return_value=driver):
                execution = device_runtime.execute_device_command(
                    device,
                    command_name="manual_text",
                    payload={},
                    requested_by="test",
                )

        self.assertEqual(driver.commit_calls_at_execute, 2)
        self.assertEqual(execution.command.command_id, 5728)
        self.assertEqual(execution.command.status, "acked")
        self.assertGreaterEqual(session.commit_calls, 3)
        event_types = [operation[1] for operation in session.operations if operation[0] == "add_event"]
        self.assertEqual(event_types, ["queued", "sent", "response"])


if __name__ == "__main__":
    unittest.main()
