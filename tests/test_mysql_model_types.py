import unittest

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from reactor_app.models import Device, DeviceManualState


class MysqlModelTypeTests(unittest.TestCase):
    def test_device_primary_key_uses_unsigned_bigint_on_mysql(self):
        ddl = str(CreateTable(Device.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("device_id BIGINT UNSIGNED NOT NULL", ddl)

    def test_device_manual_state_foreign_key_matches_unsigned_device_id(self):
        ddl = str(CreateTable(DeviceManualState.__table__).compile(dialect=mysql.dialect()))

        self.assertIn("device_id BIGINT UNSIGNED NOT NULL", ddl)
        self.assertIn("FOREIGN KEY(device_id) REFERENCES device (device_id)", ddl)


if __name__ == "__main__":
    unittest.main()
