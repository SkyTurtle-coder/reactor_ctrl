import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from reactor_app.models import Device, RecipeProgramState
from reactor_app.services import device_manual_runtime, recipe_program_runtime
from reactor_app.services.command_model import CommandPriority, CommandSource, DeviceCommand
from reactor_app.services.runtime_scheduler import (
    RuntimeCommandInterruptedError,
    RuntimeCommandQueue,
    RuntimeCommandScheduler,
    RuntimeWorker,
    ScheduledRuntimeCommand,
)
from reactor_app.services.runtime_status import RuntimeStatus


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
                        with patch.object(
                            recipe_program_runtime,
                            "_apply_safe_stop_to_binding",
                            return_value=({"actor": "Huber_01", "device_id": 7, "is_on": False}, []),
                        ):
                            with patch.object(recipe_program_runtime, "cancel_runtime_commands") as cancel_runtime_commands:
                                result = recipe_program_runtime.stop_recipe_program(app, requested_by="operator")

        self.assertIs(result, state)
        self.assertEqual(cancel_runtime_commands.call_count, 2)
        first_call = cancel_runtime_commands.call_args_list[0]
        self.assertEqual(first_call.kwargs["device_id"], 7)
        self.assertEqual(first_call.kwargs["priority_gt"], CommandPriority.SAFETY)
        self.assertEqual(first_call.kwargs["status"], RuntimeStatus.PREEMPTED)
        self.assertEqual(state.status, "stopped")


if __name__ == "__main__":
    unittest.main()
