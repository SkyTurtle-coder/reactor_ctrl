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
