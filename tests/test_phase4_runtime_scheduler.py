import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from reactor_app.models import Device, ReactorBuild, RecipeProgramState
from reactor_app.services import device_manual_runtime, recipe_program_runtime
from reactor_app.services.command_model import CommandPriority, CommandSource, DeviceCommand
from reactor_app.services.runtime_scheduler import (
    RuntimeCommandInterruptedError,
    RuntimeCommandQueue,
    RuntimeCommandScheduler,
    RuntimeWorker,
    ScheduledRuntimeCommand,
)
from reactor_app.services.runtime_status import ProgramStatus, RuntimeStatus


class RuntimeCommandQueueTests(unittest.TestCase):
    def _scheduled(
        self,
        device_id: int,
        *,
        command_type: str = "noop",
        priority: int = CommandPriority.MANUAL,
        source: str = CommandSource.API,
        result=None,
        error: Exception | None = None,
    ) -> ScheduledRuntimeCommand:
        command = DeviceCommand(
            device_id=device_id,
            command_type=command_type,
            payload={},
            priority=priority,
            source=source,
            requested_by="tester",
        )

        def execute():
            if error is not None:
                raise error
            return result if result is not None else command_type

        return ScheduledRuntimeCommand(command=command, execute=execute)

    def test_enqueue_and_dequeue_returns_pending_command(self):
        queue = RuntimeCommandQueue()
        item = self._scheduled(1, command_type="manual_text")

        queue.enqueue(item)
        next_item = queue.dequeue_next(timeout_s=0)

        self.assertIs(next_item, item)

    def test_priority_beats_fifo(self):
        queue = RuntimeCommandQueue()
        polling = self._scheduled(1, command_type="poll", priority=CommandPriority.POLLING, source=CommandSource.POLLER)
        emergency = self._scheduled(1, command_type="emergency_stop", priority=CommandPriority.EMERGENCY_STOP)

        queue.enqueue(polling)
        queue.enqueue(emergency)

        next_item = queue.dequeue_next(timeout_s=0)
        self.assertIs(next_item, emergency)
        with self.assertRaises(RuntimeCommandInterruptedError):
            polling.future.result()
        self.assertEqual(polling.status, RuntimeStatus.PREEMPTED)

    def test_fifo_is_preserved_with_same_priority(self):
        queue = RuntimeCommandQueue()
        first = self._scheduled(1, command_type="cmd_a")
        second = self._scheduled(2, command_type="cmd_b")

        queue.enqueue(first)
        queue.enqueue(second)

        self.assertIs(queue.dequeue_next(timeout_s=0), first)
        queue.mark_running(first.command_id)
        queue.mark_completed(first.command_id, "done-a")
        self.assertIs(queue.dequeue_next(timeout_s=0), second)

    def test_only_one_command_per_device_can_run(self):
        queue = RuntimeCommandQueue()
        first = self._scheduled(1, command_type="cmd_a")
        second_same_device = self._scheduled(1, command_type="cmd_b")
        other_device = self._scheduled(2, command_type="cmd_c")

        queue.enqueue(first)
        queue.enqueue(second_same_device)
        queue.enqueue(other_device)

        queue.mark_running(first.command_id)
        next_item = queue.dequeue_next(timeout_s=0)

        self.assertIs(next_item, other_device)

    def test_cancel_pending_filters_by_device_and_source(self):
        queue = RuntimeCommandQueue()
        manual = self._scheduled(1, command_type="manual_text", source=CommandSource.API)
        recipe = self._scheduled(1, command_type="set_setpoint", priority=CommandPriority.RECIPE, source=CommandSource.RECIPE)
        other = self._scheduled(2, command_type="poll", priority=CommandPriority.POLLING, source=CommandSource.POLLER)

        queue.enqueue(manual)
        queue.enqueue(recipe)
        queue.enqueue(other)

        cancelled = queue.cancel_pending(device_id=1, source=CommandSource.API)

        self.assertEqual([item.command_id for item in cancelled], [manual.command_id])
        self.assertEqual(manual.status, RuntimeStatus.CANCELLED)
        with self.assertRaises(RuntimeCommandInterruptedError):
            manual.future.result()
        self.assertIs(queue.dequeue_next(timeout_s=0), recipe)

    def test_clear_device_queue_removes_all_pending_commands_for_device(self):
        queue = RuntimeCommandQueue()
        first = self._scheduled(4, command_type="poll_1", priority=CommandPriority.POLLING, source=CommandSource.POLLER)
        second = self._scheduled(4, command_type="poll_2", priority=CommandPriority.POLLING, source=CommandSource.POLLER)
        other = self._scheduled(5, command_type="manual_text")

        queue.enqueue(first)
        queue.enqueue(second)
        queue.enqueue(other)

        cleared = queue.clear_device_queue(4)

        self.assertEqual({item.device_id for item in cleared}, {4})
        self.assertIs(queue.dequeue_next(timeout_s=0), other)

    def test_newer_poll_replaces_older_pending_poll(self):
        queue = RuntimeCommandQueue()
        older = self._scheduled(9, command_type="poll_old", priority=CommandPriority.POLLING, source=CommandSource.POLLER)
        newer = self._scheduled(9, command_type="poll_new", priority=CommandPriority.POLLING, source=CommandSource.POLLER)

        queue.enqueue(older)
        queue.enqueue(newer)

        with self.assertRaises(RuntimeCommandInterruptedError):
            older.future.result()
        self.assertEqual(older.status, RuntimeStatus.SKIPPED)
        self.assertIs(queue.dequeue_next(timeout_s=0), newer)


