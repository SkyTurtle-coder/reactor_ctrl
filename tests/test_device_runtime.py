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


class _FakeHuberSetpointDriver:
    uses_transport = False

    def execute(self, *, transport, request):
        return DeviceCommandResult(
            acknowledged=True,
            response_text="OK",
            response_hex="4f4b",
            metadata={"value": 25.0, "verified_setpoint": 24.95},
        )


class _FakePersistentTransport:
    def __init__(self):
        self.connect_calls = 0
        self.close_calls = 0
        self.recv_size = 4096

    def connect(self):
        self.connect_calls += 1

    def close(self):
        self.close_calls += 1

    def is_connected(self):
        return True

    def bind_runtime_control(self, *, cancellation_token=None):
        return None


class _FakePersistentDriver:
    uses_transport = True
    persistent_transport = True

    def __init__(self):
        self.transport_ids = []

    def execute(self, *, transport, request):
        self.transport_ids.append(id(transport))
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

    def test_execute_device_command_commits_running_and_sent_before_driver_io(self):
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
        self.assertEqual(execution.command.status, "completed")
        self.assertGreaterEqual(session.commit_calls, 3)
        event_types = [operation[1] for operation in session.operations if operation[0] == "add_event"]
        self.assertEqual(event_types, ["running", "sent", "response", "completed"])

    def test_execute_device_command_uses_immediate_result_measurement_for_huber_setpoint(self):
        session = _FakeExecuteCommandSession()
        connection = SimpleNamespace(
            connection_id=4,
            is_enabled=True,
            transport_type="tcp_socket",
            cc230_setpoint_write_mode=None,
        )
        binding = SimpleNamespace(
            device_id=8,
            connection_id=4,
            connection=connection,
        )
        device = SimpleNamespace(
            device_id=8,
            protocol="huber_cc230",
            current_binding=binding,
        )
        command = ControlCommand(
            command_id=901,
            device_id=8,
            request_uuid="req-901",
            requested_by="test",
            command_name="set_setpoint",
            status="sent",
        )
        measurement = SimpleNamespace(measurement_id=17, channel_code="setpoint_C")
        captured = {}
        fake_channel = SimpleNamespace(channel_id=18, channel_code="setpoint_C")

        spec = device_runtime._result_measurement_spec(
            device=device,
            command=command,
            command_name="set_setpoint",
            payload={"temp_c": 25.0},
            result=DeviceCommandResult(
                acknowledged=True,
                response_text="OK",
                response_hex="4f4b",
                metadata={"value": 25.0, "verified_setpoint": 24.95},
            ),
        )
        self.assertEqual(spec["raw_payload"]["command_id"], 901)
        self.assertEqual(spec["raw_payload"]["command_name"], "set_setpoint")
        self.assertEqual(spec["numeric_value"], 24.95)

        def fake_create_measurement_record(**kwargs):
            captured.update(kwargs)
            return measurement

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with patch.object(device_runtime, "get_driver", return_value=_FakeHuberSetpointDriver()):
                with patch.object(device_runtime, "_upsert_measurement_channel", return_value=fake_channel):
                    with patch.object(device_runtime, "_create_measurement_record", side_effect=fake_create_measurement_record):
                        with patch.object(device_runtime, "_persist_measurement", return_value=None):
                            execution = device_runtime.execute_device_command(
                                device,
                                command_name="set_setpoint",
                                payload={"temp_c": 25.0},
                                requested_by="test",
                            )

        self.assertIs(execution.measurement, measurement)
        self.assertEqual(captured["command"].command_name, "set_setpoint")
        self.assertEqual(captured["channel"].channel_code, "setpoint_C")
        self.assertEqual(captured["numeric_value"], 24.95)
        self.assertEqual(captured["measured_at"].tzinfo, timezone.utc)
        self.assertEqual(captured["raw_payload"]["command_id"], execution.command.command_id)

    def test_persistent_driver_reuses_transport_for_same_connection_and_timeouts(self):
        session = _FakeExecuteCommandSession()
        driver = _FakePersistentDriver()
        transport = _FakePersistentTransport()
        connection = SimpleNamespace(
            connection_id=42,
            is_enabled=True,
            transport_type="tcp_socket",
            tcp_host="127.0.0.1",
            tcp_port=4305,
        )
        binding = SimpleNamespace(device_id=7, connection_id=42, connection=connection)
        device = SimpleNamespace(device_id=7, protocol="persistent_fake", current_binding=binding)

        device_runtime._PERSISTENT_TRANSPORTS.clear()
        try:
            with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
                with patch.object(device_runtime, "get_driver", return_value=driver):
                    with patch.object(device_runtime, "build_transport", return_value=transport) as build_transport:
                        for _ in range(2):
                            device_runtime.execute_device_command(
                                device,
                                command_name="read_weight",
                                payload={"response_timeout_ms": 1200},
                                requested_by="test",
                            )

            self.assertEqual(build_transport.call_count, 1)
            self.assertEqual(driver.transport_ids, [id(transport), id(transport)])
            self.assertEqual(transport.close_calls, 0)
        finally:
            device_runtime._PERSISTENT_TRANSPORTS.clear()


