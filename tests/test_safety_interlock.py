import unittest
from types import SimpleNamespace
from unittest.mock import patch

import reactor_app.services.safety_interlock as safety_interlock
from reactor_app.services.command_model import CommandPriority, CommandSource, DeviceCommand
from reactor_app.services.runtime_status import ProgramStatus


class SafetyInterlockTests(unittest.TestCase):
    def _db_with_state(self, state):
        return SimpleNamespace(session=SimpleNamespace(get=lambda _model, _item_id: state))

    def _command(self, *, priority=CommandPriority.MANUAL):
        return DeviceCommand(
            device_id=7,
            command_type="set_setpoint",
            payload={"temp_c": 40},
            priority=priority,
            source=CommandSource.API,
            requested_by="test",
        )

    def test_stop_requested_blocks_non_safety_command(self):
        state = SimpleNamespace(status="running", stop_requested=True)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.safety_interlock_block_details(self._command())

        self.assertIsNotNone(details)
        self.assertEqual(details["runtime_status"], ProgramStatus.SAFETY_STOP)
        self.assertEqual(details["program_status"], "running")
        self.assertTrue(details["stop_requested"])

    def test_safety_stop_status_typo_alias_blocks_non_safety_command(self):
        state = SimpleNamespace(status="Safty_STOP", stop_requested=False)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.safety_interlock_block_details(self._command())

        self.assertIsNotNone(details)
        self.assertEqual(details["program_status"], "safty_stop")

    def test_safety_priority_command_is_allowed_during_safety_stop(self):
        state = SimpleNamespace(status=ProgramStatus.SAFETY_STOP, stop_requested=True)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.safety_interlock_block_details(
                self._command(priority=CommandPriority.SAFETY)
            )

        self.assertIsNone(details)

    def test_emergency_stop_command_is_allowed_during_safety_stop(self):
        state = SimpleNamespace(status=ProgramStatus.SAFETY_STOP, stop_requested=True)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.safety_interlock_block_details(
                self._command(priority=CommandPriority.EMERGENCY_STOP)
            )

        self.assertIsNone(details)

    def test_manual_safe_off_target_is_allowed_during_safety_stop(self):
        state = SimpleNamespace(status=ProgramStatus.SAFETY_STOP, stop_requested=True)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.unsafe_manual_target_blocked(
                desired_is_on=False,
                desired_speed=0,
            )

        self.assertIsNone(details)

    def test_manual_energizing_target_is_blocked_during_safety_stop(self):
        state = SimpleNamespace(status=ProgramStatus.SAFETY_STOP, stop_requested=True)

        with patch.object(safety_interlock, "db", self._db_with_state(state)):
            details = safety_interlock.unsafe_manual_target_blocked(
                desired_is_on=True,
                desired_speed=200,
            )

        self.assertIsNotNone(details)
        self.assertEqual(details["runtime_status"], ProgramStatus.SAFETY_STOP)
        self.assertTrue(details["desired_is_on"])
        self.assertEqual(details["desired_speed"], 200.0)


if __name__ == "__main__":
    unittest.main()