class RuntimeWorkerTests(unittest.TestCase):
    def _scheduled(self, device_id: int, *, result=None, error: Exception | None = None) -> ScheduledRuntimeCommand:
        command = DeviceCommand(
            device_id=device_id,
            command_type="worker_test",
            payload={},
            priority=CommandPriority.MANUAL,
            source=CommandSource.API,
            requested_by="tester",
        )

        def execute():
            if error is not None:
                raise error
            return result if result is not None else "ok"

        return ScheduledRuntimeCommand(command=command, execute=execute)

    def test_process_next_executes_command(self):
        queue = RuntimeCommandQueue()
        worker = RuntimeWorker(queue)
        item = self._scheduled(1, result="done")

        queue.enqueue(item)

        self.assertTrue(worker.process_next(timeout_s=0))
        self.assertEqual(item.future.result(), "done")
        self.assertEqual(item.status, RuntimeStatus.COMPLETED)

    def test_process_next_marks_failure(self):
        queue = RuntimeCommandQueue()
        worker = RuntimeWorker(queue)
        item = self._scheduled(1, error=RuntimeError("boom"))

        queue.enqueue(item)

        self.assertTrue(worker.process_next(timeout_s=0))
        with self.assertRaises(RuntimeError):
            item.future.result()
        self.assertEqual(item.status, RuntimeStatus.FAILED)

    def test_worker_stops_cleanly(self):
        queue = RuntimeCommandQueue()
        worker = RuntimeWorker(queue, idle_wait_s=0.05)

        worker.start()
        time.sleep(0.05)
        worker.stop(timeout_s=1.0)

        self.assertFalse(worker.is_running())

    def test_worker_remains_usable_after_transient_failure(self):
        queue = RuntimeCommandQueue()
        worker = RuntimeWorker(queue)
        failing = self._scheduled(1, error=RuntimeError("transient"))
        succeeding = self._scheduled(2, result="recovered")

        queue.enqueue(failing)
        queue.enqueue(succeeding)

        self.assertTrue(worker.process_next(timeout_s=0))
        with self.assertRaises(RuntimeError):
            failing.future.result()

        self.assertTrue(worker.process_next(timeout_s=0))
        self.assertEqual(succeeding.future.result(), "recovered")

    def test_scheduler_submit_uses_background_worker(self):
        scheduler = RuntimeCommandScheduler(worker_count=1)
        item = self._scheduled(3, result="scheduled")

        try:
            result = scheduler.submit(item, wait=True, timeout_s=2.0)
        finally:
            scheduler.stop()

        self.assertEqual(result, "scheduled")


