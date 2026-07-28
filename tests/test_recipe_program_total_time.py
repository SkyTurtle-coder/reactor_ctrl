"""Coverage for the recipe-wide (not just current-step) time/progress fields
added to the Process recipe view, and for the new pause/resume runtime.

Maps to the numbered test cases from the task:
    1-4 -> RecipeProgramTimelineTotalsTests (pure _evaluate_program_timeline)
    5-9 -> RecipeProgramStateToDictTotalsTests (recipe_program_state_to_dict)
    5-6 -> RecipeProgramPauseResumeTests (pause/resume runtime behaviour)
"""
import tempfile
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config as app_config
from sqlalchemy import text

from reactor_app import create_app
from reactor_app.extensions import db
from reactor_app.models import Recipe, ReactorBuild, RecipeProgramRun, RecipeProgramState
from reactor_app.services import recipe_program_runtime
from reactor_app.services.recipe_program_runtime import (
    _datetime_isoformat,
    _evaluate_program_timeline,
    pause_recipe_program,
    recipe_program_state_to_dict,
    resume_recipe_program,
    start_recipe_program,
)


def _motor_step(task, delta_time, rpm=None, *, status_on=None, actor="Stirrer_01"):
    return {
        "actors": [
            {
                "actor_id": actor,
                "actor": actor,
                "priority": 1,
                "params": {"status_on": status_on, "target_temp_c": None, "pressure_mbar_a": None, "rpm": rpm},
            }
        ],
        "task": task,
        "delta_time": delta_time,
    }


# ---------------------------------------------------------------------------
# 1-4: pure timeline math, no DB
# ---------------------------------------------------------------------------

