import unittest
from pathlib import Path

import config as app_config

from reactor_app import create_app


class ProcessViewTemplateTests(unittest.TestCase):
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

    def test_process_view_uses_simplified_manual_controls(self):
        response = self.client.get("/process")
        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)

        self.assertIn("process-manual-settings-form", html)
        self.assertIn("process-manual-state-input", html)
        self.assertIn("process-manual-speed-input", html)
        self.assertIn("process-manual-submit-button", html)

        forbidden_strings = (
            "Status lesen",
            "Aktor anwenden",
            "Direktbefehl",
            "Kein Befehl gesendet",
            "LIVE-WERTE",
            "Geraetestatus",
            "Gerätestatus",
            "Protokollhinweis",
        )
        for text in forbidden_strings:
            self.assertNotIn(text, html)

    def test_process_view_script_no_longer_contains_legacy_manual_ui_labels(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "js" / "process_view.js"
        source = script_path.read_text(encoding="utf-8")

        forbidden_strings = (
            "Status lesen",
            "Aktor anwenden",
            "Direktbefehl",
            "Kein Befehl gesendet",
            "LIVE-WERTE",
            "Geraetestatus",
            "Gerätestatus",
            "Protokollhinweis",
        )
        for text in forbidden_strings:
            self.assertNotIn(text, source)


if __name__ == "__main__":
    unittest.main()
