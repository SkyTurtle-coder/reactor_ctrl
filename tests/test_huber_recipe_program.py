import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from reactor_app.models import Device, ReactorBuild, Recipe, RecipeProgramState
from reactor_app.services import recipe_program_runtime


class _FakeSession:
    def __init__(self, device):
        self.device = device

    def get(self, model, item_id):
        if model is Device and int(item_id) == int(self.device.device_id):
            return self.device
        return None


class HuberRecipeProgramTests(unittest.TestCase):
    def _recipe(self, steps):
        recipe = Recipe(
            recipe_id=1,
            title="Huber Recipe",
            operator_name="tester",
        )
        recipe.steps_json = steps
        return recipe

    def _build(self):
        return ReactorBuild(
            reactor_build_id=1,
            build_name="Huber Build",
            definition_json={},
        )

    def _binding(self):
        return {
            "actor": "Huber_01",
            "is_resolved": True,
            "device_id": 7,
            "device_display_name": "Huber Unistat",
            "profile_id": "hc_system_temperature",
            "protocol": "huber_unistat_430",
        }

    def test_huber_temperature_actor_is_allowed_in_recipe_snapshot(self):
        recipe = self._recipe(
            [
                {"actor": "Huber_01", "task": "Set cold", "delta_time": 0, "temp": -10, "rpm": 0},
                {"actor": "Huber_01", "task": "Ramp warm", "delta_time": 2, "temp": 25, "pressure": 0},
            ]
        )

        with patch.object(
            recipe_program_runtime,
            "_build_target_lookup",
            return_value={"Huber_01": self._binding()},
        ):
            snapshot = recipe_program_runtime._program_snapshot_for_recipe(recipe, self._build())

        self.assertEqual(snapshot["bindings"][0]["profile_id"], "hc_system_temperature")
        self.assertEqual(snapshot["bindings"][0]["protocol"], "huber_unistat_430")
        self.assertEqual(snapshot["steps"][0]["temp"], -10)
        self.assertIsNone(snapshot["steps"][0]["rpm"])
        self.assertIsNone(snapshot["steps"][1]["pressure"])

    def test_huber_recipe_actor_rejects_rpm_values(self):
        recipe = self._recipe(
            [
                {"actor": "Huber_01", "task": "Invalid", "delta_time": 0, "temp": 20, "rpm": 300},
            ]
        )

        with patch.object(
            recipe_program_runtime,
            "_build_target_lookup",
            return_value={"Huber_01": self._binding()},
        ):
            with self.assertRaisesRegex(ValueError, "temperature values only"):
                recipe_program_runtime._program_snapshot_for_recipe(recipe, self._build())

    def test_huber_current_target_writes_setpoint_and_starts_temperature_control(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )
        state = RecipeProgramState()
        state.snapshot_json = {"bindings": [self._binding()]}
        state.last_applied_targets_json = {}

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                changes = recipe_program_runtime._apply_current_targets(
                    app,
                    state,
                    {"Huber_01": {"temp": 21.25, "pressure": 0, "rpm": 0}},
                )

        command_names = [call.kwargs["command_name"] for call in execute_command.call_args_list]
        self.assertEqual(command_names, ["set_setpoint", "start"])
        self.assertEqual(execute_command.call_args_list[0].kwargs["payload"]["temp_c"], 21.25)
        self.assertEqual(state.last_applied_targets_json["Huber_01"]["temp"], 21.25)
        self.assertTrue(state.last_applied_targets_json["Huber_01"]["is_on"])
        self.assertEqual(changes[0]["current"]["profile_id"], "hc_system_temperature")


if __name__ == "__main__":
    unittest.main()