# ---------------------------------------------------------------------------
# Helpers for transient DB error simulation
# ---------------------------------------------------------------------------

def _make_mysql_operational_error(code: int, message: str = "db error") -> Exception:
    """Create a SQLAlchemy OperationalError wrapping a pymysql-style error."""
    from sqlalchemy.exc import OperationalError
    orig = Exception(code, message)
    orig.args = (code, message)
    return OperationalError(statement="UPDATE control_command", params={}, orig=orig)


def _make_transient_db_device_command_error(command=None) -> "device_runtime.DeviceCommandError":
    """Create a DeviceCommandError whose __cause__ is a transient MySQL 1020 error."""
    cause = _make_mysql_operational_error(1020)
    exc = device_runtime.DeviceCommandError(
        "Device command log persistence failed during response.",
        status_code=500,
        command=command,
    )
    exc.__cause__ = cause
    return exc


# ---------------------------------------------------------------------------
# is_transient_db_error
# ---------------------------------------------------------------------------

class IsTransientDbErrorTests(unittest.TestCase):
    def test_mysql_1020_is_transient(self):
        exc = _make_mysql_operational_error(1020)
        self.assertTrue(device_runtime.is_transient_db_error(exc))

    def test_mysql_1205_is_transient(self):
        exc = _make_mysql_operational_error(1205)
        self.assertTrue(device_runtime.is_transient_db_error(exc))

    def test_mysql_1213_is_transient(self):
        exc = _make_mysql_operational_error(1213)
        self.assertTrue(device_runtime.is_transient_db_error(exc))

    def test_mysql_1062_is_not_transient(self):
        exc = _make_mysql_operational_error(1062)
        self.assertFalse(device_runtime.is_transient_db_error(exc))

    def test_generic_exception_is_not_transient(self):
        self.assertFalse(device_runtime.is_transient_db_error(RuntimeError("something")))

    def test_value_error_is_not_transient(self):
        self.assertFalse(device_runtime.is_transient_db_error(ValueError("bad value")))


# ---------------------------------------------------------------------------
# _add_and_commit_command_phase retry tests
# ---------------------------------------------------------------------------

