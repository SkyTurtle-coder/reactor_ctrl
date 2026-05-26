"""Phase 3 — Runtime Decoupling and Command Dispatcher tests.

Covers:
- CommandPriority ordering and values
- CommandSource constants
- DeviceCommand construction and defaults
- RuntimeStatus / ProgramStatus constants and sets
- dispatch_device_command() routing and payload handling
- API imports the dispatcher (smoke-test the import chain)
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from reactor_app.services.command_model import CommandPriority, CommandSource, DeviceCommand
from reactor_app.services.command_dispatcher import dispatch_device_command
from reactor_app.services.runtime_status import ProgramStatus, RuntimeStatus


# ---------------------------------------------------------------------------
# CommandPriority ordering
# ---------------------------------------------------------------------------

class CommandPriorityTests(unittest.TestCase):
    def test_emergency_stop_is_highest_priority(self):
        self.assertLess(CommandPriority.EMERGENCY_STOP, CommandPriority.SAFETY)
        self.assertLess(CommandPriority.SAFETY, CommandPriority.RECIPE)
        self.assertLess(CommandPriority.RECIPE, CommandPriority.MANUAL)
        self.assertLess(CommandPriority.MANUAL, CommandPriority.POLLING)

    def test_emergency_stop_value_is_zero(self):
        self.assertEqual(int(CommandPriority.EMERGENCY_STOP), 0)

    def test_polling_is_lowest_priority(self):
        all_priorities = [
            CommandPriority.EMERGENCY_STOP,
            CommandPriority.SAFETY,
            CommandPriority.RECIPE,
            CommandPriority.MANUAL,
            CommandPriority.POLLING,
        ]
        self.assertEqual(max(all_priorities), CommandPriority.POLLING)
        self.assertEqual(min(all_priorities), CommandPriority.EMERGENCY_STOP)

    def test_priorities_are_sortable(self):
        """Sorting by priority gives emergency-stop-first order."""
        unsorted = [CommandPriority.POLLING, CommandPriority.MANUAL,
                    CommandPriority.EMERGENCY_STOP, CommandPriority.RECIPE]
        expected = [CommandPriority.EMERGENCY_STOP, CommandPriority.RECIPE,
                    CommandPriority.MANUAL, CommandPriority.POLLING]
        self.assertEqual(sorted(unsorted), expected)

    def test_int_comparison_with_raw_int(self):
        self.assertLess(0, CommandPriority.MANUAL)
        self.assertGreater(9, CommandPriority.RECIPE)


# ---------------------------------------------------------------------------
# CommandSource constants
# ---------------------------------------------------------------------------

class CommandSourceTests(unittest.TestCase):
    def test_api_source(self):
        self.assertEqual(CommandSource.API, "api")

    def test_recipe_source(self):
        self.assertEqual(CommandSource.RECIPE, "recipe")

    def test_manual_reconciler_source(self):
        self.assertEqual(CommandSource.MANUAL_RECONCILER, "manual_reconciler")

    def test_poller_source(self):
        self.assertEqual(CommandSource.POLLER, "poller")

    def test_system_source(self):
        self.assertEqual(CommandSource.SYSTEM, "system")

    def test_all_sources_are_lowercase_strings(self):
        for attr in vars(CommandSource):
            if attr.startswith("_"):
                continue
            value = getattr(CommandSource, attr)
            if callable(value):
                continue
            self.assertIsInstance(value, str)
            self.assertEqual(value, value.lower(), msg=f"{attr} is not lowercase")


# ---------------------------------------------------------------------------
# DeviceCommand construction
# ---------------------------------------------------------------------------

class DeviceCommandTests(unittest.TestCase):
    def _make(self, **overrides):
        defaults = dict(
            device_id=1,
            command_type="set_setpoint",
            payload={"temp_c": 25.0},
        )
        defaults.update(overrides)
        return DeviceCommand(**defaults)

    def test_required_fields_are_stored(self):
        cmd = self._make()
        self.assertEqual(cmd.device_id, 1)
        self.assertEqual(cmd.command_type, "set_setpoint")
        self.assertEqual(cmd.payload, {"temp_c": 25.0})

    def test_default_priority_is_manual(self):
        cmd = self._make()
        self.assertEqual(cmd.priority, CommandPriority.MANUAL)

    def test_default_source_is_api(self):
        cmd = self._make()
        self.assertEqual(cmd.source, CommandSource.API)

    def test_default_requested_by_is_unknown(self):
        cmd = self._make()
        self.assertEqual(cmd.requested_by, "unknown")

    def test_command_id_is_auto_generated_uuid(self):
        import re
        cmd = self._make()
        self.assertRegex(
            cmd.command_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )

    def test_each_command_gets_unique_id(self):
        ids = {self._make().command_id for _ in range(10)}
        self.assertEqual(len(ids), 10)

    def test_created_at_is_utc_datetime(self):
        cmd = self._make()
        self.assertIsInstance(cmd.created_at, datetime)
        self.assertIsNotNone(cmd.created_at.tzinfo)
        self.assertEqual(cmd.created_at.tzinfo, timezone.utc)

    def test_custom_priority_and_source(self):
        cmd = self._make(
            priority=CommandPriority.EMERGENCY_STOP,
            source=CommandSource.RECIPE,
            requested_by="test_runner",
        )
        self.assertEqual(cmd.priority, CommandPriority.EMERGENCY_STOP)
        self.assertEqual(cmd.source, CommandSource.RECIPE)
        self.assertEqual(cmd.requested_by, "test_runner")

    def test_optional_fields_default_to_none(self):
        cmd = self._make()
        self.assertIsNone(cmd.timeout_s)
        self.assertIsNone(cmd.correlation_id)

    def test_timeout_and_correlation_id_stored(self):
        cmd = self._make(timeout_s=2.5, correlation_id="abc-123")
        self.assertEqual(cmd.timeout_s, 2.5)
        self.assertEqual(cmd.correlation_id, "abc-123")

    def test_payload_is_independent_copy(self):
        original = {"temp_c": 25.0}
        cmd = self._make(payload=original)
        original["extra"] = "mutated"
        self.assertNotIn("extra", cmd.payload)


# ---------------------------------------------------------------------------
# RuntimeStatus constants
# ---------------------------------------------------------------------------

class RuntimeStatusTests(unittest.TestCase):
    def test_active_states_are_non_terminal(self):
        for s in RuntimeStatus.ACTIVE_STATES:
            self.assertNotIn(s, RuntimeStatus.TERMINAL)

    def test_terminal_set_contains_expected_statuses(self):
        self.assertIn("completed", RuntimeStatus.TERMINAL)
        self.assertIn("stopped", RuntimeStatus.TERMINAL)
        self.assertIn("error", RuntimeStatus.TERMINAL)

    def test_error_states_set(self):
        self.assertIn("failed", RuntimeStatus.ERROR_STATES)
        self.assertIn("error", RuntimeStatus.ERROR_STATES)
        self.assertIn("timeout", RuntimeStatus.ERROR_STATES)

    def test_all_string_constants_are_lowercase(self):
        for attr in vars(RuntimeStatus):
            if attr.startswith("_") or attr[0].isupper():
                continue
            value = getattr(RuntimeStatus, attr)
            if isinstance(value, str):
                self.assertEqual(value, value.lower(), msg=f"RuntimeStatus.{attr}")

    def test_idle_and_queued_not_in_terminal(self):
        self.assertNotIn(RuntimeStatus.IDLE, RuntimeStatus.TERMINAL)
        self.assertNotIn(RuntimeStatus.QUEUED, RuntimeStatus.TERMINAL)


# ---------------------------------------------------------------------------
# ProgramStatus constants
# ---------------------------------------------------------------------------

class ProgramStatusTests(unittest.TestCase):
    def test_running_is_active(self):
        self.assertIn(ProgramStatus.RUNNING, ProgramStatus.ACTIVE)

    def test_terminal_statuses(self):
        self.assertIn(ProgramStatus.COMPLETED, ProgramStatus.TERMINAL)
        self.assertIn(ProgramStatus.STOPPED, ProgramStatus.TERMINAL)
        self.assertIn(ProgramStatus.ERROR, ProgramStatus.TERMINAL)

    def test_idle_is_not_terminal(self):
        self.assertNotIn(ProgramStatus.IDLE, ProgramStatus.TERMINAL)

    def test_running_is_not_terminal(self):
        self.assertNotIn(ProgramStatus.RUNNING, ProgramStatus.TERMINAL)


# ---------------------------------------------------------------------------
# dispatch_device_command routing
# ---------------------------------------------------------------------------

class DispatchDeviceCommandTests(unittest.TestCase):
    def _make_command(self, **kwargs):
        defaults = dict(
            device_id=42,
            command_type="get_internal_temp",
            payload={},
            source=CommandSource.API,
            requested_by="test",
        )
        defaults.update(kwargs)
        return DeviceCommand(**defaults)

    def _make_device(self):
        device = MagicMock()
        device.device_id = 42
        return device

    def test_dispatches_to_execute_device_command(self):
        device = self._make_device()
        command = self._make_command()
        mock_result = MagicMock()

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command",
            return_value=mock_result,
        ) as mock_exec:
            result = dispatch_device_command(device, command)

        self.assertIs(result, mock_result)
        mock_exec.assert_called_once()
        args, kwargs = mock_exec.call_args
        self.assertIs(args[0], device)
        self.assertEqual(kwargs["command_name"], "get_internal_temp")
        self.assertEqual(kwargs["payload"], {})
        self.assertEqual(kwargs["requested_by"], "test")
        self.assertTrue(kwargs["acquire_lock"])

    def test_passes_acquire_lock_false(self):
        device = self._make_device()
        command = self._make_command()

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command"
        ) as mock_exec:
            dispatch_device_command(device, command, acquire_lock=False)

        _, kwargs = mock_exec.call_args
        self.assertFalse(kwargs["acquire_lock"])

    def test_timeout_s_injected_into_payload(self):
        device = self._make_device()
        command = self._make_command(timeout_s=2.0)

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command"
        ) as mock_exec:
            dispatch_device_command(device, command)

        _, kwargs = mock_exec.call_args
        payload = kwargs["payload"]
        self.assertEqual(payload["response_timeout_ms"], 2000)
        self.assertEqual(payload["connect_timeout_ms"], 2000)
        self.assertEqual(payload["write_timeout_ms"], 2000)

    def test_timeout_s_does_not_override_existing_payload_keys(self):
        device = self._make_device()
        command = self._make_command(
            payload={"response_timeout_ms": 500},
            timeout_s=5.0,
        )

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command"
        ) as mock_exec:
            dispatch_device_command(device, command)

        _, kwargs = mock_exec.call_args
        self.assertEqual(kwargs["payload"]["response_timeout_ms"], 500)

    def test_device_command_error_propagates(self):
        from reactor_app.services.device_runtime import DeviceCommandError
        device = self._make_device()
        command = self._make_command()

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command",
            side_effect=DeviceCommandError("timeout", status_code=504),
        ):
            with self.assertRaises(DeviceCommandError):
                dispatch_device_command(device, command)

    def test_emergency_stop_is_dispatchable(self):
        device = self._make_device()
        command = self._make_command(
            command_type="emergency_stop",
            priority=CommandPriority.EMERGENCY_STOP,
            source=CommandSource.API,
        )
        mock_result = MagicMock()

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command",
            return_value=mock_result,
        ) as mock_exec:
            result = dispatch_device_command(device, command)

        self.assertIs(result, mock_result)
        mock_exec.assert_called_once()

    def test_recipe_priority_is_dispatchable(self):
        device = self._make_device()
        command = self._make_command(
            command_type="set_setpoint",
            priority=CommandPriority.RECIPE,
            source=CommandSource.RECIPE,
            requested_by="recipe_reconciler",
        )

        with patch(
            "reactor_app.services.command_dispatcher.execute_device_command"
        ) as mock_exec:
            dispatch_device_command(device, command)

        _, kwargs = mock_exec.call_args
        self.assertEqual(kwargs["requested_by"], "recipe_reconciler")


# ---------------------------------------------------------------------------
# API import smoke test
# ---------------------------------------------------------------------------

class ApiDispatcherImportTests(unittest.TestCase):
    def test_api_imports_dispatch_device_command(self):
        import reactor_app.api as api_module
        self.assertTrue(
            hasattr(api_module, "dispatch_device_command"),
            "api.py must import dispatch_device_command from services",
        )

    def test_api_imports_command_priority(self):
        import reactor_app.api as api_module
        self.assertTrue(
            hasattr(api_module, "CommandPriority"),
            "api.py must import CommandPriority",
        )

    def test_api_imports_device_command(self):
        import reactor_app.api as api_module
        self.assertTrue(
            hasattr(api_module, "DeviceCommand"),
            "api.py must import DeviceCommand",
        )

    def test_services_exports_dispatcher(self):
        from reactor_app.services import dispatch_device_command
        self.assertTrue(callable(dispatch_device_command))

    def test_services_exports_command_priority(self):
        from reactor_app.services import CommandPriority
        self.assertTrue(issubclass(CommandPriority, int))

    def test_services_exports_runtime_status(self):
        from reactor_app.services import RuntimeStatus
        self.assertIsInstance(RuntimeStatus.TERMINAL, frozenset)

    def test_services_exports_program_status(self):
        from reactor_app.services import ProgramStatus
        self.assertIsInstance(ProgramStatus.TERMINAL, frozenset)
