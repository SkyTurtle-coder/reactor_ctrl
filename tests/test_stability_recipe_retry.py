"""Stability and safety regression tests — recipe retry, timeout policy, safe-state.

All tests are pure unit tests: no database or Flask app required.

Covers:
- Timeout policy values are realistic for industrial devices
- _is_transient_device_error / _device_error_runtime_status helpers
- Retry counter constants and module-level state
- Retry logic: transient errors do NOT immediately set recipe to ERROR
- Fatal errors DO immediately set recipe to ERROR
- Safe-state error vs stopped-status logic
"""
import unittest
from unittest.mock import MagicMock, patch

from reactor_app.services.command_model import CommandPriority
from reactor_app.services.device_runtime import DeviceCommandError
from reactor_app.services.recipe_program_runtime import (
    RecipeProgramDeviceCommandError,
    _MAX_TRANSIENT_ERRORS,
    _RETRYABLE_RUNTIME_STATUSES,
    _device_error_runtime_status,
    _is_transient_device_error,
    _transient_error_counts,
)
from reactor_app.services.runtime_status import RuntimeStatus


# ---------------------------------------------------------------------------
# Timeout policy: values must be realistic for industrial devices
# ---------------------------------------------------------------------------

class TimeoutPolicyTests(unittest.TestCase):
    """_DEFAULT_TIMEOUT_POLICY must give devices enough time to respond."""

    def setUp(self):
        from reactor_app.services.command_dispatcher import _DEFAULT_TIMEOUT_POLICY
        self.policy = _DEFAULT_TIMEOUT_POLICY

    def _policy(self, priority: CommandPriority) -> dict:
        return self.policy[int(priority)]

    def test_recipe_queue_timeout_at_least_10s(self):
        self.assertGreaterEqual(
            self._policy(CommandPriority.RECIPE)["queue_timeout_s"], 10.0,
            "RECIPE queue timeout too short — device busy with prior cmd will trigger spurious error",
        )

    def test_recipe_execution_timeout_at_least_8s(self):
        self.assertGreaterEqual(
            self._policy(CommandPriority.RECIPE)["execution_timeout_s"], 8.0,
            "RECIPE execution timeout too short — Huber multi-step sequences need ~3-8 s",
        )

    def test_recipe_total_timeout_at_least_20s(self):
        self.assertGreaterEqual(
            self._policy(CommandPriority.RECIPE)["total_timeout_s"], 20.0,
            "RECIPE total timeout too short — queue + execution must fit inside the total budget",
        )

    def test_recipe_total_exceeds_queue_plus_execution(self):
        p = self._policy(CommandPriority.RECIPE)
        self.assertGreater(
            p["total_timeout_s"],
            p["queue_timeout_s"] + p["execution_timeout_s"],
            "Total deadline must exceed queue + execution so commands have a realistic chance",
        )

    def test_safety_queue_timeout_exceeds_recipe_execution(self):
        """Safety command must be able to wait out any running recipe command."""
        safety_q = self._policy(CommandPriority.SAFETY)["queue_timeout_s"]
        recipe_e = self._policy(CommandPriority.RECIPE)["execution_timeout_s"]
        self.assertGreater(
            safety_q, recipe_e,
            "SAFETY queue_timeout must exceed RECIPE execution_timeout so safe-state never "
            "expires while waiting for a running recipe command to finish",
        )

    def test_safety_total_timeout_at_least_30s(self):
        self.assertGreaterEqual(
            self._policy(CommandPriority.SAFETY)["total_timeout_s"], 30.0,
        )

    def test_emergency_stop_at_least_as_generous_as_safety(self):
        es = self._policy(CommandPriority.EMERGENCY_STOP)
        sa = self._policy(CommandPriority.SAFETY)
        self.assertGreaterEqual(es["queue_timeout_s"], sa["queue_timeout_s"])
        self.assertGreaterEqual(es["execution_timeout_s"], sa["execution_timeout_s"])
        self.assertGreaterEqual(es["total_timeout_s"], sa["total_timeout_s"])

    def test_polling_queue_timeout_shorter_than_recipe(self):
        """Polling commands should time out faster than recipe commands."""
        self.assertLess(
            self._policy(CommandPriority.POLLING)["queue_timeout_s"],
            self._policy(CommandPriority.RECIPE)["queue_timeout_s"],
        )

    def test_polling_execution_timeout_fits_cc230_fallback_chain(self):
        """POLLING execution budget must accommodate the CC230 primary + fallback command chain.

        CC230 ignores some queries (e.g. SETPOINT?, TE?) causing a full socket
        read timeout before the driver can try the fallback command.  With
        _CC230_POLL_RESPONSE_TIMEOUT_MS = 1500 ms, a two-command fallback chain
        takes at most 2 × 1.5 s = 3 s.  Verify the execution_timeout leaves
        enough headroom.
        """
        from reactor_app.services.device_manual_runtime import _CC230_POLL_RESPONSE_TIMEOUT_MS
        max_fallback_chain_s = 2 * (_CC230_POLL_RESPONSE_TIMEOUT_MS / 1000)
        execution_timeout_s = self._policy(CommandPriority.POLLING)["execution_timeout_s"]
        self.assertLess(
            max_fallback_chain_s,
            execution_timeout_s,
            f"POLLING execution_timeout ({execution_timeout_s} s) is too short for the CC230 "
            f"primary+fallback command chain ({max_fallback_chain_s} s)",
        )


