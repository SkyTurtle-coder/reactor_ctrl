import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from reactor_app.actuator_profiles import get_actuator_profile
from reactor_app.device_limits import IKA_EUROSTAR_60_MAX_RPM, max_rpm_for_protocol
from reactor_app.models import DeviceManualState, ReactorBuild, Recipe
from reactor_app.services import recipe_program_runtime
from reactor_app.services.device_manual_runtime import _apply_desired_ika_state, _parse_ika_numeric_response


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
        self.assertIn('paramField === "rpm" ? \' max="2000"\' : ""', source)

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

        self.assertEqual(snapshot["steps"][0]["actors"][0]["params"]["rpm"], IKA_EUROSTAR_60_MAX_RPM)
        self.assertIsNone(snapshot["steps"][0]["rpm"])

    def test_recipe_runtime_accepts_motor_step_with_zero_temp_and_pressure(self):
        recipe = Recipe(
            recipe_id=1,
            title="IKA zero fields test",
            operator_name="tester",
        )
        recipe.steps_json = [
            {
                "actor": "Stirrer_01",
                "task": "Start",
                "delta_time": 0,
                "rpm": 500,
                "temp": 0,
                "pressure": 0.0,
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

        self.assertEqual(snapshot["steps"][0]["actors"][0]["params"]["rpm"], 500)
        self.assertIsNone(snapshot["steps"][0]["rpm"])
        self.assertIsNone(snapshot["steps"][0]["temp"])
        self.assertIsNone(snapshot["steps"][0]["pressure"])


class IkaDeviceClampingDetectionTests(unittest.TestCase):
    """Tests for device-level speed clamping detection in the manual reconciler."""

    def _make_state(self, desired_speed: int, desired_is_on: bool = True) -> DeviceManualState:
        state = DeviceManualState()
        state.desired_is_on = desired_is_on
        state.desired_speed = desired_speed
        return state

    def _make_device(self) -> MagicMock:
        device = MagicMock()
        device.device_id = 1
        return device

    def test_raises_when_device_clamps_setpoint(self):
        """If the IKA panel limits speed and returns a lower value, raise with a clear message."""
        device = self._make_device()
        state = self._make_state(desired_speed=1500)

        # Device responds: START_4 → None, OUT_SP_4 → None, IN_SP_4 → "500.0 4" (clamped)
        with patch(
            "reactor_app.services.device_manual_runtime._run_logged_manual_command",
            side_effect=[None, None, "500.0 4"],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _apply_desired_ika_state(device, state)

        msg = str(ctx.exception)
        self.assertIn("500", msg)
        self.assertIn("1500", msg)
        self.assertIn("Speed Limit", msg)

    def test_no_error_when_setpoint_accepted_exactly(self):
        """No error when the device confirms the requested setpoint."""
        device = self._make_device()
        state = self._make_state(desired_speed=1500)

        with patch(
            "reactor_app.services.device_manual_runtime._run_logged_manual_command",
            side_effect=[None, None, "1500.0 4"],
        ):
            # Should not raise
            _apply_desired_ika_state(device, state)

    def test_no_error_within_tolerance(self):
        """Small rounding differences (≤ 5 rpm) do not trigger the clamping error."""
        device = self._make_device()
        state = self._make_state(desired_speed=1500)

        with patch(
            "reactor_app.services.device_manual_runtime._run_logged_manual_command",
            side_effect=[None, None, "1496.0 4"],
        ):
            _apply_desired_ika_state(device, state)

    def test_parse_ika_response_strips_channel_suffix(self):
        self.assertAlmostEqual(_parse_ika_numeric_response("500.0 4"), 500.0)
        self.assertAlmostEqual(_parse_ika_numeric_response("1500.0 4"), 1500.0)
        self.assertIsNone(_parse_ika_numeric_response(None))
        self.assertIsNone(_parse_ika_numeric_response(""))


if __name__ == "__main__":
    unittest.main()