class RuntimeIntegrationTests(unittest.TestCase):
    class _FakeSocket:
        def __init__(self, sent):
            self.sent = sent
            self.timeouts = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def settimeout(self, value):
            self.timeouts.append(value)

        def sendall(self, payload):
            self.sent.append(payload)

    def test_immediate_stop_binding_sends_ika_stop_without_runtime_queue(self):
        app = Flask(__name__)
        sent = []
        device = SimpleNamespace(
            device_id=8,
            current_binding=SimpleNamespace(
                connection=SimpleNamespace(connection_id=4, tcp_host="10.90.95.178", tcp_port=4001)
            ),
        )
        fake_db = SimpleNamespace(session=SimpleNamespace(get=lambda _model, _item_id: device))

        def fake_create_connection(address, timeout):
            self.assertEqual(address, ("10.90.95.178", 4001))
            self.assertLessEqual(timeout, 0.35)
            return self._FakeSocket(sent)

        with patch.object(recipe_program_runtime, "db", fake_db):
            with patch.object(recipe_program_runtime.socket, "create_connection", side_effect=fake_create_connection):
                error = recipe_program_runtime._immediate_stop_binding(
                    app,
                    {
                        "actor": "Stirrer_01",
                        "device_id": 8,
                        "profile_id": "motor_rpm",
                        "protocol": "ika_eurostar_60",
                    },
                )

        self.assertIsNone(error)
        self.assertEqual(sent, [b"STOP_4 \r\n"])

    def test_immediate_stop_specs_use_protocol_specific_stop_telegrams(self):
        cases = [
            ("huber_cc230", b"STOP\r\n"),
            ("huber_ministat_cc", b"CA@ 00000\r\n"),
            ("huber_unistat_430", b"{M140000\r\n"),
        ]
        with patch.object(recipe_program_runtime, "_binding_tcp_endpoint", return_value=("127.0.0.1", 4004)):
            for protocol, payload in cases:
                spec = recipe_program_runtime._immediate_stop_spec_for_binding(
                    {
                        "actor": "Huber_01",
                        "device_id": 5,
                        "profile_id": "hc_system_temperature",
                        "protocol": protocol,
                    }
                )
                self.assertIsNotNone(spec)
                self.assertEqual(spec["payload"], payload)

    def test_manual_runtime_uses_dispatcher_with_priority_and_source(self):
        fake_session = SimpleNamespace(commit=MagicMock())
        device = Device(device_id=11, display_name="IKA", protocol="ika_eurostar_60")
        execution = SimpleNamespace(result=SimpleNamespace(response_text="300.0", metadata={"value": 300.0}))

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "dispatch_device_command", return_value=execution) as dispatch_command:
                response = device_manual_runtime._run_logged_manual_command(
                    device,
                    "IN_SP_4",
                    priority=CommandPriority.POLLING,
                    source=CommandSource.POLLER,
                )

        self.assertEqual(response, "300.0")
        command = dispatch_command.call_args.args[1]
        self.assertIsInstance(command, DeviceCommand)
        self.assertEqual(command.priority, CommandPriority.POLLING)
        self.assertEqual(command.source, CommandSource.POLLER)
        self.assertEqual(command.command_type, "manual_text")
        self.assertEqual(command.payload["text"], "IN_SP_4")

    def test_recipe_runtime_uses_dispatcher_with_recipe_priority(self):
        app = Flask(__name__)
        device = Device(device_id=7, display_name="Huber", protocol="huber_unistat_430")
        execution = SimpleNamespace(result=SimpleNamespace(metadata={"value": 25.0}))

        with patch.object(recipe_program_runtime, "dispatch_device_command", return_value=execution) as dispatch_command:
            result = recipe_program_runtime._execute_recipe_device_command(
                app,
                evaluation=None,
                actor="Huber_01",
                binding={"actor": "Huber_01", "protocol": "huber_unistat_430"},
                device=device,
                command_name="set_setpoint",
                payload={"temp_c": 25.0},
                requested_by="recipe_program",
            )

        self.assertIs(result, execution)
        command = dispatch_command.call_args.args[1]
        self.assertEqual(command.priority, CommandPriority.RECIPE)
        self.assertEqual(command.source, CommandSource.RECIPE)
        self.assertEqual(command.payload["response_timeout_ms"], 1200)
        self.assertIs(dispatch_command.call_args.kwargs["app"], app)

    def test_safe_stop_dispatches_as_safety_priority(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            display_name="Huber",
            protocol="huber_unistat_430",
            device_type="thermostat",
        )
        fake_session = SimpleNamespace(get=lambda model, item_id: device)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "dispatch_device_command", return_value=SimpleNamespace()) as dispatch_command:
                recipe_program_runtime._apply_safe_stop_to_binding(
                    app,
                    {
                        "actor": "Huber_01",
                        "device_id": 7,
                        "device_display_name": "Huber",
                        "profile_id": "hc_system_temperature",
                        "protocol": "huber_unistat_430",
                    },
                    requested_by="integration_stop",
                )

        command = dispatch_command.call_args_list[0].args[1]
        self.assertEqual(command.priority, CommandPriority.SAFETY)
        self.assertEqual(command.source, CommandSource.SYSTEM)

    def test_publish_program_stop_request_enters_safety_stop_state(self):
        state = RecipeProgramState(status="running", stop_requested=False)
        fake_db = SimpleNamespace(session=SimpleNamespace(flush=MagicMock(), commit=MagicMock()))

        with patch.object(recipe_program_runtime, "db", fake_db):
            recipe_program_runtime._publish_program_stop_request(state)

        self.assertEqual(state.status, ProgramStatus.SAFETY_STOP)
        self.assertTrue(state.stop_requested)
        fake_db.session.flush.assert_called_once()
        fake_db.session.commit.assert_called_once()

    def test_reconciler_recovers_persisted_safety_stop_state(self):
        app = Flask(__name__)
        state = RecipeProgramState(
            status=ProgramStatus.SAFETY_STOP,
            stop_requested=True,
            requested_by="operator",
            lease_owner="worker-1",
        )
        state.snapshot_json = {
            "bindings": [{"actor": "Stirrer_01", "device_id": 8, "profile_id": "motor_rpm"}],
            "safe_state": [],
        }
        fake_db = SimpleNamespace(
            session=SimpleNamespace(
                get=lambda _model, _item_id: state,
                refresh=lambda _item: None,
                commit=MagicMock(),
            )
        )

        with patch.object(recipe_program_runtime, "db", fake_db):
            with patch.object(recipe_program_runtime, "_ensure_open_program_run", return_value=None):
                with patch.object(recipe_program_runtime, "_apply_immediate_stop_to_bindings", return_value=[]):
                    with patch.object(
                        recipe_program_runtime,
                        "_apply_safe_stop_to_binding",
                        return_value=({"actor": "Stirrer_01", "device_id": 8, "is_on": False, "rpm": 0}, []),
                    ) as safe_stop:
                        with patch.object(recipe_program_runtime, "cancel_runtime_commands") as cancel_runtime_commands:
                            recipe_program_runtime._process_safety_stop_state(app, worker_id="worker-1")

        safe_stop.assert_called_once()
        self.assertEqual(cancel_runtime_commands.call_count, 2)
        self.assertNotIn("device_id", cancel_runtime_commands.call_args_list[0].kwargs)
        self.assertEqual(cancel_runtime_commands.call_args_list[1].kwargs["device_id"], 8)
        self.assertEqual(cancel_runtime_commands.call_args_list[1].kwargs["priority_gt"], CommandPriority.SAFETY)
        self.assertEqual(state.status, "stopped")
        self.assertFalse(state.stop_requested)
        self.assertIsNone(state.lease_owner)
        self.assertEqual(state.last_applied_targets_json["Stirrer_01"]["rpm"], 0)

    def test_stop_recipe_program_preempts_pending_lower_priority_commands(self):
        app = Flask(__name__)
        state = RecipeProgramState(status="running", requested_by="initial")
        state.snapshot_json = {
            "bindings": [
                {"actor": "Huber_01", "device_id": 7},
                {"actor": "Stirrer_01", "device_id": 8},
            ],
            "safe_state": [],
        }
        fake_db = SimpleNamespace(session=SimpleNamespace(flush=MagicMock()))

        def publish_stop_request(item):
            item.stop_requested = True

        with patch.object(recipe_program_runtime, "db", fake_db):
            with patch.object(recipe_program_runtime, "_ensure_program_state", return_value=state):
                with patch.object(recipe_program_runtime, "_ensure_open_program_run", return_value=None):
                    with patch.object(recipe_program_runtime, "_publish_program_stop_request", side_effect=publish_stop_request):
                        with patch.object(recipe_program_runtime, "_apply_immediate_stop_to_bindings", return_value=[]):
                            with patch.object(
                                recipe_program_runtime,
                                "_apply_safe_stop_to_binding",
                                return_value=({"actor": "Huber_01", "device_id": 7, "is_on": False}, []),
                            ):
                                with patch.object(recipe_program_runtime, "cancel_runtime_commands") as cancel_runtime_commands:
                                    result = recipe_program_runtime.stop_recipe_program(app, requested_by="operator")

        self.assertIs(result, state)
        self.assertEqual(cancel_runtime_commands.call_count, 3)
        global_call = cancel_runtime_commands.call_args_list[0]
        self.assertNotIn("device_id", global_call.kwargs)
        self.assertEqual(global_call.kwargs["priority_gt"], CommandPriority.SAFETY)
        self.assertEqual(global_call.kwargs["status"], RuntimeStatus.PREEMPTED)
        first_device_call = cancel_runtime_commands.call_args_list[1]
        self.assertEqual(first_device_call.kwargs["device_id"], 7)
        self.assertEqual(first_device_call.kwargs["priority_gt"], CommandPriority.SAFETY)
        self.assertEqual(first_device_call.kwargs["status"], RuntimeStatus.PREEMPTED)
        self.assertEqual(state.status, "stopped")

    def test_stop_recipe_program_resolves_build_bindings_when_snapshot_bindings_are_empty(self):
        app = Flask(__name__)
        build = ReactorBuild(reactor_build_id=1, build_name="Build", definition_json={})
        state = RecipeProgramState(status="running", requested_by="initial", reactor_build_id=1)
        state.snapshot_json = {
            "reactor_build_id": 1,
            "bindings": [],
            "safe_state": [],
        }
        fallback_binding = {
            "actor": "Stirrer_01",
            "device_id": 8,
            "device_display_name": "IKA Stirrer",
            "profile_id": "motor_rpm",
            "protocol": "ika_eurostar_60",
            "is_resolved": True,
        }

        def fake_get(model, item_id):
            if model is ReactorBuild and int(item_id) == 1:
                return build
            return None

        fake_db = SimpleNamespace(session=SimpleNamespace(flush=MagicMock(), get=fake_get))

        def publish_stop_request(item):
            item.stop_requested = True

        with patch.object(recipe_program_runtime, "db", fake_db):
            with patch.object(recipe_program_runtime, "_ensure_program_state", return_value=state):
                with patch.object(recipe_program_runtime, "_ensure_open_program_run", return_value=None):
                    with patch.object(recipe_program_runtime, "_publish_program_stop_request", side_effect=publish_stop_request):
                        with patch.object(
                            recipe_program_runtime,
                            "_build_target_lookup",
                            return_value={"Stirrer_01": fallback_binding},
                        ):
                            with patch.object(recipe_program_runtime, "_apply_immediate_stop_to_bindings", return_value=[]):
                                with patch.object(
                                    recipe_program_runtime,
                                    "_apply_safe_stop_to_binding",
                                    return_value=({"actor": "Stirrer_01", "device_id": 8, "is_on": False}, []),
                                ) as safe_stop:
                                    with patch.object(recipe_program_runtime, "cancel_runtime_commands") as cancel_runtime_commands:
                                        result = recipe_program_runtime.stop_recipe_program(app, requested_by="operator")

        self.assertIs(result, state)
        self.assertEqual(cancel_runtime_commands.call_count, 2)
        self.assertNotIn("device_id", cancel_runtime_commands.call_args_list[0].kwargs)
        self.assertEqual(cancel_runtime_commands.call_args_list[1].kwargs["device_id"], 8)
        safe_stop.assert_called_once()
        self.assertEqual(safe_stop.call_args.args[1]["device_id"], 8)
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.last_applied_targets_json["Stirrer_01"]["is_on"], False)

    def test_stop_recipe_program_without_any_safe_stop_binding_reports_error(self):
        app = Flask(__name__)
        state = RecipeProgramState(status="running", requested_by="initial")
        state.snapshot_json = {"bindings": [], "safe_state": []}
        fake_db = SimpleNamespace(session=SimpleNamespace(flush=MagicMock()))

        def publish_stop_request(item):
            item.stop_requested = True

        with patch.object(recipe_program_runtime, "db", fake_db):
            with patch.object(recipe_program_runtime, "_ensure_program_state", return_value=state):
                with patch.object(recipe_program_runtime, "_ensure_open_program_run", return_value=None):
                    with patch.object(recipe_program_runtime, "_publish_program_stop_request", side_effect=publish_stop_request):
                        with patch.object(recipe_program_runtime, "_apply_safe_stop_to_binding") as safe_stop:
                            result = recipe_program_runtime.stop_recipe_program(app, requested_by="operator")

        self.assertIs(result, state)
        safe_stop.assert_not_called()
        self.assertEqual(state.status, "error")
        self.assertIn("No recipe bindings or reactor_build_id", state.last_error or "")


if __name__ == "__main__":
    unittest.main()