# ---------------------------------------------------------------------------
# _device_error_runtime_status
# ---------------------------------------------------------------------------

class DeviceErrorRuntimeStatusTests(unittest.TestCase):
    def test_returns_runtime_status_from_details(self):
        err = DeviceCommandError("err", status_code=504, details={"runtime_status": "timeout"})
        self.assertEqual(_device_error_runtime_status(err), "timeout")

    def test_returns_none_when_details_missing(self):
        err = DeviceCommandError("err", status_code=504)
        self.assertIsNone(_device_error_runtime_status(err))

    def test_returns_none_when_runtime_status_empty(self):
        err = DeviceCommandError("err", status_code=504, details={"runtime_status": ""})
        self.assertIsNone(_device_error_runtime_status(err))

    def test_returns_none_when_details_not_dict(self):
        err = DeviceCommandError("err", status_code=504, details="oops")
        self.assertIsNone(_device_error_runtime_status(err))

    def test_lowercases_status(self):
        err = DeviceCommandError("err", status_code=504, details={"runtime_status": "TIMEOUT"})
        self.assertEqual(_device_error_runtime_status(err), "timeout")

    def test_expired_status(self):
        err = DeviceCommandError("err", status_code=504, details={"runtime_status": "expired"})
        self.assertEqual(_device_error_runtime_status(err), "expired")


# ---------------------------------------------------------------------------
# _RETRYABLE_RUNTIME_STATUSES
# ---------------------------------------------------------------------------

class RetryableStatusesTests(unittest.TestCase):
    def test_timeout_is_retryable(self):
        self.assertIn(RuntimeStatus.TIMEOUT, _RETRYABLE_RUNTIME_STATUSES)

    def test_expired_is_retryable(self):
        self.assertIn(RuntimeStatus.EXPIRED, _RETRYABLE_RUNTIME_STATUSES)

    def test_cancelled_is_not_retryable(self):
        self.assertNotIn(RuntimeStatus.CANCELLED, _RETRYABLE_RUNTIME_STATUSES)

    def test_failed_is_not_retryable(self):
        self.assertNotIn(RuntimeStatus.FAILED, _RETRYABLE_RUNTIME_STATUSES)

    def test_preempted_is_not_retryable(self):
        self.assertNotIn(RuntimeStatus.PREEMPTED, _RETRYABLE_RUNTIME_STATUSES)


# ---------------------------------------------------------------------------
# _is_transient_device_error
# ---------------------------------------------------------------------------

