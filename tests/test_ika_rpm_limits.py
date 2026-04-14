import unittest
from pathlib import Path
from unittest.mock import patch

from reactor_app.actuator_profiles import get_actuator_profile
from reactor_app.device_limits import IKA_EUROSTAR_60_MAX_RPM, max_rpm_for_protocol
from reactor_app.models import ReactorBuild, Recipe
from reactor_app.services import recipe_program_runtime


class IkaRpmLimitTests(unittest.TestCase):
    def test_motor_profile_caps_speed_at_ika_limit(self):
        profile = get_actuator_profile("motor_rpm")
        self.assertIsNotNone(profile)

        speed_field = next(field for field in profile["fields"] if field["key"] == "speed")
        self.assertEqual(speed_field["max"], IKA_EUROSTAR_60_MAX_RPM)

    def test_process_manual_template_caps_rpm_at_ika_limit(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "process.html").read_text(encoding="utf-8")
        self.assertIn('id="process-manual-speed-input"', source)
        self.assertIn('max="2000"', source)

    def test_recipe_editor_caps_rpm_inputs_at_ika_limit(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "js" / "recipes.js").read_text(encoding="utf-8")
        self.assertIn('fieldName === "rpm" ? \' max="2000"\' : ""', source)

    def test_protocol_specific_lookup_returns_ika_limit(self):
        self.assertEqual(max_rpm_for_protocol("ika_eurostar_60", default=10000), IKA_EUROSTAR_60_MAX_RPM)
        self.assertEqual(max_rpm_for_protocol("other_protocol", default=10000), 10000)

    def test_recipe_runtime_rejects_rpm_above_ika_limit(self):
        recipe = Recipe(
            recipe_id=1,
            title="IKA limit test",
            operator_name="tester",
        )
        recipe.steps_json = [
            {
                "actor": "Stirrer_01",
                "task": "Start",
                "delta_time": 0,
                "rpm": IKA_EUROSTAR_60_MAX_RPM + 1,
            }
        ]
        build = ReactorBuild(
            reactor_build_id=1,
            build_name="Test Build",
            definition_json={},
        )
        binding = {
            "actor": "Stirrer_01",
            "is_resolved": True,
            "device_id": 1,
            "profile_id": "motor_rpm",
            "protocol": "ika_eurostar_60",
        }

        with patch.object(recipe_program_runtime, "_build_target_lookup", return_value={"Stirrer_01": binding}):
            with self.assertRaisesRegex(ValueError, str(IKA_EUROSTAR_60_MAX_RPM)):
                recipe_program_runtime._program_snapshot_for_recipe(recipe, build)

    def test_recipe_runtime_accepts_rpm_at_ika_limit(self):
        recipe = Recipe(
            recipe_id=1,
            title="IKA limit test",
            operator_name="tester",
        )
        recipe.steps_json = [
            {
                "actor": "Stirrer_01",
                "task": "Start",
                "delta_time": 0,
                "rpm": IKA_EUROSTAR_60_MAX_RPM,
            }
        ]
        build = ReactorBuild(
            reactor_build_id=1,
            build_name="Test Build",
            definition_json={},
        )
        binding = {
            "actor": "Stirrer_01",
            "is_resolved": True,
            "device_id": 1,
            "profile_id": "motor_rpm",
            "protocol": "ika_eurostar_60",
        }

        with patch.object(recipe_program_runtime, "_build_target_lookup", return_value={"Stirrer_01": binding}):
            snapshot = recipe_program_runtime._program_snapshot_for_recipe(recipe, build)

        self.assertEqual(snapshot["steps"][0]["rpm"], IKA_EUROSTAR_60_MAX_RPM)


if __name__ == "__main__":
    unittest.main()
