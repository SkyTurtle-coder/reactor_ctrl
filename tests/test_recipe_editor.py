import unittest
from pathlib import Path

import config as app_config

from reactor_app import create_app
from reactor_app import api as recipe_api


class RecipeEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED

        app_config.Config.SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False

        cls.app = create_app()
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    @classmethod
    def tearDownClass(cls):
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required

    def test_recipe_view_renders_flowsheet_selector_and_actor_table(self):
        response = self.client.get("/recipes")
        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)
        self.assertIn('id="recipe-build-select"', html)
        self.assertIn('id="recipe-select"', html)
        self.assertIn('id="recipe-save-btn"', html)
        self.assertIn("Pressure [mBar(A)]", html)
        self.assertIn("Status", html)
        self.assertIn("recipe-no-flowsheet-hint", html)
        self.assertIn("Actor", html)

    def test_recipe_template_uses_required_headers_and_hint(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "recipes.html").read_text(encoding="utf-8")

        self.assertIn('<select id="recipe-build-select"', source)
        self.assertIn('<div class="table-responsive recipe-steps-scroll">', source)
        self.assertIn('<th class="recipe-col-actor">Actor</th>', source)
        self.assertIn("&Delta; [min]", source)
        self.assertIn('<th class="recipe-col-status">Status</th>', source)
        self.assertIn("Soll Temp. [&deg;C]", source)
        self.assertIn("Pressure [mBar(A)]", source)
        self.assertIn('id="recipe-no-flowsheet-hint"', source)

    def test_recipe_editor_script_uses_flowsheet_bound_actor_dropdowns(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "js" / "recipes.js").read_text(encoding="utf-8")

        self.assertIn('document.getElementById("recipe-build-select")', source)
        self.assertIn("function makeActorPicker(step, rowIndex, isEmpty, disabled)", source)
        self.assertIn("function actorOptionsForBuild(buildData)", source)
        self.assertIn("function normalizeActorRefs(rawActors)", source)
        self.assertIn("function makeActorStatusSelect(ref, rowIndex, disabled)", source)
        self.assertIn("recipe-actor-chip", source)
        self.assertIn("recipe-actor-chip-main", source)
        self.assertIn("recipe-param-cell", source)
        self.assertIn('fetchJson(`/api/reactor-builds/${state.reactorBuildId}`)', source)
        self.assertIn("Select a flowsheet before adding steps.", source)
        self.assertIn("At least one actor from the selected flowsheet is required for every step before saving.", source)
        self.assertNotIn("recipe-actor-advanced", source)
        self.assertNotIn("<summary>Advanced</summary>", source)

    def test_recipe_api_requires_build_and_valid_actor(self):
        source = (Path(__file__).resolve().parents[1] / "reactor_app" / "api.py").read_text(encoding="utf-8")

        self.assertIn("def _recipe_allowed_actor_instance_ids", source)
        self.assertIn("def _recipe_allowed_actor_lookup", source)
        self.assertIn("Field 'reactor_build_id' is required.", source)
        self.assertIn("must match actuator instance_ids from the selected flowsheet.", source)
        self.assertIn("The selected flowsheet does not contain any actors.", source)

    def test_recipe_api_accepts_multi_actor_steps_with_actor_scoped_params(self):
        allowed = {
            "Huber_01": {"actor": "Huber_01", "profile_id": "hc_system_temperature", "symbol_id": "hc_system"},
            "Stirrer_01": {"actor": "Stirrer_01", "profile_id": "motor_rpm", "symbol_id": "motor"},
        }

        steps = recipe_api._validate_recipe_steps(
            [
                {
                    "actors": [
                        {
                            "actor_id": "Huber_01",
                            "priority": 1,
                            "params": {"status_on": True, "target_temp_c": 35, "pressure_mbar_a": None, "rpm": None},
                        },
                        {
                            "actor_id": "Stirrer_01",
                            "priority": 2,
                            "params": {"status_on": True, "target_temp_c": None, "pressure_mbar_a": None, "rpm": 300},
                        },
                    ],
                    "task": "Heat and stir",
                    "delta_time": 5,
                },
                {
                    "actors": [
                        {
                            "actor_id": "Huber_01",
                            "priority": 1,
                            "params": {"status_on": None, "target_temp_c": 35, "pressure_mbar_a": None, "rpm": None},
                        }
                    ],
                    "task": "Hold",
                    "delta_time": 5,
                },
            ],
            allowed_actor_lookup=allowed,
        )

        self.assertNotIn("actor", steps[0])
        stirrer_ref = steps[0]["actors"][1]
        self.assertEqual(stirrer_ref["actor"], "Stirrer_01")
        self.assertEqual(stirrer_ref["priority"], 2)
        self.assertEqual(stirrer_ref["params"]["rpm"], 300.0)
        self.assertTrue(stirrer_ref["params"]["status_on"])
        step1_actors = steps[1]["actors"]
        self.assertEqual(len(step1_actors), 1)
        self.assertEqual(step1_actors[0]["actor"], "Huber_01")
        self.assertEqual(step1_actors[0]["params"]["target_temp_c"], 35.0)

    def test_recipe_api_rejects_old_top_level_step_fields(self):
        allowed = {
            "Huber_01": {"actor": "Huber_01", "profile_id": "hc_system_temperature", "symbol_id": "hc_system"},
        }

        with self.assertRaisesRegex(ValueError, "old recipe structure"):
            recipe_api._validate_recipe_steps(
                [
                    {"actor": "Huber_01", "task": "Legacy", "delta_time": 1, "temp": 25},
                ],
                allowed_actor_lookup=allowed,
            )

    def test_recipe_api_requires_initial_parameter_for_each_selected_actor(self):
        allowed = {
            "Huber_01": {"actor": "Huber_01", "profile_id": "hc_system_temperature", "symbol_id": "hc_system"},
            "Stirrer_01": {"actor": "Stirrer_01", "profile_id": "motor_rpm", "symbol_id": "motor"},
        }

        with self.assertRaisesRegex(ValueError, "Stirrer_01.*rpm"):
            recipe_api._validate_recipe_steps(
                [
                    {
                        "actors": [
                            {
                                "actor": "Huber_01",
                                "params": {"status_on": True, "target_temp_c": 35},
                            },
                            {
                                "actor": "Stirrer_01",
                                "params": {"status_on": True},
                            },
                        ],
                        "task": "Missing stirrer rpm",
                        "delta_time": 5,
                    },
                ],
                allowed_actor_lookup=allowed,
            )

    def test_recipe_api_allows_off_without_target_and_rejects_off_setpoint(self):
        allowed = {
            "Stirrer_01": {"actor": "Stirrer_01", "profile_id": "motor_rpm", "symbol_id": "motor"},
        }

        steps = recipe_api._validate_recipe_steps(
            [
                {
                    "actors": [
                        {
                            "actor": "Stirrer_01",
                            "priority": 1,
                            "params": {"status_on": False, "rpm": None},
                        }
                    ],
                    "task": "Stop stirrer",
                    "delta_time": 0,
                },
            ],
            allowed_actor_lookup=allowed,
        )

        self.assertFalse(steps[0]["actors"][0]["params"]["status_on"])
        self.assertIsNone(steps[0]["actors"][0]["params"]["rpm"])

        with self.assertRaisesRegex(ValueError, "must be empty when status_on is false"):
            recipe_api._validate_recipe_steps(
                [
                    {
                        "actors": [
                            {
                                "actor": "Stirrer_01",
                                "priority": 1,
                                "params": {"status_on": False, "rpm": 150},
                            }
                        ],
                        "task": "Invalid stop",
                        "delta_time": 0,
                    },
                ],
                allowed_actor_lookup=allowed,
            )

    def test_recipe_api_priority_validation_allows_duplicates_but_rejects_invalid_values(self):
        allowed = {
            "Huber_01": {"actor": "Huber_01", "profile_id": "hc_system_temperature", "symbol_id": "hc_system"},
            "Stirrer_01": {"actor": "Stirrer_01", "profile_id": "motor_rpm", "symbol_id": "motor"},
        }

        steps = recipe_api._validate_recipe_steps(
            [
                {
                    "actors": [
                            {
                                "actor": "Huber_01",
                                "priority": 1,
                                "params": {"status_on": True, "target_temp_c": 25},
                            },
                            {
                                "actor": "Stirrer_01",
                                "priority": 1,
                                "params": {"status_on": True, "rpm": 150},
                            },
                    ],
                    "task": "Same priority",
                    "delta_time": 0,
                },
            ],
            allowed_actor_lookup=allowed,
        )

        self.assertEqual([actor["priority"] for actor in steps[0]["actors"]], [1, 1])

        with self.assertRaisesRegex(ValueError, "between 1 and 10"):
            recipe_api._validate_recipe_steps(
                [
                    {
                        "actors": [
                            {
                                "actor": "Huber_01",
                                "priority": 11,
                                "params": {"status_on": True, "target_temp_c": 25},
                            }
                        ],
                        "task": "Invalid priority",
                        "delta_time": 0,
                    },
                ],
                allowed_actor_lookup=allowed,
            )

        with self.assertRaisesRegex(ValueError, "integer from 1 to 10"):
            recipe_api._validate_recipe_steps(
                [
                    {
                        "actors": [
                            {
                                "actor": "Huber_01",
                                "priority": 1.5,
                                "params": {"status_on": True, "target_temp_c": 25},
                            }
                        ],
                        "task": "Invalid decimal priority",
                        "delta_time": 0,
                    },
                ],
                allowed_actor_lookup=allowed,
            )

    def test_recipe_styles_cover_actor_dropdown_and_hint(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn(".recipe-steps-scroll", source)
        self.assertIn(".recipe-col-actor", source)
        self.assertIn(".recipe-actor-cell", source)
        self.assertIn(".recipe-actor-select", source)
        self.assertIn(".recipe-actor-chip", source)
        self.assertIn(".recipe-actor-chip-main", source)
        self.assertIn(".recipe-param-cell", source)
        self.assertIn(".recipe-col-status", source)
        self.assertIn(".recipe-status-select", source)
        self.assertIn(".recipe-num-input-inactive", source)
        self.assertIn(".recipe-cell-required", source)
        self.assertIn(".recipe-no-flowsheet-hint", source)
        self.assertNotIn(".recipe-actor-advanced", source)
        self.assertNotIn(".recipe-priority-field", source)


if __name__ == "__main__":
    unittest.main()