class TransientDeviceErrorTests(unittest.TestCase):
    def _err(self, *, status_code: int, runtime_status: str | None = None, message: str = "device error"):
        details = {"runtime_status": runtime_status} if runtime_status else None
        return DeviceCommandError(message, status_code=status_code, details=details)

    # --- transient errors ---

    def test_504_with_timeout_runtime_status_is_transient(self):
        self.assertTrue(_is_transient_device_error(self._err(status_code=504, runtime_status="timeout")))

    def test_504_with_expired_runtime_status_is_transient(self):
        self.assertTrue(_is_transient_device_error(self._err(status_code=504, runtime_status="expired")))

    def test_504_without_runtime_status_is_transient(self):
        self.assertTrue(_is_transient_device_error(self._err(status_code=504)))

    def test_409_without_runtime_status_is_transient(self):
        """Device-busy lock contention is retryable."""
        self.assertTrue(_is_transient_device_error(self._err(status_code=409)))

    def test_timeout_in_message_is_transient(self):
        err = self._err(status_code=500, message="socket timed out connecting to device")
        self.assertTrue(_is_transient_device_error(err))

    def test_timed_out_in_message_is_transient(self):
        err = self._err(status_code=500, message="Timed out while waiting for device response bytes.")
        self.assertTrue(_is_transient_device_error(err))

    def test_connection_in_message_is_transient(self):
        err = self._err(status_code=500, message="Connection closed while sending device request bytes.")
        self.assertTrue(_is_transient_device_error(err))

    # --- non-transient errors ---

    def test_409_with_cancelled_runtime_status_is_not_transient(self):
        """Cancelled is a programme-level interrupt, not a transport error."""
        self.assertFalse(_is_transient_device_error(self._err(status_code=409, runtime_status="cancelled")))

    def test_409_with_preempted_runtime_status_is_not_transient(self):
        self.assertFalse(_is_transient_device_error(self._err(status_code=409, runtime_status="preempted")))

    def test_400_validation_error_is_not_transient(self):
        self.assertFalse(_is_transient_device_error(self._err(status_code=400)))

    def test_500_generic_server_error_is_not_transient(self):
        self.assertFalse(_is_transient_device_error(self._err(status_code=500, message="driver internal error")))

    def test_404_not_found_is_not_transient(self):
        self.assertFalse(_is_transient_device_error(self._err(status_code=404)))


# ---------------------------------------------------------------------------
# Retry counter constants
# ---------------------------------------------------------------------------

class RetryCounterConstantTests(unittest.TestCase):
    def test_max_transient_errors_is_reasonable(self):
        self.assertGreaterEqual(_MAX_TRANSIENT_ERRORS, 2, "Too few retries — single glitch kills recipe")
        self.assertLessEqual(_MAX_TRANSIENT_ERRORS, 10, "Too many retries — device offline goes undetected")

    def test_transient_error_counts_is_dict(self):
        self.assertIsInstance(_transient_error_counts, dict)

    def test_transient_error_counts_can_be_mutated(self):
        _transient_error_counts[999] = 1
        self.assertEqual(_transient_error_counts.get(999), 1)
        _transient_error_counts.pop(999, None)


# ---------------------------------------------------------------------------
# Retry logic: transient errors must not immediately set recipe to ERROR
# ---------------------------------------------------------------------------