class RecipeProgramTimelineTotalsTests(unittest.TestCase):
    def test_running_recipe_reports_correct_recipe_wide_totals(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _motor_step("Start", 1, 300, status_on=True),  # 60s
            _motor_step("Hold", 2, 300),  # 120s
            _motor_step("Ramp", 3, 600),  # 180s
        ]
        # 30 s into the second step (index 1).
        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=1,
            step_started_at=started_at,
            now=started_at + timedelta(seconds=30),
        )

        self.assertFalse(evaluation["completed"])
        self.assertEqual(evaluation["active_step_index"], 1)
        self.assertAlmostEqual(evaluation["total_planned_seconds"], 360.0)
        self.assertAlmostEqual(evaluation["elapsed_planned_seconds"], 90.0)  # 60 (step0) + 30 (partial step1)
        self.assertAlmostEqual(evaluation["step_remaining_seconds"], 90.0)

    def test_partially_elapsed_current_step_contributes_only_its_elapsed_portion(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [_motor_step("Hold", 10, 300, status_on=True)]  # 600s step, nothing before it

        evaluation = _evaluate_program_timeline(
            steps, active_step_index=0, step_started_at=started_at, now=started_at + timedelta(seconds=123)
        )

        self.assertAlmostEqual(evaluation["elapsed_planned_seconds"], 123.0)
        self.assertAlmostEqual(evaluation["total_planned_seconds"], 600.0)

    def test_upcoming_steps_are_included_in_remaining_but_not_in_elapsed(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        now = started_at + timedelta(seconds=60)  # exactly finished step 0
        short_steps = [_motor_step("Start", 1, 300, status_on=True), _motor_step("Hold", 2, 300)]
        long_steps = [_motor_step("Start", 1, 300, status_on=True), _motor_step("Hold", 5, 300)]

        short_eval = _evaluate_program_timeline(short_steps, active_step_index=0, step_started_at=started_at, now=now)
        long_eval = _evaluate_program_timeline(long_steps, active_step_index=0, step_started_at=started_at, now=now)

        self.assertAlmostEqual(short_eval["elapsed_planned_seconds"], 60.0)
        self.assertAlmostEqual(long_eval["elapsed_planned_seconds"], 60.0)
        self.assertAlmostEqual(short_eval["total_planned_seconds"], 180.0)
        self.assertAlmostEqual(long_eval["total_planned_seconds"], 360.0)

    def test_completed_earlier_steps_are_counted_exactly_once(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _motor_step("Start", 1, 300, status_on=True),
            _motor_step("Hold", 2, 300),
            _motor_step("Ramp", 3, 600),
        ]
        # active_step_index=2 with step_started_at reflecting when step 2
        # began (steps 0 and 1 are already fully completed in the persisted
        # runtime state) — their duration must be counted once, not zero and
        # not twice.
        step2_started_at = started_at + timedelta(seconds=180)
        evaluation = _evaluate_program_timeline(
            steps,
            active_step_index=2,
            step_started_at=step2_started_at,
            now=step2_started_at + timedelta(seconds=45),
        )

        self.assertAlmostEqual(evaluation["elapsed_planned_seconds"], 225.0)
        self.assertAlmostEqual(evaluation["total_planned_seconds"], 360.0)

    def test_completed_program_has_full_elapsed_and_zero_remaining(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [_motor_step("Start", 1, 300, status_on=True)]
        evaluation = _evaluate_program_timeline(
            steps, active_step_index=0, step_started_at=started_at, now=started_at + timedelta(seconds=60)
        )
        self.assertTrue(evaluation["completed"])
        self.assertAlmostEqual(evaluation["elapsed_planned_seconds"], evaluation["total_planned_seconds"])
        self.assertAlmostEqual(evaluation["step_remaining_seconds"], 0.0)


# ---------------------------------------------------------------------------
# 5-9: recipe_program_state_to_dict (the API-facing payload)
# ---------------------------------------------------------------------------

class RecipeProgramStateToDictTotalsTests(unittest.TestCase):
    def _running_state(self, *, started_at, active_step_index, step_started_at, steps):
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="running",
            started_at=started_at,
            active_step_index=active_step_index,
            step_started_at=step_started_at,
        )
        state.snapshot_json = {"steps": steps, "bindings": []}
        return state

    def test_running_program_progress_percent_and_remaining(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        steps = [
            _motor_step("Start", 1, 300, status_on=True),
            _motor_step("Hold", 2, 300),
            _motor_step("Ramp", 3, 600),
        ]
        state = self._running_state(
            started_at=started_at,
            active_step_index=1,
            step_started_at=started_at + timedelta(seconds=60),
            steps=steps,
        )

        with patch.object(recipe_program_runtime, "_now_utc", return_value=started_at + timedelta(seconds=90)):
            payload = recipe_program_state_to_dict(state)

        self.assertEqual(payload["status"], "running")
        self.assertAlmostEqual(payload["recipe_total_seconds"], 360.0)
        self.assertAlmostEqual(payload["recipe_elapsed_seconds"], 90.0)
        self.assertAlmostEqual(payload["recipe_remaining_seconds"], 270.0)
        self.assertAlmostEqual(payload["recipe_progress_percent"], 25.0)
        self.assertFalse(payload["recipe_duration_is_estimated"])
        self.assertIsNotNone(payload["recipe_estimated_end_at"])
        self.assertEqual(payload["recipe_started_at"], _datetime_isoformat(started_at))

    def test_completed_program_reports_full_elapsed_and_100_percent(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="completed",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=60),
            active_step_index=1,
            step_started_at=started_at,
        )
        state.snapshot_json = {"steps": [_motor_step("Start", 1, 300, status_on=True)], "bindings": []}

        payload = recipe_program_state_to_dict(state)

        self.assertEqual(payload["recipe_progress_percent"], 100.0)
        self.assertAlmostEqual(payload["recipe_elapsed_seconds"], 60.0)
        self.assertAlmostEqual(payload["recipe_remaining_seconds"], 0.0)
        self.assertIsNone(payload["recipe_estimated_end_at"])

    def test_stopped_program_freezes_elapsed_at_abort_time_not_now(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        finished_at = started_at + timedelta(seconds=90)
        steps = [_motor_step("Start", 1, 300, status_on=True), _motor_step("Hold", 5, 300)]
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="stopped",
            started_at=started_at,
            finished_at=finished_at,
            active_step_index=1,
            step_started_at=started_at + timedelta(seconds=60),
        )
        state.snapshot_json = {"steps": steps, "bindings": []}

        # "now" is far in the future: elapsed must be frozen at the moment
        # the program was aborted (finished_at), not keep drifting forward.
        far_future = finished_at + timedelta(hours=5)
        with patch.object(recipe_program_runtime, "_now_utc", return_value=far_future):
            payload = recipe_program_state_to_dict(state)

        self.assertAlmostEqual(payload["recipe_elapsed_seconds"], 90.0)
        self.assertNotAlmostEqual(payload["recipe_elapsed_seconds"], (far_future - started_at).total_seconds())
        self.assertIsNone(payload["recipe_estimated_end_at"])

    def test_paused_program_freezes_time_regardless_of_wall_clock(self):
        started_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
        paused_at = started_at + timedelta(seconds=90)
        steps = [_motor_step("Start", 1, 300, status_on=True), _motor_step("Hold", 5, 300)]
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="paused",
            started_at=started_at,
            active_step_index=1,
            step_started_at=started_at + timedelta(seconds=60),
        )
        state.paused_at = paused_at
        state.snapshot_json = {"steps": steps, "bindings": []}

        payload_immediately = recipe_program_state_to_dict(state)
        with patch.object(recipe_program_runtime, "_now_utc", return_value=paused_at + timedelta(hours=2)):
            payload_much_later = recipe_program_state_to_dict(state)

        self.assertEqual(payload_immediately["status"], "paused")
        self.assertAlmostEqual(payload_immediately["recipe_elapsed_seconds"], 90.0)
        self.assertEqual(payload_immediately["recipe_elapsed_seconds"], payload_much_later["recipe_elapsed_seconds"])
        self.assertEqual(payload_immediately["step_remaining_seconds"], payload_much_later["step_remaining_seconds"])
        self.assertIsNone(payload_immediately["recipe_estimated_end_at"])
        self.assertEqual(payload_immediately["paused_at"], _datetime_isoformat(paused_at))

    def test_idle_never_started_program_has_no_meaningful_totals(self):
        payload = recipe_program_state_to_dict(None)
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["recipe_total_seconds"], 0.0)
        self.assertIsNone(payload["recipe_progress_percent"])
        self.assertIsNone(payload["recipe_estimated_end_at"])