class CommitPhaseRetryTests(unittest.TestCase):
    """_add_and_commit_command_phase retries the full add+commit sequence on 1020."""

    def _make_command(self, command_id=99):
        cmd = ControlCommand(
            device_id=1,
            request_uuid="req-retry",
            requested_by="test",
            command_name="set_speed",
            status="running",
        )
        cmd.command_id = command_id
        return cmd

    def test_commit_phase_retries_on_mysql_1020_then_succeeds(self):
        """First attempt raises MySQL 1020; second attempt succeeds."""
        call_count = {"n": 0}

        class RetrySession(_FakeExecuteCommandSession):
            def flush(self, *args, **kwargs):
                # Assign command_id so event can be created
                targets = args[0] if args else self.objects
                if targets is None:
                    targets = self.objects
                for item in targets:
                    if isinstance(item, ControlCommand) and item.command_id is None:
                        item.command_id = 99
                self.operations.append(("flush", None))

            def commit(self):
                call_count["n"] += 1
                self.operations.append(("commit", call_count["n"]))
                if call_count["n"] == 1:
                    raise _make_mysql_operational_error(1020)

        session = RetrySession()
        cmd = self._make_command(99)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            # Should not raise — second attempt succeeds
            device_runtime._add_and_commit_command_phase(cmd, "sent", "sent", {"info": "x"})

        # Two commits attempted (1st failed, 2nd succeeded)
        commit_ops = [op for op in session.operations if op[0] == "commit"]
        self.assertEqual(len(commit_ops), 2)
        # Rollback was called after first failure
        rollback_ops = [op for op in session.operations if op[0] == "rollback"]
        self.assertGreaterEqual(len(rollback_ops), 1)

    def test_commit_phase_raises_device_error_after_exhausting_retries(self):
        """Three consecutive 1020 errors → DeviceCommandError raised."""

        class AlwaysFailSession(_FakeExecuteCommandSession):
            def flush(self, *args, **kwargs):
                targets = args[0] if args else self.objects
                if targets is None:
                    targets = self.objects
                for item in targets:
                    if isinstance(item, ControlCommand) and item.command_id is None:
                        item.command_id = 99
                self.operations.append(("flush", None))

            def commit(self):
                self.operations.append(("commit_attempt", None))
                raise _make_mysql_operational_error(1020)

        session = AlwaysFailSession()
        cmd = self._make_command(99)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with self.assertRaises(device_runtime.DeviceCommandError) as ctx:
                device_runtime._add_and_commit_command_phase(cmd, "sent", "sent", {"info": "x"})

        self.assertEqual(ctx.exception.status_code, 500)
        # Session must have been rolled back — not in pending-rollback state
        rollback_ops = [op for op in session.operations if op[0] == "rollback"]
        self.assertGreaterEqual(len(rollback_ops), 1)

    def test_session_not_in_pending_rollback_after_transient_error(self):
        """After a 1020 flush error, session.rollback() is called and session is reusable."""
        rolled_back = {"called": False}

        class FlushFailSession(_FakeExecuteCommandSession):
            def flush(self, *args, **kwargs):
                targets = args[0] if args else self.objects
                if targets is None:
                    targets = self.objects
                # Only fail on second flush (event flush)
                flush_count = sum(1 for op in self.operations if op[0] == "flush")
                if flush_count >= 1:
                    raise _make_mysql_operational_error(1020)
                for item in targets:
                    if isinstance(item, ControlCommand) and item.command_id is None:
                        item.command_id = 99
                self.operations.append(("flush", None))

            def rollback(self):
                rolled_back["called"] = True
                self.rollback_calls += 1
                self.operations.append(("rollback", self.rollback_calls))

        session = FlushFailSession()
        cmd = self._make_command(99)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with self.assertRaises(device_runtime.DeviceCommandError):
                device_runtime._add_and_commit_command_phase(cmd, "sent", "sent", {})

        # Rollback must have been called after the transient flush error
        self.assertTrue(rolled_back["called"], "session.rollback() must be called after a transient flush error")


# ---------------------------------------------------------------------------
# _add_command_event retry on deadlock (1213)
# ---------------------------------------------------------------------------

class AddCommandEventRetryTests(unittest.TestCase):
    def test_add_event_flushes_normally(self):
        """Baseline: _add_command_event works with a clean session."""
        session = _FakeCommandEventSession()
        command = ControlCommand(
            device_id=1,
            request_uuid="req-event",
            requested_by="test",
            command_name="ping",
            status="running",
        )
        command.command_id = 77

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            device_runtime._add_command_event(command, "running", {"info": "ok"})

        event = next(item for item in session.objects if isinstance(item, ControlCommandEvent))
        self.assertEqual(event.command_id, 77)
        self.assertEqual(event.event_type, "running")


# ---------------------------------------------------------------------------
# Post-execution DB failure does not raise when device comms succeeded
# ---------------------------------------------------------------------------