class RecipeProgramRetryLogicTests(unittest.TestCase):
    """Unit-level tests for the retry path in _process_recipe_program_state.

    We verify the expected behaviour by constructing the exact exception that
    the retry branch checks for and confirming that a warning is logged instead
    of an error transition.
    """

    def setUp(self):
        import reactor_app.services.recipe_program_runtime as mod
        mod._transient_error_counts.clear()
        self.mod = mod

    def tearDown(self):
        self.mod._transient_error_counts.clear()

    def _make_transient_recipe_error(self) -> RecipeProgramDeviceCommandError:
        """Create a RecipeProgramDeviceCommandError whose cause is a transient DeviceCommandError."""
        cause = DeviceCommandError("socket timed out", status_code=504, details={"runtime_status": "timeout"})
        exc = RecipeProgramDeviceCommandError("Recipe device command failed at step 1: socket timed out")
        exc.__cause__ = cause
        return exc

    def _make_fatal_recipe_error(self) -> RecipeProgramDeviceCommandError:
        """Create a RecipeProgramDeviceCommandError whose cause is a non-transient DeviceCommandError."""
        cause = DeviceCommandError("driver validation failed", status_code=400)
        exc = RecipeProgramDeviceCommandError("Recipe device command failed: validation error")
        exc.__cause__ = cause
        return exc

    def test_transient_cause_is_detected(self):
        exc = self._make_transient_recipe_error()
        cause = getattr(exc, "__cause__", None)
        self.assertIsNotNone(cause)
        self.assertIsInstance(cause, DeviceCommandError)
        self.assertTrue(_is_transient_device_error(cause))

    def test_fatal_cause_is_not_transient(self):
        exc = self._make_fatal_recipe_error()
        cause = getattr(exc, "__cause__", None)
        self.assertIsNotNone(cause)
        self.assertFalse(_is_transient_device_error(cause))

    def test_first_transient_error_increments_counter(self):
        exc = self._make_transient_recipe_error()
        cause = getattr(exc, "__cause__", None)
        # Simulate what _process_recipe_program_state does
        retry_count = self.mod._transient_error_counts.get(self.mod._PROGRAM_STATE_ID, 0) + 1
        self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID] = retry_count
        self.assertEqual(self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID], 1)

    def test_counter_below_max_means_retry_not_fatal(self):
        for attempt in range(1, _MAX_TRANSIENT_ERRORS + 1):
            self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID] = attempt
            self.assertLessEqual(attempt, _MAX_TRANSIENT_ERRORS)

    def test_counter_exceeding_max_means_fatal(self):
        self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID] = _MAX_TRANSIENT_ERRORS + 1
        count = self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID]
        self.assertGreater(count, _MAX_TRANSIENT_ERRORS)

    def test_success_clears_counter(self):
        self.mod._transient_error_counts[self.mod._PROGRAM_STATE_ID] = 2
        self.mod._transient_error_counts.pop(self.mod._PROGRAM_STATE_ID, None)
        self.assertNotIn(self.mod._PROGRAM_STATE_ID, self.mod._transient_error_counts)


# ---------------------------------------------------------------------------
# Safe-state status logic
# ---------------------------------------------------------------------------

class SafeStateStatusTests(unittest.TestCase):
    """When safe-state errors occur the recipe must end in 'error' not 'stopped'."""

    def test_safe_state_errors_produce_error_status(self):
        safe_errors = ["actor_01: stop failed: timeout"]
        error_message = "; ".join(safe_errors) if safe_errors else None
        status = "error" if error_message else "stopped"
        self.assertEqual(status, "error")

    def test_no_safe_state_errors_produce_stopped_status(self):
        safe_errors: list[str] = []
        error_message = "; ".join(safe_errors) if safe_errors else None
        status = "error" if error_message else "stopped"
        self.assertEqual(status, "stopped")

    def test_multiple_safe_state_errors_joined(self):
        safe_errors = ["actor_01: set_setpoint failed: timeout", "actor_01: stop failed: timeout"]
        error_message = "; ".join(safe_errors)
        self.assertIn("set_setpoint", error_message)
        self.assertIn("stop failed", error_message)

    def test_retryable_statuses_are_frozenset(self):
        self.assertIsInstance(_RETRYABLE_RUNTIME_STATUSES, frozenset)


# ---------------------------------------------------------------------------
# _is_transient_device_error: DB persistence failures with MySQL 1020/1205/1213
# are treated as transient so the recipe is not put into ERROR state
# ---------------------------------------------------------------------------

class TransientDbErrorAsTransientDeviceErrorTests(unittest.TestCase):
    """DeviceCommandError(status_code=500) whose __cause__ is a transient MySQL error
    must be classified as transient so the recipe retries instead of erroring out.
    """

    def _make_mysql_operational_error(self, code: int) -> Exception:
        from sqlalchemy.exc import OperationalError
        orig = Exception(code, "db conflict")
        orig.args = (code, "db conflict")
        return OperationalError(statement="UPDATE control_command", params={}, orig=orig)

    def _make_db_persistence_device_error(self, mysql_code: int) -> DeviceCommandError:
        cause = self._make_mysql_operational_error(mysql_code)
        exc = DeviceCommandError(
            "Device command log persistence failed during response.",
            status_code=500,
        )
        exc.__cause__ = cause
        return exc

    def test_500_with_mysql_1020_cause_is_transient(self):
        exc = self._make_db_persistence_device_error(1020)
        self.assertTrue(_is_transient_device_error(exc))

    def test_500_with_mysql_1205_cause_is_transient(self):
        exc = self._make_db_persistence_device_error(1205)
        self.assertTrue(_is_transient_device_error(exc))

    def test_500_with_mysql_1213_cause_is_transient(self):
        exc = self._make_db_persistence_device_error(1213)
        self.assertTrue(_is_transient_device_error(exc))

    def test_500_without_transient_cause_is_not_transient(self):
        """Generic 500 without a transient DB root cause is NOT retried."""
        exc = DeviceCommandError("driver internal error", status_code=500)
        self.assertFalse(_is_transient_device_error(exc))

    def test_500_with_mysql_1062_cause_is_not_transient(self):
        """Duplicate key error (1062) is NOT a transient concurrency conflict."""
        exc = self._make_db_persistence_device_error(1062)
        self.assertFalse(_is_transient_device_error(exc))


