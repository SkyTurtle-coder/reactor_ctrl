import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from reactor_app.models import ControlCommand, ControlCommandEvent
from reactor_app.services import device_runtime


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

    def flush(self):
        for item in self.objects:
            if isinstance(item, ControlCommand) and item.command_id is None:
                item.command_id = 123
        self.operations.append(("flush", None))


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

    def test_add_command_event_flushes_parent_command_before_child_event(self):
        session = _FakeCommandEventSession()
        command = ControlCommand(
            device_id=7,
            request_uuid="request-1",
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


if __name__ == "__main__":
    unittest.main()
