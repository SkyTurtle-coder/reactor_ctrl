import unittest
from pathlib import Path

import config as app_config

from reactor_app import create_app


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
        self.assertIn("recipe-no-flowsheet-hint", html)
        self.assertIn("Actor", html)

    def test_recipe_template_uses_required_headers_and_hint(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "recipes.html").read_text(encoding="utf-8")

        self.assertIn('<select id="recipe-build-select"', source)
        self.assertIn('<th class="recipe-col-actor">Actor</th>', source)
        self.assertIn("&Delta; [min]", source)
        self.assertIn("Soll Temp. [&deg;C]", source)
        self.assertIn("Pressure [mBar(A)]", source)
        self.assertIn('id="recipe-no-flowsheet-hint"', source)

    def test_recipe_editor_script_uses_flowsheet_bound_actor_dropdowns(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "js" / "recipes.js").read_text(encoding="utf-8")

        self.assertIn('document.getElementById("recipe-build-select")', source)
        self.assertIn("function makeActorSelect(value, rowIndex, isEmpty, disabled)", source)
        self.assertIn("function actorOptionsForBuild(buildData)", source)
        self.assertIn('fetchJson(`/api/reactor-builds/${state.reactorBuildId}`)', source)
        self.assertIn("Select a flowsheet before adding steps.", source)
        self.assertIn("Actor must be set from the selected flowsheet for every step before saving.", source)

    def test_recipe_api_requires_build_and_valid_actor(self):
        source = (Path(__file__).resolve().parents[1] / "reactor_app" / "api.py").read_text(encoding="utf-8")

        self.assertIn("def _recipe_allowed_actor_instance_ids", source)
        self.assertIn("Field 'reactor_build_id' is required.", source)
        self.assertIn("must match an actuator instance_id from the selected flowsheet.", source)
        self.assertIn("The selected flowsheet does not contain any actors.", source)

    def test_recipe_styles_cover_actor_dropdown_and_hint(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn(".recipe-col-actor", source)
        self.assertIn(".recipe-actor-select", source)
        self.assertIn(".recipe-cell-required", source)
        self.assertIn(".recipe-no-flowsheet-hint", source)


if __name__ == "__main__":
    unittest.main()