# ---------------------------------------------------------------------------
# Pause/resume runtime behaviour (real SQLite-backed; only the 3 tables the
# pause/resume code path actually touches are needed).
# ---------------------------------------------------------------------------

class RecipeProgramPauseResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_uri = app_config.Config.SQLALCHEMY_DATABASE_URI
        cls._original_engine_options = app_config.Config.SQLALCHEMY_ENGINE_OPTIONS
        cls._original_auto_create_schema = app_config.Config.AUTO_CREATE_SCHEMA
        cls._original_api_auth_required = app_config.Config.API_AUTH_REQUIRED
        cls._original_manual_reconciler_enabled = app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED
        cls._original_program_reconciler_enabled = app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED

        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls._tmpdir.name) / "recipe_program_pause_resume.sqlite"
        app_config.Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        app_config.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
        app_config.Config.AUTO_CREATE_SCHEMA = False
        app_config.Config.API_AUTH_REQUIRED = False
        app_config.Config.DEVICE_MANUAL_RECONCILER_ENABLED = False
        app_config.Config.RECIPE_PROGRAM_RECONCILER_ENABLED = False

        cls.app = create_app()
        with cls.app.app_context():
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_state (
                        recipe_program_state_id INTEGER PRIMARY KEY,
                        recipe_id INTEGER,
                        reactor_build_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'idle',
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        recipe_title TEXT,
                        operator_name TEXT,
                        snapshot_json TEXT,
                        last_applied_targets_json TEXT,
                        active_step_index INTEGER NOT NULL DEFAULT 0,
                        step_started_at TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        last_progress_at TEXT,
                        stop_requested INTEGER NOT NULL DEFAULT 0,
                        paused_at TEXT,
                        last_error TEXT,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_run (
                        recipe_program_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipe_id INTEGER,
                        reactor_build_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'running',
                        requested_by TEXT NOT NULL DEFAULT 'system',
                        recipe_title TEXT,
                        operator_name TEXT,
                        snapshot_json TEXT,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        last_progress_at TEXT,
                        last_error TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE recipe_program_event (
                        recipe_program_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipe_program_run_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        active_step_index INTEGER,
                        event_payload TEXT,
                        created_at TEXT
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TABLE reactor_build (
                        reactor_build_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        build_name TEXT NOT NULL,
                        build_date TEXT,
                        created_by TEXT,
                        updated_by TEXT,
                        definition_json TEXT NOT NULL,
                        notes TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            for table_name in ("recipe_program_event", "recipe_program_run", "recipe_program_state", "reactor_build"):
                db.session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
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
            db.session.execute(text("DELETE FROM recipe_program_event"))
            db.session.execute(text("DELETE FROM recipe_program_run"))
            db.session.execute(text("DELETE FROM recipe_program_state"))
            db.session.commit()

    def _seed_running_program(self, *, started_at, active_step_index, step_started_at, steps):
        state = RecipeProgramState(
            recipe_program_state_id=1,
            recipe_id=7,
            status="running",
            requested_by="process_recipe",
            recipe_title="Test Recipe",
            active_step_index=active_step_index,
            step_started_at=step_started_at,
            started_at=started_at,
            last_progress_at=started_at,
        )
        state.snapshot_json = {"steps": steps, "bindings": []}
        db.session.add(state)
        db.session.flush()
        run = RecipeProgramRun(
            recipe_id=7,
            status="running",
            requested_by="process_recipe",
            recipe_title="Test Recipe",
            started_at=started_at,
            last_progress_at=started_at,
        )
        run.snapshot_json = {"steps": steps, "bindings": []}
        db.session.add(run)
        db.session.commit()
        return state

    def test_pause_sets_status_and_paused_at_and_releases_lease(self):
        with self.app.app_context():
            started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
            self._seed_running_program(
                started_at=started_at,
                active_step_index=0,
                step_started_at=started_at,
                steps=[_motor_step("Hold", 10, 300, status_on=True)],
            )
            state = db.session.get(RecipeProgramState, 1)
            state.lease_owner = "worker-1"
            state.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
            db.session.commit()

            result = pause_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            self.assertEqual(result.status, "paused")
            self.assertIsNotNone(result.paused_at)
            self.assertIsNone(result.lease_owner)
            self.assertIsNone(result.lease_expires_at)
            self.assertEqual(result.requested_by, "operator")

            events = db.session.execute(text("SELECT event_type FROM recipe_program_event")).fetchall()
            self.assertIn(("paused",), events)

    def test_pause_requires_running_status(self):
        with self.app.app_context():
            state = RecipeProgramState(recipe_program_state_id=1, status="idle")
            db.session.add(state)
            db.session.commit()

            with self.assertRaises(ValueError):
                pause_recipe_program(self.app, requested_by="operator")

    def test_resume_requires_paused_status(self):
        with self.app.app_context():
            state = RecipeProgramState(recipe_program_state_id=1, status="running")
            db.session.add(state)
            db.session.commit()

            with self.assertRaises(ValueError):
                resume_recipe_program(self.app, requested_by="operator")

    def test_resume_shifts_step_clock_forward_by_pause_duration(self):
        with self.app.app_context():
            started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            step_started_at = datetime.now(timezone.utc) - timedelta(seconds=90)
            self._seed_running_program(
                started_at=started_at,
                active_step_index=0,
                step_started_at=step_started_at,
                steps=[_motor_step("Hold", 10, 300, status_on=True)],
            )

            pause_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            paused_state = db.session.get(RecipeProgramState, 1)
            self.assertIsNotNone(paused_state.paused_at)

            time_module.sleep(0.2)  # simulate a real pause interval

            resumed = resume_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            self.assertEqual(resumed.status, "running")
            self.assertIsNone(resumed.paused_at)
            # SQLite round-trips datetimes as naive; normalize before comparing.
            resumed_step_started_at = resumed.step_started_at.replace(tzinfo=timezone.utc)
            self.assertGreater(resumed_step_started_at, step_started_at)

            events = db.session.execute(text("SELECT event_type FROM recipe_program_event")).fetchall()
            self.assertIn(("resumed",), events)

    def test_resumed_program_evaluates_as_if_pause_never_happened(self):
        """Elapsed step time right after resume must reflect only active
        (non-paused) time, not the wall-clock time spent paused."""
        with self.app.app_context():
            started_at = datetime.now(timezone.utc) - timedelta(seconds=40)
            self._seed_running_program(
                started_at=started_at,
                active_step_index=0,
                step_started_at=started_at,
                steps=[_motor_step("Hold", 10, 300, status_on=True)],  # 600s step
            )

            pause_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            time_module.sleep(0.2)  # simulate a real pause interval

            resumed = resume_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            payload = recipe_program_state_to_dict(resumed)
            # ~40s of real elapsed step time accrued before the pause; the
            # ~0.2s pause interval must not have been added on top of it.
            self.assertLess(payload["step_elapsed_seconds"], 41.0)
            self.assertGreaterEqual(payload["step_elapsed_seconds"], 39.5)

    def test_start_is_rejected_while_another_program_is_paused(self):
        """A paused program must block Start the same way a running one
        does — otherwise Start would silently overwrite a paused run."""
        with self.app.app_context():
            started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            self._seed_running_program(
                started_at=started_at,
                active_step_index=0,
                step_started_at=started_at,
                steps=[_motor_step("Hold", 10, 300, status_on=True)],
            )
            pause_recipe_program(self.app, requested_by="operator")
            db.session.commit()

            build = ReactorBuild(build_name="Other Build", definition_json={})
            db.session.add(build)
            db.session.flush()
            other_recipe = Recipe(
                recipe_id=99,
                title="Other Recipe",
                operator_name="tester",
                reactor_build_id=build.reactor_build_id,
            )
            other_recipe.steps_json = [_motor_step("Hold", 1, 300, status_on=True)]

            binding = {
                "actor": "Stirrer_01",
                "is_resolved": True,
                "device_id": 1,
                "device_display_name": "IKA Stirrer",
                "profile_id": "motor_rpm",
                "protocol": "ika_eurostar_60",
            }
            with patch.object(recipe_program_runtime, "_build_target_lookup", return_value={"Stirrer_01": binding}):
                with self.assertRaisesRegex(ValueError, "paused"):
                    start_recipe_program(self.app, other_recipe, requested_by="operator")

            # The paused program must be left untouched.
            state = db.session.get(RecipeProgramState, 1)
            self.assertEqual(state.status, "paused")


if __name__ == "__main__":
    unittest.main()