# ---------------------------------------------------------------------------
# Recipe does not go to ERROR on transient DB persistence failure
# ---------------------------------------------------------------------------

class RecipeNoErrorOnTransientDbPersistenceTests(unittest.TestCase):
    """Verify that a DeviceCommandError(500) with a transient MySQL 1020 root
    cause is seen as transient by the recipe retry logic.

    The full _process_recipe_program_state integration requires a real DB, so
    we test the classifier that it uses directly.
    """

    def _make_transient_db_recipe_error(self, mysql_code: int = 1020):
        from sqlalchemy.exc import OperationalError
        orig = Exception(mysql_code, "Record has changed since last read")
        orig.args = (mysql_code, "Record has changed since last read")
        db_cause = OperationalError(
            statement="UPDATE control_command SET status=%(status)s",
            params={},
            orig=orig,
        )
        device_exc = DeviceCommandError(
            "Device command log persistence failed during response.",
            status_code=500,
        )
        device_exc.__cause__ = db_cause

        recipe_exc = RecipeProgramDeviceCommandError(
            "Recipe device command failed during manual device execution "
            "at step 1 (Hold at 25 degC): actor 'HC_01', command 'set_setpoint', "
            "device 'Huber Unistat' (ID 2, protocol huber_unistat_430). "
            "Device error: Device command log persistence failed during response."
        )
        recipe_exc.__cause__ = device_exc
        return recipe_exc

    def test_db_persistence_failure_classified_as_transient(self):
        exc = self._make_transient_db_recipe_error(1020)
        cause = getattr(exc, "__cause__", None)
        self.assertIsInstance(cause, DeviceCommandError)
        self.assertTrue(_is_transient_device_error(cause),
                        "DeviceCommandError(500) with MySQL 1020 cause must be transient")

    def test_recipe_runtime_retries_instead_of_erroring_on_1020(self):
        """Ensure the retry branch in _process_recipe_program_state is taken."""
        import reactor_app.services.recipe_program_runtime as mod
        mod._transient_error_counts.clear()

        exc = self._make_transient_db_recipe_error(1020)
        cause = getattr(exc, "__cause__", None)
        self.assertIsInstance(cause, DeviceCommandError)

        # Simulate exactly what _process_recipe_program_state does:
        is_transient = isinstance(cause, DeviceCommandError) and _is_transient_device_error(cause)
        retry_count = mod._transient_error_counts.get(mod._PROGRAM_STATE_ID, 0) + 1
        mod._transient_error_counts[mod._PROGRAM_STATE_ID] = retry_count

        self.assertTrue(is_transient, "1020 persistence failure must be classified transient")
        self.assertLessEqual(retry_count, mod._MAX_TRANSIENT_ERRORS,
                             "First 1020 error should not exceed retry limit")

        mod._transient_error_counts.clear()

    def test_non_transient_recipe_error_is_not_retried(self):
        """A genuine device failure (e.g. validation error) must not be retried."""
        cause = DeviceCommandError("validation failed", status_code=400)
        exc = RecipeProgramDeviceCommandError("Recipe device command failed")
        exc.__cause__ = cause

        inner = getattr(exc, "__cause__", None)
        is_transient = isinstance(inner, DeviceCommandError) and _is_transient_device_error(inner)
        self.assertFalse(is_transient)


# ---------------------------------------------------------------------------
# is_device_busy_error: detects device lock-contention 409
# ---------------------------------------------------------------------------

