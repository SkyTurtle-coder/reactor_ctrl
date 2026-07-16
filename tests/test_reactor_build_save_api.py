import tempfile
import unittest
from pathlib import Path

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db


class ReactorBuildSaveApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "reactor_build_save.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False

        cls.app = create_app()
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.session.execute(
                text(
                    """
                    CREATE TABLE reactor_build (
                        reactor_build_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        build_name TEXT NOT NULL,
                        build_date DATE NOT NULL,
                        created_by TEXT NOT NULL,
                        updated_by TEXT,
                        definition_json JSON NOT NULL,
                        notes TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.session.execute(text("DROP TABLE IF EXISTS reactor_build"))
            db.session.commit()
            db.engine.dispose()

        cls._tmpdir.cleanup()
        app_config.Config.SQLALCHEMY_DATABASE_URI = cls._original_database_uri
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = cls._original_engine_options
        app_config.Config.AUTO_CREATE_SCHEMA = cls._original_auto_create_schema
        app_config.Config.API_AUTH_REQUIRED = cls._original_api_auth_required
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = cls._original_manual_reconciler_enabled
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = cls._original_program_reconciler_enabled

    def setUp(self):
        with self.app.app_context():
            db.session.execute(text("DELETE FROM reactor_build"))
            db.session.commit()

    def _scale_build_payload(self, *, x=120) -> dict:
        return {
            "build_name": "Scale Build",
            "build_date": "2026-07-16",
            "created_by": "tester",
            "updated_by": "tester",
            "definition_json": {
                "canvas": {"width": 1200, "height": 840},
                "nodes": [
                    {
                        "id": "node-scale",
                        "symbol_id": "scale",
                        "instance_id": "SCALE-01",
                        "x": x,
                        "y": 180,
                        "width": 243,
                        "height": 73,
                        "communication": {
                            "device_server_code": "ICS435-01",
                            "connection_label": "COM2 Ethernet",
                            "protocol": "mettler_toledo_ics435",
                        },
                        "anchors": [
                            {
                                "id": "left_process",
                                "x_ratio": 41 / 243,
                                "y_ratio": 41 / 73,
                                "side": "west",
                            },
                            {
                                "id": "right_process",
                                "x_ratio": 241 / 243,
                                "y_ratio": 1 / 73,
                                "side": "east",
                            },
                        ],
                    }
                ],
                "edges": [],
            },
        }

    def test_create_and_patch_scale_reactor_build(self):
        create_response = self.client.post("/api/reactor-builds", json=self._scale_build_payload())

        self.assertEqual(create_response.status_code, 201, create_response.get_data(as_text=True))
        created = create_response.get_json()
        build_id = created["reactor_build_id"]
        node = created["definition_json"]["nodes"][0]
        self.assertEqual(node["symbol_id"], "scale")
        self.assertEqual(node["label"], "Scale")
        self.assertEqual(node["category"], "sensors")
        self.assertTrue(node["svg_url"].endswith("/static/flowsheet/library/sensors/scale.svg"))
        self.assertEqual(node["communication"]["device_server_code"], "ICS435-01")

        patch_payload = self._scale_build_payload(x=160)
        patch_payload["updated_by"] = "operator-2"
        patch_response = self.client.patch(f"/api/reactor-builds/{build_id}", json=patch_payload)

        self.assertEqual(patch_response.status_code, 200, patch_response.get_data(as_text=True))
        patched = patch_response.get_json()
        self.assertEqual(patched["updated_by"], "operator-2")
        self.assertEqual(patched["definition_json"]["nodes"][0]["x"], 160)

    def test_rejects_non_finite_coordinates_before_save(self):
        payload = self._scale_build_payload(x="Infinity")

        response = self.client.post("/api/reactor-builds", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("finite number", response.get_json()["error"])

    def test_list_reactor_builds_omits_large_definition_payloads(self):
        response = self.client.post("/api/reactor-builds", json=self._scale_build_payload())
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

        list_response = self.client.get("/api/reactor-builds")

        self.assertEqual(list_response.status_code, 200, list_response.get_data(as_text=True))
        item = list_response.get_json()["items"][0]
        self.assertNotIn("definition_json", item)
        self.assertIsNone(item["node_count"])
        self.assertEqual(item["build_name"], "Scale Build")


if __name__ == "__main__":
    unittest.main()
