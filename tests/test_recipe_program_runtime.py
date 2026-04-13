import unittest
from datetime import datetime, timedelta, timezone

from reactor_app.services.recipe_program_runtime import _evaluate_program_timeline


class RecipeProgramRuntimeTests(unittest.TestCase):
    def test_second_step_ramps_from_previous_rpm_target(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {"actor": "Stirrer_01", "task": "Start", "delta_time": 0, "rpm": 300},
            {"actor": "Stirrer_01", "task": "Ramp", "delta_time": 1, "rpm": 500},
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=30),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 1)
        self.assertEqual(evaluation["active_step"]["actor"], "Stirrer_01")
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 400.0)
        self.assertAlmostEqual(evaluation["step_progress"], 0.5)

    def test_identical_rpm_step_holds_target_for_delta_time(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {"actor": "Stirrer_01", "task": "Start", "delta_time": 0, "rpm": 500},
            {"actor": "Stirrer_01", "task": "Hold", "delta_time": 1, "rpm": 500},
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=30),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 1)
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 500.0)
        self.assertAlmostEqual(evaluation["step_remaining_seconds"], 30.0)

    def test_zero_delta_steps_apply_immediately_and_complete(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {"actor": "Stirrer_01", "task": "Start", "delta_time": 0, "rpm": 500},
            {"actor": "Stirrer_01", "task": "Stop", "delta_time": 0, "rpm": 0},
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at,
        )

        self.assertTrue(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 2)
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 0.0)


if __name__ == "__main__":
    unittest.main()