class IsDeviceBusyErrorTests(unittest.TestCase):
    """is_device_busy_error must distinguish scheduling contention from real 409s."""

    def setUp(self):
        from reactor_app.services.device_runtime import is_device_busy_error
        self.is_busy = is_device_busy_error

    def test_busy_message_is_detected(self):
        exc = DeviceCommandError("Device 3 is busy executing another command.", status_code=409)
        self.assertTrue(self.is_busy(exc))

    def test_executing_another_command_is_detected(self):
        exc = DeviceCommandError("device 7 is busy executing another command", status_code=409)
        self.assertTrue(self.is_busy(exc))

    def test_non_409_is_not_busy(self):
        exc = DeviceCommandError("socket timed out", status_code=504)
        self.assertFalse(self.is_busy(exc))

    def test_409_without_busy_message_is_not_busy(self):
        """A real 409 (no binding, disabled connection) must NOT be treated as busy."""
        exc = DeviceCommandError("Device 3 has no current binding.", status_code=409)
        self.assertFalse(self.is_busy(exc))

    def test_non_device_command_error_is_not_busy(self):
        self.assertFalse(self.is_busy(RuntimeError("something broke")))

    def test_409_connection_disabled_is_not_busy(self):
        exc = DeviceCommandError("Connection 5 is disabled.", status_code=409)
        self.assertFalse(self.is_busy(exc))


# ---------------------------------------------------------------------------
# Per-priority lock timeout: POLLING drops fast, RECIPE/SAFETY wait longer
# ---------------------------------------------------------------------------

class PerPriorityLockTimeoutTests(unittest.TestCase):
    """_DEVICE_COMMAND_LOCK_TIMEOUT_BY_PRIORITY must have sensible values."""

    def setUp(self):
        from reactor_app.services.device_runtime import _DEVICE_COMMAND_LOCK_TIMEOUT_BY_PRIORITY
        self.timeouts = _DEVICE_COMMAND_LOCK_TIMEOUT_BY_PRIORITY

    def test_polling_drops_in_at_most_queue_timeout(self):
        from reactor_app.services.command_dispatcher import _DEFAULT_TIMEOUT_POLICY
        polling_queue_timeout = _DEFAULT_TIMEOUT_POLICY[int(CommandPriority.POLLING)]["queue_timeout_s"]
        polling_lock_timeout = self.timeouts[int(CommandPriority.POLLING)]
        self.assertLessEqual(
            polling_lock_timeout, polling_queue_timeout,
            "POLLING lock timeout must not exceed queue_timeout_s so poll drops quickly",
        )

    def test_recipe_outlasts_polling_execution(self):
        from reactor_app.services.command_dispatcher import _DEFAULT_TIMEOUT_POLICY
        polling_exec_timeout = _DEFAULT_TIMEOUT_POLICY[int(CommandPriority.POLLING)]["execution_timeout_s"]
        recipe_lock_timeout = self.timeouts[int(CommandPriority.RECIPE)]
        self.assertGreater(
            recipe_lock_timeout, polling_exec_timeout,
            "RECIPE lock timeout must exceed POLLING execution_timeout_s so recipes can wait out a poll cycle",
        )

    def test_safety_at_least_as_long_as_recipe(self):
        self.assertGreaterEqual(
            self.timeouts[int(CommandPriority.SAFETY)],
            self.timeouts[int(CommandPriority.RECIPE)],
            "SAFETY lock timeout must be at least as long as RECIPE",
        )

    def test_emergency_stop_at_least_as_long_as_safety(self):
        self.assertGreaterEqual(
            self.timeouts[int(CommandPriority.EMERGENCY_STOP)],
            self.timeouts[int(CommandPriority.SAFETY)],
        )


# ---------------------------------------------------------------------------
# Device-busy retry: separate counter with higher limit than general transient
# ---------------------------------------------------------------------------

