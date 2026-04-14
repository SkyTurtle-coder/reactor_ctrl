import unittest

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from reactor_app.models import Device, DeviceManualState, RecipeProgramEvent, RecipeProgramRun, RecipeProgramState


class MysqlModelTypeTests(unittest.TestCase):
    def test_device_primary_key_uses_unsigned_bigint_on_mysql(self):
        ddl = str(CreateTable(Device.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("device_id BIGINT UNSIGNED NOT NULL", ddl)

    def test_device_manual_state_foreign_key_matches_unsigned_device_id(self):
        ddl = str(CreateTable(DeviceManualState.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("device_id BIGINT UNSIGNED NOT NULL", ddl)
        self.assertIn("FOREIGN KEY(device_id) REFERENCES device (device_id)", ddl)

    def test_recipe_program_state_foreign_keys_match_unsigned_ids(self):
        ddl = str(CreateTable(RecipeProgramState.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("recipe_program_state_id BIGINT UNSIGNED NOT NULL", ddl)
        self.assertIn("recipe_id BIGINT UNSIGNED", ddl)
        self.assertIn("reactor_build_id BIGINT UNSIGNED", ddl)
        self.assertIn("FOREIGN KEY(recipe_id) REFERENCES recipe (recipe_id)", ddl)
        self.assertIn("FOREIGN KEY(reactor_build_id) REFERENCES reactor_build (reactor_build_id)", ddl)

    def test_recipe_program_history_tables_use_unsigned_bigints(self):
        run_ddl = str(CreateTable(RecipeProgramRun.__table__).compile(dialect=mysql.dialect()))
        event_ddl = str(CreateTable(RecipeProgramEvent.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("recipe_program_run_id BIGINT UNSIGNED NOT NULL", run_ddl)
        self.assertIn("recipe_id BIGINT UNSIGNED", run_ddl)
        self.assertIn("reactor_build_id BIGINT UNSIGNED", run_ddl)
        self.assertIn("FOREIGN KEY(recipe_id) REFERENCES recipe (recipe_id)", run_ddl)
        self.assertIn("FOREIGN KEY(reactor_build_id) REFERENCES reactor_build (reactor_build_id)", run_ddl)

        self.assertIn("recipe_program_event_id BIGINT UNSIGNED NOT NULL", event_ddl)
        self.assertIn("recipe_program_run_id BIGINT UNSIGNED NOT NULL", event_ddl)
        self.assertIn("FOREIGN KEY(recipe_program_run_id) REFERENCES recipe_program_run (recipe_program_run_id)", event_ddl)


if __name__ == "__main__":
    unittest.main()
