"""Phase 1 stability tests.

Covers the concrete changes made in the DB-integrity and runtime-stabilisation
pass:

  1. _is_transient_mysql_error / _is_duplicate_key_error classification helpers
  2. _ensure_program_state: IntegrityError handling for concurrent singleton inserts
  3. start_recipe_program: TOCTOU guard — rejects a second concurrent start
  4. _process_recipe_program_state: transient MySQL errors (1020/1205/1213) must NOT
     set recipe status to "error"; non-transient errors must set status to "error"
  5. _ensure_manual_state_row: IntegrityError handling for concurrent inserts
  6. _ensure_manual_state: IntegrityError handling with re-read fallback
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy.exc import IntegrityError, OperationalError

from reactor_app.models import Device, DeviceManualState, ReactorBuild, Recipe, RecipeProgramState
from reactor_app.services import device_manual_runtime, recipe_program_runtime
from reactor_app.services.recipe_program_runtime import (
    _is_duplicate_key_error,
    _is_transient_mysql_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _operational_error(code: int, msg: str = "db error") -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(code, msg))


def _integrity_error(code: int, msg: str = "constraint violation") -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception(code, msg))


# ---------------------------------------------------------------------------
# 1. Error classification helpers
# ---------------------------------------------------------------------------

class IsTransientMysqlErrorTests(unittest.TestCase):
    def test_1020_record_changed_is_transient(self):
        self.assertTrue(_is_transient_mysql_error(_operational_error(1020)))

    def test_1205_lock_wait_timeout_is_transient(self):
        self.assertTrue(_is_transient_mysql_error(_operational_error(1205)))

    def test_1213_deadlock_is_transient(self):
        self.assertTrue(_is_transient_mysql_error(_operational_error(1213)))

    def test_1062_duplicate_key_is_not_transient(self):
        self.assertFalse(_is_transient_mysql_error(_operational_error(1062)))

    def test_1045_access_denied_is_not_transient(self):
        self.assertFalse(_is_transient_mysql_error(_operational_error(1045)))

    def test_unknown_code_is_not_transient(self):
        self.assertFalse(_is_transient_mysql_error(_operational_error(9999)))


class IsDuplicateKeyErrorTests(unittest.TestCase):
    def test_mysql_1062_is_duplicate_key(self):
        err = _integrity_error(1062, "Duplicate entry '1' for key 'PRIMARY'")
        self.assertTrue(_is_duplicate_key_error(err))

    def test_1451_fk_violation_is_not_duplicate(self):
        err = _integrity_error(1451, "FK constraint")
        self.assertFalse(_is_duplicate_key_error(err))

    def test_duplicate_entry_in_message_is_detected(self):
        original = Exception(1062, "Duplicate entry 'x' for key 'uq_foo'")
        err = IntegrityError("INSERT", {}, original)
        self.assertTrue(_is_duplicate_key_error(err))


# ---------------------------------------------------------------------------
# 2. _ensure_program_state: concurrent singleton insert handling
# ---------------------------------------------------------------------------

class _FakeSessionForEnsureProgramState:
    """Simulates a session where the first get() returns None (row missing),
    flush() raises IntegrityError (another worker inserted the row first),
    rollback() is called, and the second get() returns the already-inserted row."""

    def __init__(self, *, existing_state: RecipeProgramState | None = None, flush_raises: Exception | None = None):
        self._existing_state = existing_state
        self._flush_raises = flush_raises
        self._get_count = 0
        self.add_calls = 0
        self.flush_calls = 0
        self.rollback_calls = 0

    def get(self, model, pk):
        self._get_count += 1
        if self._get_count == 1:
            return None  # row not found on first look
        return self._existing_state  # found on second look (after rollback)

    def add(self, obj):
        self.add_calls += 1

    def flush(self):
        self.flush_calls += 1
        if self._flush_raises is not None:
            raise self._flush_raises

    def rollback(self):
        self.rollback_calls += 1


class EnsureProgramStateTests(unittest.TestCase):
    def test_returns_existing_state_without_insert(self):
        existing = RecipeProgramState(recipe_program_state_id=1, status="idle", active_step_index=0)

        class _Session:
            def get(self, model, pk):
                return existing

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=_Session())):
            result = recipe_program_runtime._ensure_program_state()

        self.assertIs(result, existing)

    def test_creates_state_when_not_found(self):
        fake = _FakeSessionForEnsureProgramState(existing_state=None, flush_raises=None)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake)):
            result = recipe_program_runtime._ensure_program_state()

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "idle")
        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(fake.flush_calls, 1)
        self.assertEqual(fake.rollback_calls, 0)

    def test_handles_concurrent_integrity_error_by_re_reading(self):
        concurrent_state = RecipeProgramState(
            recipe_program_state_id=1, status="idle", active_step_index=0
        )
        integrity_err = _integrity_error(1062, "Duplicate entry '1' for key 'PRIMARY'")
        fake = _FakeSessionForEnsureProgramState(
            existing_state=concurrent_state,
            flush_raises=integrity_err,
        )

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake)):
            result = recipe_program_runtime._ensure_program_state()

        self.assertIs(result, concurrent_state)
        self.assertEqual(fake.rollback_calls, 1, "rollback must be called after IntegrityError")
        self.assertEqual(fake._get_count, 2, "second get() must re-read the row")

    def test_raises_runtime_error_when_re_read_also_returns_none(self):
        """If the row is still missing after rollback, a RuntimeError must be raised."""
        integrity_err = _integrity_error(1062, "Duplicate entry '1' for key 'PRIMARY'")
        fake = _FakeSessionForEnsureProgramState(
            existing_state=None,  # re-read also returns None
            flush_raises=integrity_err,
        )

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake)):
            with self.assertRaises(RuntimeError):
                recipe_program_runtime._ensure_program_state()


# ---------------------------------------------------------------------------
# 3. start_recipe_program: TOCTOU guard
# ---------------------------------------------------------------------------

class _FakeSessionForStartRecipe:
    """Session for start_recipe_program tests.

    get(ReactorBuild, ...) → always returns a build
    get(RecipeProgramState, ...) → returns program_state (may be "running")
    execute() → no-op (FOR UPDATE lock attempt)
    """

    def __init__(self, program_state: RecipeProgramState):
        self._program_state = program_state

    def get(self, model, pk):
        if model is ReactorBuild:
            return ReactorBuild(reactor_build_id=int(pk))
        if model is RecipeProgramState:
            return self._program_state
        return None

    def execute(self, *args, **kwargs):
        return MagicMock()

    def add(self, obj):
        pass

    def flush(self):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


class StartRecipeProgramTests(unittest.TestCase):
    def _make_recipe(self):
        recipe = Recipe(recipe_id=1, reactor_build_id=1, title="Test Recipe", operator_name="tester")
        recipe.steps_json = []
        return recipe

    def test_raises_value_error_when_already_running(self):
        running_state = RecipeProgramState(
            recipe_program_state_id=1,
            status="running",
            active_step_index=0,
            stop_requested=False,
        )
        app = Flask(__name__)
        fake_session = _FakeSessionForStartRecipe(program_state=running_state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_snapshot_for_recipe", return_value={"steps": [], "bindings": []}):
                with self.assertRaises(ValueError) as ctx:
                    recipe_program_runtime.start_recipe_program(
                        app, self._make_recipe(), requested_by="user"
                    )

        self.assertIn("already running", str(ctx.exception).lower())

    def test_does_not_raise_when_status_is_idle(self):
        idle_state = RecipeProgramState(
            recipe_program_state_id=1,
            status="idle",
            active_step_index=0,
            stop_requested=False,
        )
        app = Flask(__name__)
        fake_session = _FakeSessionForStartRecipe(program_state=idle_state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_snapshot_for_recipe", return_value={"steps": [], "bindings": []}):
                with patch.object(recipe_program_runtime, "_record_program_event"):
                    # Should not raise
                    result = recipe_program_runtime.start_recipe_program(
                        app, self._make_recipe(), requested_by="user"
                    )

        self.assertEqual(result.status, "running")

    def test_does_not_raise_when_status_is_stopped(self):
        stopped_state = RecipeProgramState(
            recipe_program_state_id=1,
            status="stopped",
            active_step_index=0,
            stop_requested=False,
        )
        app = Flask(__name__)
        fake_session = _FakeSessionForStartRecipe(program_state=stopped_state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_snapshot_for_recipe", return_value={"steps": [], "bindings": []}):
                with patch.object(recipe_program_runtime, "_record_program_event"):
                    result = recipe_program_runtime.start_recipe_program(
                        app, self._make_recipe(), requested_by="user"
                    )

        self.assertEqual(result.status, "running")


# ---------------------------------------------------------------------------
# 4. _process_recipe_program_state: transient vs non-transient error handling
# ---------------------------------------------------------------------------

class _FakeSessionForReconciler:
    """Minimal session stub for _process_recipe_program_state tests."""

    def __init__(self, state: RecipeProgramState):
        self._state = state
        self.rollback_calls = 0
        self.commit_calls = 0
        self.execute_calls: list[str] = []

    def get(self, model, pk):
        if model is RecipeProgramState:
            return self._state
        return None

    def execute(self, stmt, params=None):
        self.execute_calls.append(str(stmt))
        return MagicMock()

    def rollback(self):
        self.rollback_calls += 1

    def commit(self):
        self.commit_calls += 1


class ReconcilerTransientErrorTests(unittest.TestCase):
    def _make_running_state(self) -> RecipeProgramState:
        state = RecipeProgramState(
            recipe_program_state_id=1,
            status="running",
            active_step_index=0,
            stop_requested=False,
            lease_owner="worker-1",
        )
        state.snapshot_json = {"steps": [], "bindings": []}
        return state

    def test_transient_1213_deadlock_does_not_set_status_to_error(self):
        state = self._make_running_state()
        app = Flask(__name__)
        fake_session = _FakeSessionForReconciler(state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_claim_allows_target_application", return_value=True):
                with patch.object(
                    recipe_program_runtime,
                    "_ensure_open_program_run",
                    side_effect=_operational_error(1213, "Deadlock found when trying to get lock"),
                ):
                    recipe_program_runtime._process_recipe_program_state(app, worker_id="worker-1")

        self.assertNotEqual(state.status, "error", "Deadlock must not mark recipe as error")
        self.assertEqual(state.status, "running")
        self.assertEqual(fake_session.rollback_calls, 1)
        # Lease-release UPDATE must have been executed
        self.assertTrue(
            any("lease_owner" in s for s in fake_session.execute_calls),
            "Lease release UPDATE must be executed on transient error",
        )

    def test_transient_1205_lock_wait_does_not_set_status_to_error(self):
        state = self._make_running_state()
        app = Flask(__name__)
        fake_session = _FakeSessionForReconciler(state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_claim_allows_target_application", return_value=True):
                with patch.object(
                    recipe_program_runtime,
                    "_ensure_open_program_run",
                    side_effect=_operational_error(1205, "Lock wait timeout exceeded"),
                ):
                    recipe_program_runtime._process_recipe_program_state(app, worker_id="worker-1")

        self.assertNotEqual(state.status, "error")
        self.assertEqual(fake_session.rollback_calls, 1)

    def test_transient_1020_record_changed_does_not_set_status_to_error(self):
        state = self._make_running_state()
        app = Flask(__name__)
        fake_session = _FakeSessionForReconciler(state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_claim_allows_target_application", return_value=True):
                with patch.object(
                    recipe_program_runtime,
                    "_ensure_open_program_run",
                    side_effect=_operational_error(1020, "Record has changed since last read"),
                ):
                    recipe_program_runtime._process_recipe_program_state(app, worker_id="worker-1")

        self.assertNotEqual(state.status, "error")
        self.assertEqual(fake_session.rollback_calls, 1)

    def test_non_transient_runtime_error_sets_status_to_error(self):
        state = self._make_running_state()
        app = Flask(__name__)
        fake_session = _FakeSessionForReconciler(state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_claim_allows_target_application", return_value=True):
                with patch.object(
                    recipe_program_runtime,
                    "_ensure_open_program_run",
                    side_effect=RuntimeError("device command failed unexpectedly"),
                ):
                    with patch.object(recipe_program_runtime, "_record_program_event"):
                        recipe_program_runtime._process_recipe_program_state(app, worker_id="worker-1")

        self.assertEqual(state.status, "error")
        self.assertIn("device command failed", state.last_error or "")
        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 1)

    def test_non_transient_operational_error_sets_status_to_error(self):
        state = self._make_running_state()
        app = Flask(__name__)
        fake_session = _FakeSessionForReconciler(state)

        with patch.object(recipe_program_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(recipe_program_runtime, "_program_claim_allows_target_application", return_value=True):
                with patch.object(
                    recipe_program_runtime,
                    "_ensure_open_program_run",
                    side_effect=_operational_error(1045, "Access denied for user"),
                ):
                    with patch.object(recipe_program_runtime, "_record_program_event"):
                        recipe_program_runtime._process_recipe_program_state(app, worker_id="worker-1")

        self.assertEqual(state.status, "error")
        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 1)


# ---------------------------------------------------------------------------
# 5. _ensure_manual_state_row: concurrent insert handling
# ---------------------------------------------------------------------------

class _FakeSessionForManualStateRow:
    def __init__(self, *, existing_state=None, flush_raises=None):
        self._existing_state = existing_state
        self._flush_raises = flush_raises
        self.add_calls = 0
        self.flush_calls = 0
        self.rollback_calls = 0

    def get(self, model, pk):
        return self._existing_state

    def add(self, obj):
        self.add_calls += 1

    def flush(self):
        self.flush_calls += 1
        if self._flush_raises is not None:
            raise self._flush_raises

    def rollback(self):
        self.rollback_calls += 1


class EnsureManualStateRowTests(unittest.TestCase):
    def test_skips_insert_when_row_already_exists(self):
        existing = DeviceManualState(device_id=5, queue_status="idle", desired_version=0, applied_version=0)
        fake = _FakeSessionForManualStateRow(existing_state=existing)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            device_manual_runtime._ensure_manual_state_row(5)

        self.assertEqual(fake.add_calls, 0)
        self.assertEqual(fake.flush_calls, 0)

    def test_inserts_row_when_missing(self):
        fake = _FakeSessionForManualStateRow(existing_state=None)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            device_manual_runtime._ensure_manual_state_row(5)

        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(fake.flush_calls, 1)
        self.assertEqual(fake.rollback_calls, 0)

    def test_handles_integrity_error_silently_without_propagating(self):
        integrity_err = _integrity_error(1062, "Duplicate entry '5' for key 'PRIMARY'")
        fake = _FakeSessionForManualStateRow(existing_state=None, flush_raises=integrity_err)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            # Must not raise
            device_manual_runtime._ensure_manual_state_row(5)

        self.assertEqual(fake.rollback_calls, 1, "rollback must be called on IntegrityError")

    def test_handles_integrity_error_for_different_device_ids(self):
        for device_id in (1, 42, 99):
            with self.subTest(device_id=device_id):
                integrity_err = _integrity_error(1062, f"Duplicate entry '{device_id}' for key 'PRIMARY'")
                fake = _FakeSessionForManualStateRow(existing_state=None, flush_raises=integrity_err)

                with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
                    device_manual_runtime._ensure_manual_state_row(device_id)

                self.assertEqual(fake.rollback_calls, 1)


# ---------------------------------------------------------------------------
# 6. _ensure_manual_state: concurrent insert with re-read
# ---------------------------------------------------------------------------

class _FakeSessionForEnsureManualState:
    def __init__(self, *, existing_state=None, flush_raises=None, concurrent_state=None):
        self._existing_state = existing_state
        self._flush_raises = flush_raises
        self._concurrent_state = concurrent_state
        self._get_count = 0
        self.add_calls = 0
        self.flush_calls = 0
        self.rollback_calls = 0

    def get(self, model, pk):
        self._get_count += 1
        if self._get_count == 1:
            return self._existing_state
        return self._concurrent_state

    def add(self, obj):
        self.add_calls += 1

    def flush(self):
        self.flush_calls += 1
        if self._flush_raises is not None:
            raise self._flush_raises

    def rollback(self):
        self.rollback_calls += 1


class EnsureManualStateTests(unittest.TestCase):
    def _make_device(self, device_id: int = 3) -> Device:
        return Device(device_id=device_id, asset_serial=f"IKA-{device_id}", display_name="IKA")

    def test_returns_existing_state_immediately(self):
        device = self._make_device()
        existing = DeviceManualState(device_id=3, queue_status="idle", desired_version=1, applied_version=1)
        fake = _FakeSessionForEnsureManualState(existing_state=existing)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            result = device_manual_runtime._ensure_manual_state(device)

        self.assertIs(result, existing)
        self.assertEqual(fake.add_calls, 0)

    def test_creates_new_state_when_missing(self):
        device = self._make_device()
        fake = _FakeSessionForEnsureManualState(existing_state=None)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            result = device_manual_runtime._ensure_manual_state(device)

        self.assertIsNotNone(result)
        self.assertEqual(result.device_id, 3)
        self.assertEqual(result.queue_status, "idle")
        self.assertEqual(fake.add_calls, 1)
        self.assertEqual(fake.rollback_calls, 0)

    def test_handles_concurrent_insert_by_re_reading(self):
        device = self._make_device()
        concurrent = DeviceManualState(device_id=3, queue_status="idle", desired_version=0, applied_version=0)
        integrity_err = _integrity_error(1062, "Duplicate entry '3' for key 'PRIMARY'")
        fake = _FakeSessionForEnsureManualState(
            existing_state=None,
            flush_raises=integrity_err,
            concurrent_state=concurrent,
        )

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            result = device_manual_runtime._ensure_manual_state(device)

        self.assertIs(result, concurrent)
        self.assertEqual(fake.rollback_calls, 1)
        self.assertEqual(fake._get_count, 2)

    def test_raises_runtime_error_when_re_read_returns_none_after_integrity_error(self):
        device = self._make_device()
        integrity_err = _integrity_error(1062, "Duplicate entry '3' for key 'PRIMARY'")
        fake = _FakeSessionForEnsureManualState(
            existing_state=None,
            flush_raises=integrity_err,
            concurrent_state=None,  # still None after rollback
        )

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake)):
            with self.assertRaises(RuntimeError) as ctx:
                device_manual_runtime._ensure_manual_state(device)

        self.assertIn("3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