class ExecuteCommandPostSuccessDbFailureTests(unittest.TestCase):
    """Device comms succeed; transient 1020 in response/completed commit does not raise."""

    def _make_session_with_commit_failure(self, fail_on_commit: int, error_code: int = 1020):
        """Return a session that raises a transient DB error on the N-th commit."""
        call_count = {"n": 0}

        class _Session(_FakeExecuteCommandSession):
            def commit(self):
                call_count["n"] += 1
                self.commit_calls += 1
                self.operations.append(("commit", call_count["n"]))
                if call_count["n"] == fail_on_commit:
                    raise _make_mysql_operational_error(error_code)

        return _Session()

    def test_post_success_response_commit_failure_1020_does_not_raise(self):
        """commit() for 'response' phase raises 1020 on all retries → no exception raised."""
        # The response phase commit is commit #3 (RUNNING, SENT, then response).
        # With retry, commit #3 and #4 and #5 (all attempts) fail — should not raise.
        fail_calls = set()
        call_count = {"n": 0}

        class _Session(_FakeExecuteCommandSession):
            def commit(self):
                call_count["n"] += 1
                self.commit_calls += 1
                self.operations.append(("commit", call_count["n"]))
                # Fail all commits from 3rd onward (response, completed phases)
                if call_count["n"] >= 3:
                    raise _make_mysql_operational_error(1020)

        session = _Session()
        driver = _FakeDriver(session)
        connection = SimpleNamespace(connection_id=5, is_enabled=True, transport_type="tcp_socket")
        binding = SimpleNamespace(device_id=9, connection_id=5, connection=connection)
        device = SimpleNamespace(device_id=9, protocol="fake", current_binding=binding)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with patch.object(device_runtime, "get_driver", return_value=driver):
                # Must NOT raise DeviceCommandError
                execution = device_runtime.execute_device_command(
                    device,
                    command_name="manual_text",
                    payload={},
                    requested_by="test",
                )

        # Device comms succeeded — result is returned
        self.assertIsNotNone(execution)
        self.assertEqual(execution.result.response_text, "OK")

    def test_post_success_completed_transition_failure_1020_does_not_raise(self):
        """The final COMPLETED transition raises 1020 after all retries → no exception."""
        call_count = {"n": 0}

        class _Session(_FakeExecuteCommandSession):
            def commit(self):
                call_count["n"] += 1
                self.commit_calls += 1
                self.operations.append(("commit", call_count["n"]))
                # Let RUNNING and SENT commits succeed; fail everything from response onward
                if call_count["n"] >= 3:
                    raise _make_mysql_operational_error(1020)

        session = _Session()
        driver = _FakeDriver(session)
        connection = SimpleNamespace(connection_id=5, is_enabled=True, transport_type="tcp_socket")
        binding = SimpleNamespace(device_id=9, connection_id=5, connection=connection)
        device = SimpleNamespace(device_id=9, protocol="fake", current_binding=binding)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            with patch.object(device_runtime, "get_driver", return_value=driver):
                execution = device_runtime.execute_device_command(
                    device,
                    command_name="manual_text",
                    payload={},
                    requested_by="test",
                )

        self.assertEqual(execution.result.response_text, "OK")


class TransitionControlCommandRecordTests(unittest.TestCase):
    class _Session(_FakeExecuteCommandSession):
        def __init__(self, command):
            super().__init__()
            self.command = command
            self.command_flush_attempts = 0

        def get(self, model, item_id):
            if model is ControlCommand and int(item_id) == int(self.command.command_id):
                return self.command
            return None

        def refresh(self, _item):
            return None

    def test_transition_retries_after_transient_mysql_1020(self):
        command = ControlCommand(
            command_id=77,
            device_id=1,
            request_uuid="req-transition-77",
            requested_by="tester",
            command_name="manual_text",
            status="running",
        )

        class RetrySession(self._Session):
            def flush(self, *args, **kwargs):
                targets = list(args[0]) if args else list(self.objects)
                if any(isinstance(item, ControlCommand) for item in targets):
                    self.command_flush_attempts += 1
                    if self.command_flush_attempts == 1:
                        raise _make_mysql_operational_error(1020)
                super().flush(*args, **kwargs)

        session = RetrySession(command)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            updated = device_runtime.transition_control_command_record(
                command,
                device_runtime.RuntimeStatus.SENT,
                event_payload={"sent_at": "2026-05-26T12:00:00+00:00"},
            )

        self.assertEqual(updated.status, "sent")
        self.assertGreaterEqual(session.rollback_calls, 1)
        self.assertGreaterEqual(session.commit_calls, 1)

    def test_completed_to_sent_transition_is_noop(self):
        command = ControlCommand(
            command_id=88,
            device_id=1,
            request_uuid="req-transition-88",
            requested_by="tester",
            command_name="manual_text",
            status="completed",
        )
        session = self._Session(command)

        with patch.object(device_runtime, "db", SimpleNamespace(session=session)):
            updated = device_runtime.transition_control_command_record(
                command,
                device_runtime.RuntimeStatus.SENT,
                event_payload={"sent_at": "2026-05-26T12:05:00+00:00"},
            )

        self.assertEqual(updated.status, "completed")
        self.assertEqual(session.commit_calls, 0)
        self.assertFalse(any(op[0] == "add_event" for op in session.operations))

    def test_describe_device_command_error_uses_persistence_prefix(self):
        command = ControlCommand(
            command_id=91,
            device_id=5,
            request_uuid="req-transition-91",
            requested_by="tester",
            command_name="set_speed",
            status="sent",
            error_message="Persistence error while updating control_command during sent.",
        )
        exc = device_runtime.ControlCommandPersistenceError(
            "Persistence error while updating control_command during sent.",
            status_code=500,
            command=command,
            details={"error_kind": "persistence"},
        )

        message = device_runtime.describe_device_command_error(exc)

        self.assertIn("Persistence error", message)
        self.assertNotIn("Device command failed", message)


if __name__ == "__main__":
    unittest.main()
