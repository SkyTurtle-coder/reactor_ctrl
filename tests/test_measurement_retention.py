import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

from flask import Flask

from reactor_app.services.measurement_retention import RetentionResult, cutoff_for_days, run_retention


def _make_app(**config_overrides) -> Flask:
    """Minimal Flask app for retention tests — no real DB needed."""
    app = Flask(__name__)
    app.config.update(
        {
            "TESTING": True,
            "MEASUREMENT_RETENTION_ENABLED": True,
            "MEASUREMENT_RETENTION_DAYS": 30,
            "MEASUREMENT_RETENTION_BATCH_SIZE": 100,
            "MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN": 5,
            "MEASUREMENT_RETENTION_DRY_RUN": False,
        }
    )
    app.config.update(config_overrides)
    return app


def _mock_execute_returning(rowcount: int) -> MagicMock:
    m = MagicMock()
    m.rowcount = rowcount
    return m


class CutoffTests(unittest.TestCase):
    def test_cutoff_is_approximately_n_days_ago(self):
        before = datetime.now(timezone.utc) - timedelta(days=30, seconds=1)
        cutoff = cutoff_for_days(30)
        after = datetime.now(timezone.utc) - timedelta(days=30) + timedelta(seconds=1)
        self.assertGreater(cutoff, before)
        self.assertLess(cutoff, after)

    def test_cutoff_is_timezone_aware(self):
        self.assertIsNotNone(cutoff_for_days(30).tzinfo)

    def test_cutoff_respects_days_parameter(self):
        c7 = cutoff_for_days(7)
        c30 = cutoff_for_days(30)
        self.assertGreater(c7, c30)


class RetentionResultLogLineTests(unittest.TestCase):
    def _make(self, **kwargs) -> RetentionResult:
        return RetentionResult(
            cutoff=datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc),
            **kwargs,
        )

    def test_live_ok(self):
        line = self._make(rows_deleted=5000, batches_run=1).as_log_line()
        self.assertIn("[LIVE]", line)
        self.assertIn("deleted=5000", line)
        self.assertIn("status=ok", line)
        self.assertNotIn("stopped_early", line)

    def test_dry_run(self):
        line = self._make(dry_run=True, rows_deleted=12345).as_log_line()
        self.assertIn("[DRY-RUN]", line)
        self.assertIn("deleted=12345", line)

    def test_stopped_early(self):
        line = self._make(rows_deleted=50000, batches_run=5, stopped_early=True).as_log_line()
        self.assertIn("stopped_early=true", line)

    def test_error(self):
        line = self._make(error="connection refused").as_log_line()
        self.assertIn("error=", line)
        self.assertIn("connection refused", line)


class RunRetentionDisabledTests(unittest.TestCase):
    def test_disabled_returns_immediately_without_touching_db(self):
        app = _make_app(MEASUREMENT_RETENTION_ENABLED=False)
        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                result = run_retention(app)
        mock_db.session.execute.assert_not_called()
        mock_db.session.commit.assert_not_called()
        self.assertEqual(result.rows_deleted, 0)
        self.assertIsNone(result.error)


class RunRetentionDryRunTests(unittest.TestCase):
    def test_dry_run_counts_rows_without_deleting(self):
        app = _make_app(MEASUREMENT_RETENTION_DRY_RUN=True)
        mock_execute_result = MagicMock()
        mock_execute_result.scalar.return_value = 42

        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.return_value = mock_execute_result
                result = run_retention(app)

        self.assertTrue(result.dry_run)
        self.assertEqual(result.rows_deleted, 42)
        self.assertEqual(result.batches_run, 0)
        # Must use SELECT COUNT, not DELETE
        executed_sql = str(mock_db.session.execute.call_args[0][0])
        self.assertIn("COUNT", executed_sql.upper())
        self.assertNotIn("DELETE", executed_sql.upper())
        mock_db.session.commit.assert_not_called()

    def test_dry_run_handles_zero_count(self):
        app = _make_app(MEASUREMENT_RETENTION_DRY_RUN=True)
        mock_execute_result = MagicMock()
        mock_execute_result.scalar.return_value = 0

        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.return_value = mock_execute_result
                result = run_retention(app)

        self.assertEqual(result.rows_deleted, 0)
        self.assertIsNone(result.error)


class RunRetentionLiveTests(unittest.TestCase):
    def test_stops_when_partial_batch_returned(self):
        """A partial final batch signals no more rows before the cutoff."""
        app = _make_app(MEASUREMENT_RETENTION_BATCH_SIZE=100, MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN=5)
        responses = [
            _mock_execute_returning(100),  # batch 1: full
            _mock_execute_returning(100),  # batch 2: full
            _mock_execute_returning(37),   # batch 3: partial → stop
        ]
        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.side_effect = responses
                result = run_retention(app)

        self.assertEqual(result.rows_deleted, 237)
        self.assertEqual(result.batches_run, 3)
        self.assertFalse(result.stopped_early)
        self.assertIsNone(result.error)
        self.assertEqual(mock_db.session.commit.call_count, 3)

    def test_stops_after_max_batches(self):
        """When every batch is full and max_batches is reached, stopped_early is set."""
        app = _make_app(MEASUREMENT_RETENTION_BATCH_SIZE=100, MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN=3)
        responses = [_mock_execute_returning(100)] * 3

        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.side_effect = responses
                result = run_retention(app)

        self.assertEqual(result.rows_deleted, 300)
        self.assertEqual(result.batches_run, 3)
        self.assertTrue(result.stopped_early)
        self.assertIsNone(result.error)

    def test_single_batch_clears_all_old_rows(self):
        """When first batch returns 0 rows, no further batches run."""
        app = _make_app(MEASUREMENT_RETENTION_BATCH_SIZE=100)
        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.return_value = _mock_execute_returning(0)
                result = run_retention(app)

        self.assertEqual(result.rows_deleted, 0)
        self.assertEqual(result.batches_run, 1)
        self.assertFalse(result.stopped_early)

    def test_commits_after_every_batch(self):
        """Each batch is committed independently to limit MySQL lock pressure."""
        app = _make_app(MEASUREMENT_RETENTION_BATCH_SIZE=100, MEASUREMENT_RETENTION_MAX_BATCHES_PER_RUN=5)
        responses = [_mock_execute_returning(100)] * 2 + [_mock_execute_returning(50)]

        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.side_effect = responses
                run_retention(app)

        self.assertEqual(mock_db.session.commit.call_count, 3)

    def test_delete_uses_order_by_and_limit(self):
        """The DELETE statement must use ORDER BY measured_at LIMIT to delete oldest first."""
        app = _make_app()
        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.return_value = _mock_execute_returning(0)
                run_retention(app)

        executed_sql = str(mock_db.session.execute.call_args[0][0]).upper()
        self.assertIn("DELETE", executed_sql)
        self.assertIn("ORDER BY", executed_sql)
        self.assertIn("LIMIT", executed_sql)


class RunRetentionErrorTests(unittest.TestCase):
    def test_db_exception_is_captured_not_raised(self):
        app = _make_app()
        with app.app_context():
            with patch("reactor_app.services.measurement_retention.db") as mock_db:
                mock_db.session.execute.side_effect = Exception("DB connection lost")
                result = run_retention(app)

        self.assertIsNotNone(result.error)
        self.assertIn("DB connection lost", result.error)
        self.assertEqual(result.rows_deleted, 0)

    def test_elapsed_time_is_always_set(self):
        app = _make_app(MEASUREMENT_RETENTION_ENABLED=False)
        with app.app_context():
            result = run_retention(app)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
