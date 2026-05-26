import unittest
from pathlib import Path

import config as app_config

from reactor_app import create_app


class ThemeToggleTests(unittest.TestCase):
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
        cls.app.extensions["sqlalchemy"]._app_engines.pop(cls.app, None)
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required

    def test_theme_toggle_renders_on_app_pages(self):
        for route in ("/process", "/recipes", "/logs", "/reactor-builder", "/data"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('data-theme-toggle', html)
                self.assertIn("reactor_ctrl.theme.v1", html)
                self.assertIn("js/theme.js", html)

    def test_removed_landing_pages_redirect_to_process_view(self):
        for route in ("/", "/reactor-control", "/reactor-control-system", "/infrared-camera"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/process", response.headers["Location"])

    def test_theme_assets_define_dark_mode_and_system_fallback(self):
        repo_root = Path(__file__).resolve().parents[1]
        stylesheet = (repo_root / "static" / "css" / "app.css").read_text(encoding="utf-8")
        script = (repo_root / "static" / "js" / "theme.js").read_text(encoding="utf-8")
        process_script = (repo_root / "static" / "js" / "process_view.js").read_text(encoding="utf-8")

        self.assertIn('html[data-theme="dark"]', stylesheet)
        self.assertIn(".theme-switch", stylesheet)
        self.assertIn("--plot-surface", stylesheet)
        self.assertIn("--plot-label", stylesheet)
        self.assertIn(".process-program-meta-item", stylesheet)
        self.assertIn(".process-program-progress-bar", stylesheet)
        self.assertIn("(prefers-color-scheme: dark)", script)
        self.assertIn("localStorage", script)
        self.assertIn('root.dataset.theme = theme;', script)
        self.assertIn('window.dispatchEvent(new CustomEvent("reactor:themechange"', script)
        self.assertIn('window.addEventListener("reactor:themechange"', process_script)
        self.assertIn('cssThemeValue("--plot-surface"', process_script)
        self.assertNotIn('fill="rgba(255,255,255,0.82)"', process_script)


if __name__ == "__main__":
    unittest.main()
