import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from reactor_app.models import Device, ReactorBuild, Recipe, RecipeProgramState
from reactor_app.services import recipe_program_runtime


class _FakeSession:
    def __init__(self, device):
        self.device = device
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0

    def get(self, model, item_id):
        if model is Device and int(item_id) == int(self.device.device_id):
            return self.device
        return None

    def refresh(self, item):
        return None

    def flush(self):
        self.flush_calls += 1

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class HuberRecipeProgramTests(unittest.TestCase):
    def _recipe(self, steps):
        recipe = Recipe(
            recipe_id=1,
            title="Huber Recipe",
            operator_name="tester",
        )
        recipe.steps_json = steps
        return recipe

    def _huber_step(self, task, delta_time, target_temp_c=None, *, status_on=None, actor="Huber_01", priority=1, extra_params=None):
        params = {
            "status_on": status_on,
            "target_temp_c": target_temp_c,
            "pressure_mbar_a": None,
            "rpm": None,
        }
        if extra_params:
            params.update(extra_params)
        return {
            "actors": [
                {
                    "actor_id": actor,
                    "actor": actor,
                    "priority": priority,
                    "params": params,
                }
            ],
            "task": task,
            "delta_time": delta_time,
        }

    def _motor_ref(self, rpm, *, status_on=None, actor="Stirrer_01", priority=2):
        return {
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

    def _motor_binding(self):
        return {
            "actor": "Stirrer_01",
            "is_resolved": True,
            "device_id": 8,
            "device_display_name": "IKA Stirrer",
            "profile_id": "motor_rpm",
            "protocol": "ika_eurostar_60",
        }

    def test_huber_temperature_actor_is_allowed_in_recipe_snapshot(self):
        recipe = self._recipe(
            [
                self._huber_step("Set cold", 0, -10, status_on=True),
                self._huber_step("Ramp warm", 2, 25),
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
        self.assertEqual(snapshot["steps"][0]["actors"][0]["params"]["target_temp_c"], -10)
        self.assertEqual(snapshot["steps"][0]["actors"][0]["params"]["control_sensor"], "internal")
        self.assertNotIn("temp", snapshot["steps"][0])
        self.assertNotIn("rpm", snapshot["steps"][0])
        self.assertNotIn("pressure", snapshot["steps"][1])

    def test_huber_recipe_actor_rejects_rpm_values(self):
        recipe = self._recipe(
            [
                self._huber_step("Invalid", 0, 20, extra_params={"rpm": 300}),
            ]
        )

        with patch.object(
            recipe_program_runtime,
            "_build_target_lookup",
            return_value={"Huber_01": self._binding()},
        ):
            with self.assertRaisesRegex(ValueError, "does not support this field"):
                recipe_program_runtime._program_snapshot_for_recipe(recipe, self._build())

    def test_unistat_recipe_actor_keeps_existing_temperature_range(self):
        recipe = self._recipe(
            [
                self._huber_step("Too high", 0, 180),
            ]
        )

        with patch.object(
            recipe_program_runtime,
            "_build_target_lookup",
            return_value={"Huber_01": self._binding()},
        ):
            with self.assertRaisesRegex(ValueError, "limited to -40..150"):
                recipe_program_runtime._program_snapshot_for_recipe(recipe, self._build())

    def test_multi_actor_step_keeps_relevant_values_for_each_actor(self):
        recipe = self._recipe(
            [
                {
                    "actors": [
                        self._huber_step("unused", 0, 35, status_on=True)["actors"][0],
                        self._motor_ref(300, status_on=True),
                    ],
                    "task": "Heat while stirring",
                    "delta_time": 5,
                },
            ]
        )

        with patch.object(
            recipe_program_runtime,
            "_build_target_lookup",
            return_value={"Huber_01": self._binding(), "Stirrer_01": self._motor_binding()},
        ):
            snapshot = recipe_program_runtime._program_snapshot_for_recipe(recipe, self._build())

        actors = snapshot["steps"][0]["actors"]
        self.assertEqual([(actor["actor"], actor["priority"]) for actor in actors], [("Huber_01", 1), ("Stirrer_01", 2)])
        self.assertEqual(actors[0]["params"]["target_temp_c"], 35)
        self.assertEqual(actors[0]["params"]["control_sensor"], "internal")
        self.assertIsNone(actors[0]["params"]["rpm"])
        self.assertEqual(actors[1]["params"]["rpm"], 300)
        self.assertIsNone(actors[1]["params"]["target_temp_c"])
        self.assertNotIn("temp", snapshot["steps"][0])
        self.assertNotIn("rpm", snapshot["steps"][0])
        self.assertNotIn("pressure", snapshot["steps"][0])
        self.assertEqual({binding["actor"] for binding in snapshot["bindings"]}, {"Huber_01", "Stirrer_01"})

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
        self.assertEqual(command_names, ["enable_remote", "select_internal_sensor", "set_setpoint", "get_setpoint", "start"])
        self.assertEqual(execute_command.call_args_list[2].kwargs["payload"]["temp_c"], 21.25)
        self.assertEqual(execute_command.call_args_list[2].kwargs["payload"]["response_timeout_ms"], 1200)
        self.assertEqual(execute_command.call_args_list[2].kwargs["payload"]["max_retries"], 1)
        self.assertEqual(state.last_applied_targets_json["Huber_01"]["temp"], 21.25)
        self.assertTrue(state.last_applied_targets_json["Huber_01"]["is_on"])
        self.assertEqual(state.last_applied_targets_json["Huber_01"]["control_sensor"], "internal")
        self.assertEqual(changes[0]["current"]["profile_id"], "hc_system_temperature")

    def test_huber_current_target_selects_external_sensor_before_setpoint_and_start(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_cc230",
        )
        state = RecipeProgramState()
        binding = {**self._binding(), "protocol": "huber_cc230"}
        state.snapshot_json = {"bindings": [binding]}
        state.last_applied_targets_json = {}

        with patch.object(recipe_program_runtime, "_SENSOR_SELECT_SETTLE_SECONDS", 0):
            with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
                with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                    changes = recipe_program_runtime._apply_current_targets(
                        app,
                        state,
                        {"Huber_01": {"temp": 18.5, "pressure": 0, "rpm": 0, "control_sensor": "external"}},
                    )

        command_names = [call.kwargs["command_name"] for call in execute_command.call_args_list]
        self.assertEqual(command_names, ["enable_remote", "select_external_sensor", "set_setpoint", "get_setpoint", "start"])
        self.assertFalse(execute_command.call_args_list[0].kwargs["acquire_lock"])
        self.assertFalse(execute_command.call_args_list[1].kwargs["acquire_lock"])
        self.assertEqual(execute_command.call_args_list[1].kwargs["payload"]["skip_remote"], True)
        self.assertEqual(changes[0]["current"]["control_sensor"], "external")

    def test_huber_current_target_off_sends_stop_without_setpoint(self):
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
        state.last_applied_targets_json = {"Huber_01": {"temp": 21.25, "is_on": True}}

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                changes = recipe_program_runtime._apply_current_targets(
                    app,
                    state,
                    {"Huber_01": {"is_on": False, "temp": 0, "pressure": 0, "rpm": 0}},
                )

        command_names = [call.kwargs["command_name"] for call in execute_command.call_args_list]
        self.assertEqual(command_names, ["stop"])
        self.assertFalse(state.last_applied_targets_json["Huber_01"]["is_on"])
        self.assertEqual(changes[0]["current"]["profile_id"], "hc_system_temperature")

    def test_huber_current_target_failure_reports_recipe_context(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat 430",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )
        state = RecipeProgramState()
        state.snapshot_json = {"bindings": [self._binding()]}
        state.last_applied_targets_json = {}
        command = SimpleNamespace(
            command_id=390180,
            command_name="set_setpoint",
            status="failed",
            error_message="No response from Huber Unistat 430.",
        )
        fake_session = _FakeSession(device)

        def fail_command(*args, **kwargs):
            if kwargs.get("command_name") == "set_setpoint":
                raise recipe_program_runtime.DeviceCommandError(
                    "Device command execution failed.",
                    status_code=502,
                    command=command,
                )
            return SimpleNamespace(result=SimpleNamespace(metadata={"value": 21.25}))

        evaluation = {"active_step_index": 1, "active_step": {"task": "Ramp"}}
        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "execute_device_command", side_effect=fail_command):
                with self.assertRaises(recipe_program_runtime.RecipeProgramDeviceCommandError) as ctx:
                    recipe_program_runtime._apply_current_targets(
                        app,
                        state,
                        {"Huber_01": {"temp": 21.25, "pressure": 0, "rpm": 0}},
                        evaluation=evaluation,
                    )

        message = str(ctx.exception)
        self.assertIn("step 2 (Ramp)", message)
        self.assertIn("Huber_01", message)
        self.assertIn("set_setpoint", message)
        self.assertIn("Huber Unistat", message)
        self.assertIn("390180", message)
        self.assertIn("No response", message)
        self.assertEqual(fake_session.commit_calls, 0)

    def test_huber_current_target_respects_stop_before_start_command(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )
        state = RecipeProgramState(status="running", lease_owner="worker-1", stop_requested=False)
        state.snapshot_json = {"bindings": [self._binding()]}
        state.last_applied_targets_json = {}

        class StopAfterSetpointSession(_FakeSession):
            def __init__(self, session_device):
                super().__init__(session_device)
                self.refresh_calls = 0

            def refresh(self, item):
                self.refresh_calls += 1
                if self.refresh_calls >= 7:
                    item.stop_requested = True

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=StopAfterSetpointSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                changes = recipe_program_runtime._apply_current_targets(
                    app,
                    state,
                    {"Huber_01": {"temp": 21.25, "pressure": 0, "rpm": 0}},
                    worker_id="worker-1",
                )

        self.assertIsNone(changes)
        command_names = [call.kwargs["command_name"] for call in execute_command.call_args_list]
        self.assertEqual(command_names, ["enable_remote", "select_internal_sensor", "set_setpoint", "get_setpoint"])

    def test_target_application_aborts_when_stop_was_requested(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )
        state = RecipeProgramState(status="running", lease_owner="worker-1", stop_requested=True)
        state.snapshot_json = {"bindings": [self._binding()]}
        state.last_applied_targets_json = {}

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                changes = recipe_program_runtime._apply_current_targets(
                    app,
                    state,
                    {"Huber_01": {"temp": 21.25, "pressure": 0, "rpm": 0}},
                    worker_id="worker-1",
                )

        self.assertIsNone(changes)
        execute_command.assert_not_called()

    def test_huber_safe_stop_sets_twenty_degrees_before_stop(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                safe_target, errors = recipe_program_runtime._apply_safe_stop_to_binding(
                    app,
                    self._binding(),
                    requested_by="integration_stop",
                )

        self.assertEqual(errors, [])
        command_names = [call.kwargs["command_name"] for call in execute_command.call_args_list]
        self.assertEqual(command_names, ["set_setpoint", "stop"])
        self.assertEqual(execute_command.call_args_list[0].kwargs["payload"]["temp_c"], 20.0)
        self.assertEqual(safe_target["temp"], 20.0)
        self.assertFalse(safe_target["is_on"])

    def test_huber_safe_stop_collects_error_when_stop_command_fails(self):
        app = Flask(__name__)
        device = Device(
            device_id=7,
            asset_serial="HUBER-7",
            display_name="Huber Unistat",
            device_type="thermostat",
            protocol="huber_unistat_430",
        )

        def raise_on_stop(*args, **kwargs):
            if kwargs.get("command_name") == "stop":
                raise RuntimeError("Connection lost")

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "execute_device_command", side_effect=raise_on_stop):
                safe_target, errors = recipe_program_runtime._apply_safe_stop_to_binding(
                    app,
                    self._binding(),
                    requested_by="integration_stop",
                )

        self.assertEqual(len(errors), 1)
        self.assertIn("stop", errors[0])
        self.assertIn("Connection lost", errors[0])
        self.assertEqual(safe_target["temp"], 20.0)
        self.assertFalse(safe_target["is_on"])

    def test_ika_safe_stop_sets_zero_rpm_and_sends_stop(self):
        app = Flask(__name__)
        device = Device(
            device_id=8,
            asset_serial="IKA-8",
            display_name="IKA Stirrer",
            device_type="actuator",
            protocol="ika_eurostar_60",
        )
        binding = {
            "actor": "Stirrer_01",
            "is_resolved": True,
            "device_id": 8,
            "device_display_name": "IKA Stirrer",
            "profile_id": "motor_rpm",
            "protocol": "ika_eurostar_60",
        }

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_FakeSession(device))):
            with patch.object(recipe_program_runtime, "queue_manual_state_update") as queue_update:
                with patch.object(recipe_program_runtime, "execute_device_command") as execute_command:
                    safe_target, errors = recipe_program_runtime._apply_safe_stop_to_binding(
                        app,
                        binding,
                        requested_by="integration_stop",
                    )

        self.assertEqual(errors, [])
        queue_update.assert_called_once()
        self.assertFalse(queue_update.call_args.kwargs["desired_is_on"])
        self.assertEqual(queue_update.call_args.kwargs["desired_speed"], 0)
        self.assertEqual([call.kwargs["payload"]["text"] for call in execute_command.call_args_list], ["OUT_SP_4 0", "STOP_4"])
        self.assertEqual(safe_target["rpm"], 0)
        self.assertFalse(safe_target["is_on"])


if __name__ == "__main__":
    unittest.main()
