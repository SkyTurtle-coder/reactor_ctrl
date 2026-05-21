import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_database_backup as backup


class DatabaseBackupRunnerTests(unittest.TestCase):
    def test_parse_mysql_database_url_masks_password_from_config_shape(self):
        config = backup._parse_database_url(
            "mysql+pymysql://reactor_user:p%40ssword@127.0.0.1:3306/reactor_ctrl?charset=utf8mb4"
        )

        self.assertEqual(config.username, "reactor_user")
        self.assertEqual(config.password, "p@ssword")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 3306)
        self.assertEqual(config.database, "reactor_ctrl")

    def test_dump_command_uses_defaults_file_before_other_options(self):
        command = backup._dump_command("/usr/bin/mariadb-dump", Path("/tmp/client.cnf"), "reactor_ctrl")

        self.assertEqual(command[0], "/usr/bin/mariadb-dump")
        self.assertTrue(command[1].startswith("--defaults-extra-file="))
        self.assertTrue(command[1].endswith("client.cnf"))
        self.assertIn("--single-transaction", command)
        self.assertIn("--skip-lock-tables", command)
        self.assertEqual(command[-1], "reactor_ctrl")

    def test_defaults_file_contains_client_credentials_without_database_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults_file = Path(tmp) / "client.cnf"
            config = backup.DatabaseConfig(
                username="reactor_user",
                password="secret",
                host="127.0.0.1",
                port=3306,
                database="reactor_ctrl",
            )

            backup._write_defaults_file(defaults_file, config)

            content = defaults_file.read_text(encoding="utf-8")
            self.assertIn("[client]", content)
            self.assertIn('user="reactor_user"', content)
            self.assertIn('password="secret"', content)
            self.assertIn('host="127.0.0.1"', content)
            self.assertNotIn("reactor_ctrl", content)

    def test_prune_old_backups_keeps_latest_symlink_and_recent_dump(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            old_file = backup_dir / "reactor_ctrl.2026-04-01_010000.sql.gz"
            recent_file = backup_dir / "reactor_ctrl.2026-05-20_010000.sql.gz"
            latest_file = backup_dir / "reactor_ctrl.latest.sql.gz"
            old_file.write_text("old", encoding="utf-8")
            recent_file.write_text("recent", encoding="utf-8")
            latest_file.write_text("latest", encoding="utf-8")

            old_time = (now - timedelta(days=45)).timestamp()
            recent_time = (now - timedelta(days=1)).timestamp()
            old_file.touch()
            recent_file.touch()
            latest_file.touch()
            import os

            os.utime(old_file, (old_time, old_time))
            os.utime(recent_file, (recent_time, recent_time))
            os.utime(latest_file, (old_time, old_time))

            deleted = backup._prune_old_backups(backup_dir, "reactor_ctrl", 30, now)

            self.assertEqual(deleted, [old_file])
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(latest_file.exists())


if __name__ == "__main__":
    unittest.main()