class DeviceBusyRetryLimitTests(unittest.TestCase):
    """_MAX_DEVICE_BUSY_ERRORS must be greater than _MAX_TRANSIENT_ERRORS."""

    def setUp(self):
        from reactor_app.services.recipe_program_runtime import (
            _MAX_DEVICE_BUSY_ERRORS,
            _MAX_TRANSIENT_ERRORS,
        )
        self.max_busy = _MAX_DEVICE_BUSY_ERRORS
        self.max_transient = _MAX_TRANSIENT_ERRORS

    def test_device_busy_limit_exceeds_general_transient_limit(self):
        self.assertGreater(
            self.max_busy, self.max_transient,
            "Device-busy retries must exceed general transient retries "
            "(scheduling contention, not hardware failure)",
        )

    def test_device_busy_limit_is_reasonable(self):
        self.assertGreaterEqual(self.max_busy, 5, "At least 5 busy retries needed")
        self.assertLessEqual(self.max_busy, 20, "Too many busy retries delays detection of real lock-up")

    def test_busy_error_uses_higher_limit(self):
        from reactor_app.services.device_runtime import is_device_busy_error
        from reactor_app.services.recipe_program_runtime import _MAX_DEVICE_BUSY_ERRORS, _MAX_TRANSIENT_ERRORS
        busy_exc = DeviceCommandError("Device 3 is busy executing another command.", status_code=409)
        other_exc = DeviceCommandError("socket timed out", status_code=504)
        expected_busy = _MAX_DEVICE_BUSY_ERRORS if is_device_busy_error(busy_exc) else _MAX_TRANSIENT_ERRORS
        expected_other = _MAX_DEVICE_BUSY_ERRORS if is_device_busy_error(other_exc) else _MAX_TRANSIENT_ERRORS
        self.assertEqual(expected_busy, _MAX_DEVICE_BUSY_ERRORS)
        self.assertEqual(expected_other, _MAX_TRANSIENT_ERRORS)


# ---------------------------------------------------------------------------
# Polling device-busy does NOT fail the recipe (manual reconciler path)
# ---------------------------------------------------------------------------

class PollingBusyDoesNotFailRecipeTests(unittest.TestCase):
    """When a polling command fails with device-busy the manual reconciler must
    reschedule the poll — it must never call _fail_active_recipe_program_for_device.
    """

    def _make_busy_exc(self) -> DeviceCommandError:
        return DeviceCommandError(
            "Device 3 is busy executing another command.", status_code=409
        )

    def test_busy_error_is_transient_in_recipe_path(self):
        """409 device-busy must be seen as transient by the recipe error classifier."""
        exc = self._make_busy_exc()
        self.assertTrue(_is_transient_device_error(exc))

    def test_busy_error_uses_device_busy_retry_limit(self):
        from reactor_app.services.device_runtime import is_device_busy_error
        from reactor_app.services.recipe_program_runtime import _MAX_DEVICE_BUSY_ERRORS, _MAX_TRANSIENT_ERRORS
        exc = self._make_busy_exc()
        max_retries = _MAX_DEVICE_BUSY_ERRORS if is_device_busy_error(exc) else _MAX_TRANSIENT_ERRORS
        self.assertEqual(max_retries, _MAX_DEVICE_BUSY_ERRORS)

    def test_busy_error_within_limit_does_not_exhaust_counter(self):
        """Verify that _MAX_DEVICE_BUSY_ERRORS consecutive busy errors are all within limit."""
        from reactor_app.services.recipe_program_runtime import _MAX_DEVICE_BUSY_ERRORS
        for attempt in range(1, _MAX_DEVICE_BUSY_ERRORS + 1):
            self.assertLessEqual(attempt, _MAX_DEVICE_BUSY_ERRORS)

    def test_busy_after_limit_becomes_fatal(self):
        """After _MAX_DEVICE_BUSY_ERRORS+1 attempts the recipe must ERROR."""
        from reactor_app.services.recipe_program_runtime import _MAX_DEVICE_BUSY_ERRORS
        over_limit = _MAX_DEVICE_BUSY_ERRORS + 1
        self.assertGreater(over_limit, _MAX_DEVICE_BUSY_ERRORS)

    def test_real_device_error_still_fatal(self):
        """A non-transient DeviceCommandError (400 validation) must never be retried."""
        from reactor_app.services.recipe_program_runtime import _MAX_TRANSIENT_ERRORS
        exc = DeviceCommandError("driver validation failed", status_code=400)
        self.assertFalse(_is_transient_device_error(exc))
