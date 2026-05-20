import unittest
from datetime import datetime, timedelta, timezone

from reactor_app.models import RecipeProgramState
from reactor_app.services.recipe_program_runtime import _evaluate_program_timeline, recipe_program_state_to_dict


def _motor_step(task, delta_time, rpm=None, *, status_on=None, actor="Stirrer_01", priority=1):
    return {
        "actors": [
            {
                "actor_id": actor,
                "actor": actor,
                "priority": priority,
                "params": {
                    "status_on": status_on,
                    "target_temp_c": None,
                    "pressure_mbar_a": None,
                    "rpm": rpm,
                },
            }
        ],
        "task": task,
        "delta_time": delta_time,
    }


def _huber_step(task, delta_time, target_temp_c=None, *, status_on=None, actor="Huber_01", priority=1):
    return {
        "actors": [
            {
                "actor_id": actor,
                "actor": actor,
                "priority": priority,
                "params": {
                    "status_on": status_on,
                    "target_temp_c": target_temp_c,
                    "pressure_mbar_a": None,
                    "rpm": None,
                },
            }
        ],
        "task": task,
        "delta_time": delta_time,
    }


class RecipeProgramRuntimeTests(unittest.TestCase):
    def test_second_step_ramps_from_previous_rpm_target(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _motor_step("Start", 0, 300, status_on=True),
            _motor_step("Ramp", 1, 500),
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=30),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 1)
        self.assertEqual(evaluation["active_step"]["actors"][0]["actor"], "Stirrer_01")
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 400.0)
        self.assertAlmostEqual(evaluation["step_progress"], 0.5)

    def test_identical_rpm_step_holds_target_for_delta_time(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _motor_step("Start", 0, 500, status_on=True),
            _motor_step("Hold", 1, 500),
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
            _motor_step("Start", 0, 500, status_on=True),
            _motor_step("Stop", 0, None, status_on=False),
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
        self.assertFalse(evaluation["current_targets"]["Stirrer_01"]["is_on"])

    def test_temperature_step_ramps_from_previous_temperature_target(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _huber_step("Set initial temperature", 0, 20, status_on=True),
            _huber_step("Ramp temperature", 2, 40),
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=60),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 1)
        self.assertEqual(evaluation["active_step"]["actors"][0]["actor"], "Huber_01")
        self.assertAlmostEqual(evaluation["current_targets"]["Huber_01"]["temp"], 30.0)
        self.assertAlmostEqual(evaluation["step_progress"], 0.5)

    def test_multi_actor_step_ramps_selected_actors_in_parallel(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {
                "actors": [
                    {
                        "actor_id": "Huber_01",
                        "actor": "Huber_01",
                        "priority": 1,
                        "params": {"status_on": True, "target_temp_c": 20, "pressure_mbar_a": None, "rpm": None},
                    },
                    {
                        "actor_id": "Stirrer_01",
                        "actor": "Stirrer_01",
                        "priority": 2,
                        "params": {"status_on": True, "target_temp_c": None, "pressure_mbar_a": None, "rpm": 200},
                    },
                ],
                "task": "Initialize",
                "delta_time": 0,
            },
            {
                "actors": [
                    {
                        "actor_id": "Huber_01",
                        "actor": "Huber_01",
                        "priority": 1,
                        "params": {"status_on": None, "target_temp_c": 40, "pressure_mbar_a": None, "rpm": None},
                    },
                    {
                        "actor_id": "Stirrer_01",
                        "actor": "Stirrer_01",
                        "priority": 2,
                        "params": {"status_on": None, "target_temp_c": None, "pressure_mbar_a": None, "rpm": 600},
                    },
                ],
                "task": "Parallel ramp",
                "delta_time": 2,
            },
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=60),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual([item["actor"] for item in evaluation["active_step"]["actors"]], ["Huber_01", "Stirrer_01"])
        self.assertAlmostEqual(evaluation["current_targets"]["Huber_01"]["temp"], 30.0)
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 400.0)
        self.assertEqual(evaluation["current_targets"]["Huber_01"]["_priority"], 1)
        self.assertEqual(evaluation["current_targets"]["Stirrer_01"]["_priority"], 2)

    def test_multi_actor_step_uses_actor_scoped_params_and_priority_order(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {
                "actors": [
                    {
                        "actor_id": "Huber_01",
                        "actor": "Huber_01",
                        "priority": 2,
                        "params": {"status_on": True, "target_temp_c": 20, "pressure_mbar_a": None, "rpm": None},
                    },
                    {
                        "actor_id": "Stirrer_01",
                        "actor": "Stirrer_01",
                        "priority": 1,
                        "params": {"status_on": True, "target_temp_c": None, "pressure_mbar_a": None, "rpm": 200},
                    },
                ],
                "task": "Initialize",
                "delta_time": 0,
            },
            {
                "actors": [
                    {
                        "actor_id": "Huber_01",
                        "actor": "Huber_01",
                        "priority": 2,
                        "params": {"status_on": None, "target_temp_c": 40, "pressure_mbar_a": None, "rpm": None},
                    },
                    {
                        "actor_id": "Stirrer_01",
                        "actor": "Stirrer_01",
                        "priority": 1,
                        "params": {"status_on": None, "target_temp_c": None, "pressure_mbar_a": None, "rpm": 600},
                    },
                ],
                "task": "Parallel ramp",
                "delta_time": 2,
            },
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=60),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual([item["actor"] for item in evaluation["active_step"]["actors"]], ["Stirrer_01", "Huber_01"])
        self.assertAlmostEqual(evaluation["current_targets"]["Huber_01"]["temp"], 30.0)
        self.assertAlmostEqual(evaluation["current_targets"]["Stirrer_01"]["rpm"], 400.0)
        self.assertEqual(evaluation["current_targets"]["Stirrer_01"]["_priority"], 1)
        self.assertEqual(evaluation["current_targets"]["Huber_01"]["_priority"], 2)

    def test_initial_actor_with_empty_params_does_not_create_target(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            {
                "actors": [
                    {
                        "actor_id": "Stirrer_01",
                        "actor": "Stirrer_01",
                        "priority": 1,
                        "params": {"status_on": None, "target_temp_c": None, "pressure_mbar_a": None, "rpm": ""},
                    }
                ],
                "task": "No command",
                "delta_time": 1,
            },
        ]

        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=0,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=30),
        )

        self.assertFalse(evaluation["completed"])
        self.assertNotIn("Stirrer_01", evaluation["current_targets"])

    def test_stopped_program_payload_resets_active_progress_and_targets(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="stopped",
            active_step_index=0,
            step_started_at=started_at,
        )
        state.snapshot_json = {
            "steps": [
                _motor_step("Ramp", 5, 500, status_on=True),
            ],
            "bindings": [
                {"actor": "Stirrer_01", "profile_id": "motor_rpm", "protocol": "ika_eurostar_60"},
            ],
        }

        payload = recipe_program_state_to_dict(state)

        self.assertEqual(payload["status"], "stopped")
        self.assertIsNone(payload["active_step"])
        self.assertIsNone(payload["active_step_number"])
        self.assertEqual(payload["step_progress"], 0.0)
        self.assertEqual(payload["current_targets"], [])


if __name__ == "__main__":
    unittest.main()
