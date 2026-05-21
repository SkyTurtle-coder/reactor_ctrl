import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy.exc import OperationalError

from reactor_app.models import Device, DeviceManualState, RecipeProgramState
from reactor_app.services import device_manual_runtime


class _FakeSessionForProcess:
    def __init__(self, *, state, device, program_state=None):
        self._state = state
        self._device = device
        self._program_state = program_state
        self.commit_calls = 0
        self.rollback_calls = 0

    def get(self, model, _device_id):
        if model is DeviceManualState:
            return self._state
        if model is Device:
            return self._device
        if model is RecipeProgramState:
            return self._program_state
        raise AssertionError(f"Unexpected model lookup: {model}")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class _FakeSessionForClaim:
    def __init__(self, *, candidates, claim_side_effects):
        self._query_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

        self._candidates_query = MagicMock()
        self._candidates_query.join.return_value = self._candidates_query
        self._candidates_query.outerjoin.return_value = self._candidates_query
        self._candidates_query.filter.return_value = self._candidates_query
        self._candidates_query.order_by.return_value = self._candidates_query
        self._candidates_query.limit.return_value = self._candidates_query
        self._candidates_query.all.return_value = list(candidates)

        self._claim_query = MagicMock()
        self._claim_query.filter.return_value = self._claim_query
        self._claim_query.update.side_effect = list(claim_side_effects)

    def query(self, *_args):
        self._query_calls += 1
        if self._query_calls == 1:
            return self._candidates_query
        return self._claim_query

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class _FakeSessionForRetry:
    def __init__(self):
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1


class _FakeManualStateUpdateQuery:
    def __init__(self, session, update_side_effects):
        self._session = session
        self._update_side_effects = update_side_effects

    def filter_by(self, **_kwargs):
        return self

    def update(self, values, synchronize_session=False):
        self._session.update_calls += 1
        if synchronize_session is not False:
            raise AssertionError("manual-state updates must not synchronize stale ORM state")
        if self._update_side_effects:
            effect = self._update_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            if not effect:
                return int(effect)

        for column, value in values.items():
            name = getattr(column, "key", None)
            if not name:
                continue
            if name in {"desired_is_on", "desired_speed"} and not isinstance(value, (bool, int, type(None))):
                continue
            setattr(self._session._state, name, value)
        return 1


class _FakeSessionForManualStateUpdate:
    def __init__(self, *, state, update_side_effects=()):
        self._state = state
        self._update_side_effects = list(update_side_effects)
        self.update_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.expire_all_calls = 0

    def query(self, *_args):
        return _FakeManualStateUpdateQuery(self, self._update_side_effects)

    def get(self, model, _device_id):
        if model is DeviceManualState:
            return self._state
        raise AssertionError(f"Unexpected model lookup: {model}")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1

    def expire_all(self):
        self.expire_all_calls += 1


class _FakeSessionForMeasurementBestEffort:
    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0

    def query(self, *_args):
        raise AssertionError("query should not be reached when persistence is patched")

    def add(self, *_args):
        raise AssertionError("add should not be reached when persistence is patched")

    def flush(self):
        raise AssertionError("flush should not be reached when persistence is patched")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class DeviceManualRuntimeTests(unittest.TestCase):
    def test_manual_state_to_dict_normalizes_naive_datetimes_to_utc(self):
        naive = datetime(2026, 4, 13, 15, 20, 53, 123000)
        state = DeviceManualState(
            device_id=2,
            queue_status="idle",
            desired_version=4,
            applied_version=3,
            requested_by="process_manual",
        )
        state.last_desired_at = naive
        state.last_reported_at = naive
        state.next_poll_at = naive
        state.watch_expires_at = naive

        payload = device_manual_runtime.manual_state_to_dict(state)

        self.assertEqual(payload["desired_state"]["updated_at"], "2026-04-13T15:20:53.123000+00:00")
        self.assertEqual(payload["reported_state"]["updated_at"], "2026-04-13T15:20:53.123000+00:00")
        self.assertEqual(payload["next_poll_at"], "2026-04-13T15:20:53.123000+00:00")
        self.assertEqual(payload["watch_expires_at"], "2026-04-13T15:20:53.123000+00:00")

    def test_process_manual_state_accepts_naive_database_datetimes(self):
        app = Flask(__name__)
        now_naive = datetime(2026, 4, 13, 15, 20, 53)
        state = DeviceManualState(
            device_id=2,
            queue_status="running",
            desired_version=0,
            applied_version=0,
            lease_owner="worker-1",
        )
        state.watch_expires_at = now_naive + timedelta(seconds=10)
        state.next_poll_at = now_naive + timedelta(seconds=5)
        device = Device(
            device_id=2,
            asset_serial="IKA-2",
            display_name="IKA Stirrer",
            device_type="actuator",
            protocol="ika_eurostar_60",
        )
        fake_session = _FakeSessionForProcess(state=state, device=device)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(
                device_manual_runtime,
                "_read_ika_status",
                return_value={"setpoint_rpm": 250.0, "actual_rpm": 248.0, "torque_ncm": 1.1},
            ):
                device_manual_runtime._process_manual_state(app, device_id=2, worker_id="worker-1")

        self.assertEqual(state.queue_status, "idle")
        self.assertIsNone(state.lease_owner)
        self.assertIsNone(state.lease_expires_at)
        self.assertEqual(fake_session.commit_calls, 2)

    def test_claim_next_device_id_retries_after_mysql_record_changed(self):
        app = Flask(__name__)
        original = Exception(1020, "Record has changed since last read in table 'device_manual_state'")
        conflict_error = OperationalError("UPDATE device_manual_state ...", {}, original)
        fake_session = _FakeSessionForClaim(
            candidates=[(2,), (3,)],
            claim_side_effects=[conflict_error, 1],
        )

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            claimed = device_manual_runtime._claim_next_device_id(app, "worker-1")

        self.assertEqual(claimed, 3)
        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 1)

    def test_transient_db_retry_handles_mysql_record_changed(self):
        original = Exception(1020, "Record has changed since last read in table 'device_manual_state'")
        conflict_error = OperationalError("UPDATE device_manual_state ...", {}, original)
        fake_session = _FakeSessionForRetry()
        calls = {"count": 0}

        def operation():
            calls["count"] += 1
            if calls["count"] == 1:
                raise conflict_error
            return "ok"

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime.time, "sleep"):
                result = device_manual_runtime._run_with_transient_db_retry(operation)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        self.assertEqual(fake_session.rollback_calls, 1)

    def test_ika_success_state_update_retries_mysql_record_changed(self):
        app = Flask(__name__)
        state = DeviceManualState(
            device_id=3,
            queue_status="running",
            desired_version=2,
            applied_version=1,
            desired_is_on=True,
            desired_speed=600,
            lease_owner="worker-1",
        )
        original = Exception(1020, "Record has changed since last read in table 'device_manual_state'")
        conflict_error = OperationalError("UPDATE device_manual_state ...", {}, original)
        fake_session = _FakeSessionForManualStateUpdate(
            state=state,
            update_side_effects=[conflict_error, 1],
        )
        measured_at = datetime.now(timezone.utc)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime.time, "sleep"):
                updated = device_manual_runtime._commit_ika_manual_state_success(
                    app,
                    device_id=3,
                    telemetry={"setpoint_rpm": 612.0, "actual_rpm": 608.77, "torque_ncm": 1.4},
                    measured_at=measured_at,
                    desired_pending=True,
                    processed_version=2,
                    watch_active=False,
                    bg_interval=timedelta(seconds=30),
                )

        self.assertIs(updated, state)
        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 1)
        self.assertEqual(fake_session.update_calls, 2)
        self.assertEqual(state.applied_version, 2)
        self.assertEqual(state.reported_setpoint_rpm, 612)
        self.assertAlmostEqual(state.actual_rpm, 608.77)
        self.assertEqual(state.queue_status, "idle")
        self.assertIsNone(state.lease_owner)
        self.assertIsNone(state.last_error)

    def test_manual_state_release_retries_mysql_record_changed(self):
        state = DeviceManualState(
            device_id=3,
            queue_status="running",
            desired_version=2,
            applied_version=1,
            lease_owner="worker-1",
        )
        original = Exception(1020, "Record has changed since last read in table 'device_manual_state'")
        conflict_error = OperationalError("UPDATE device_manual_state ...", {}, original)
        fake_session = _FakeSessionForManualStateUpdate(
            state=state,
            update_side_effects=[conflict_error, 1],
        )
        next_poll_at = datetime.now(timezone.utc) + timedelta(milliseconds=250)

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime.time, "sleep"):
                updated = device_manual_runtime._commit_manual_state_release(
                    3,
                    status="queued",
                    next_poll_at=next_poll_at,
                )

        self.assertIs(updated, state)
        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 1)
        self.assertEqual(fake_session.update_calls, 2)
        self.assertEqual(state.queue_status, "queued")
        self.assertEqual(state.next_poll_at, next_poll_at)
        self.assertIsNone(state.lease_owner)
        self.assertIsNone(state.lease_expires_at)

    def test_measurement_best_effort_rolls_back_failed_session_without_raising(self):
        app = Flask(__name__)
        device = Device(device_id=3, display_name="IKA", protocol="ika_eurostar_60")
        fake_session = _FakeSessionForMeasurementBestEffort()

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(
                device_manual_runtime,
                "_persist_ika_telemetry_as_measurements",
                side_effect=RuntimeError("measurement flush failed"),
            ):
                device_manual_runtime._persist_ika_telemetry_as_measurements_best_effort(
                    app,
                    device,
                    {"setpoint_rpm": 612.0, "actual_rpm": 608.77, "torque_ncm": 1.4},
                    datetime.now(timezone.utc),
                )

        self.assertEqual(fake_session.rollback_calls, 1)
        self.assertEqual(fake_session.commit_calls, 0)

    def test_manual_claim_sort_uses_port_number_when_no_recipe_priority_exists(self):
        rows = [
            (4, 0, 0, None, None, 2),
            (3, 0, 0, None, None, 1),
        ]

        ordered = sorted(
            rows,
            key=lambda row: device_manual_runtime._manual_claim_candidate_sort_key(
                row,
                active_recipe_priority_order={},
                active_recipe=False,
            ),
        )

        self.assertEqual([row[0] for row in ordered], [3, 4])

    def test_manual_claim_sort_uses_recipe_priority_before_port_number(self):
        rows = [
            (4, 0, 0, None, None, 2),
            (3, 0, 0, None, None, 1),
        ]

        ordered = sorted(
            rows,
            key=lambda row: device_manual_runtime._manual_claim_candidate_sort_key(
                row,
                active_recipe_priority_order={4: (1, 0), 3: (2, 1)},
                active_recipe=True,
            ),
        )

        self.assertEqual([row[0] for row in ordered], [4, 3])


class ParseIkaNumericResponseTests(unittest.TestCase):
    """_parse_ika_numeric_response edge-cases."""

    def _call(self, text):
        return device_manual_runtime._parse_ika_numeric_response(text)

    def test_valid_float_string(self):
        self.assertAlmostEqual(self._call("300.0"), 300.0)

    def test_zero(self):
        self.assertAlmostEqual(self._call("0.0"), 0.0)

    def test_none_input(self):
        self.assertIsNone(self._call(None))

    def test_empty_string(self):
        self.assertIsNone(self._call(""))

    def test_whitespace_only(self):
        self.assertIsNone(self._call("   "))

    def test_non_numeric_string(self):
        self.assertIsNone(self._call("ERR"))

    def test_float_with_whitespace(self):
        self.assertAlmostEqual(self._call(" 150.5 "), 150.5)

    def test_ika_channel_suffix_setpoint(self):
        # IKA EUROSTAR appends the channel number: "IN_SP_4" → "100.0 4"
        self.assertAlmostEqual(self._call("100.0 4"), 100.0)

    def test_ika_channel_suffix_pv(self):
        # "IN_PV_5" → "2.3 5"
        self.assertAlmostEqual(self._call("2.3 5"), 2.3)

    def test_ika_channel_suffix_zero(self):
        self.assertAlmostEqual(self._call("0.0 4"), 0.0)


class ReadIkaStatusAllNoneTests(unittest.TestCase):
    """_read_ika_status must raise RuntimeError when every channel returns None."""

    def test_raises_when_all_responses_are_empty(self):
        # Simulate a device that responds with empty strings to all IN_ queries.
        call_count = {"n": 0}

        def fake_run(device, cmd):
            call_count["n"] += 1
            return ""  # empty → _parse_ika_numeric_response returns None

        with patch.object(device_manual_runtime, "_run_logged_manual_command", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                device_manual_runtime._read_ika_status(object())

        self.assertIn("no valid data", str(ctx.exception))
        self.assertEqual(call_count["n"], 3)

    def test_does_not_raise_when_only_one_channel_is_none(self):
        # setpoint valid, actual/torque empty → should not raise.
        responses = iter(["300.0", "", ""])

        def fake_run(device, cmd):
            return next(responses)

        with patch.object(device_manual_runtime, "_run_logged_manual_command", fake_run):
            result = device_manual_runtime._read_ika_status(object())

        self.assertAlmostEqual(result["setpoint_rpm"], 300.0)
        self.assertIsNone(result["actual_rpm"])
        self.assertIsNone(result["torque_ncm"])


class ApplyDesiredIkaStateTests(unittest.TestCase):
    """_apply_desired_ika_state must verify setpoint acceptance after START."""

    def _make_state(self, *, is_on, speed):
        state = DeviceManualState(
            device_id=1,
            desired_is_on=is_on,
            desired_speed=speed,
        )
        return state

    def test_on_raises_when_setpoint_not_confirmed(self):
        """If IN_SP_4 returns None after START, raise RuntimeError."""
        sent = []

        def fake_run(device, cmd):
            sent.append(cmd)
            if cmd.startswith("IN_SP_4"):
                return ""  # device not responding
            return None  # write commands return None

        state = self._make_state(is_on=True, speed=300)
        with patch.object(device_manual_runtime, "_run_logged_manual_command", fake_run):
            with patch.object(device_manual_runtime.time, "sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    device_manual_runtime._apply_desired_ika_state(object(), state)

        self.assertIn("did not confirm setpoint", str(ctx.exception))
        # START_4 and OUT_SP_4 must have been sent before the check
        self.assertIn("START_4", sent)
        self.assertTrue(any("OUT_SP_4" in s for s in sent))

    def test_on_succeeds_when_setpoint_confirmed(self):
        """If IN_SP_4 returns a value, no exception is raised."""
        def fake_run(device, cmd):
            if cmd.startswith("IN_SP_4"):
                return "300.0"
            return None

        state = self._make_state(is_on=True, speed=300)
        with patch.object(device_manual_runtime, "_run_logged_manual_command", fake_run):
            with patch.object(device_manual_runtime.time, "sleep"):
                # Should not raise
                device_manual_runtime._apply_desired_ika_state(object(), state)

    def test_off_sends_stop_and_does_not_read_back(self):
        """STOP_4 path must not read IN_SP_4."""
        sent = []

        def fake_run(device, cmd):
            sent.append(cmd)
            return None

        state = self._make_state(is_on=False, speed=0)
        with patch.object(device_manual_runtime, "_run_logged_manual_command", fake_run):
            with patch.object(device_manual_runtime.time, "sleep"):
                device_manual_runtime._apply_desired_ika_state(object(), state)

        self.assertEqual(sent, ["STOP_4"])


class ProcessManualStateAppliedVersionTests(unittest.TestCase):
    """applied_version must NOT be incremented when telemetry is invalid after apply."""

    def _make_app(self):
        return Flask(__name__)

    def test_applied_version_not_set_when_setpoint_is_none_after_apply(self):
        """
        Simulate: desired ON v1, apply runs (raises RuntimeError because setpoint
        check fails).  applied_version must stay at 0 and last_error must be set.
        """
        app = self._make_app()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = DeviceManualState(
            device_id=1,
            queue_status="running",
            desired_version=1,
            applied_version=0,
            desired_is_on=True,
            desired_speed=300,
            lease_owner="worker-x",
        )
        state.watch_expires_at = now + timedelta(seconds=30)
        state.next_poll_at = now - timedelta(seconds=1)

        device = Device(
            device_id=1,
            asset_serial="IKA-1",
            display_name="IKA Stirrer",
            device_type="actuator",
            protocol="ika_eurostar_60",
        )
        fake_session = _FakeSessionForProcess(state=state, device=device)

        def fake_apply(dev, st):
            raise RuntimeError("IN_SP_4 returned empty – device still booting")

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "_apply_desired_ika_state", fake_apply):
                device_manual_runtime._process_manual_state(app, device_id=1, worker_id="worker-x")

        self.assertEqual(state.applied_version, 0, "applied_version must stay 0 on failure")
        self.assertIn("booting", state.last_error or "")
        self.assertEqual(state.queue_status, "error")

    def test_recipe_program_is_failed_when_active_recipe_device_command_fails(self):
        app = self._make_app()
        now = datetime.now(timezone.utc)
        state = DeviceManualState(
            device_id=1,
            queue_status="running",
            desired_version=2,
            applied_version=1,
            desired_is_on=True,
            desired_speed=300,
            lease_owner="worker-x",
        )
        state.watch_expires_at = now + timedelta(seconds=30)
        state.next_poll_at = now - timedelta(seconds=1)

        device = Device(
            device_id=1,
            asset_serial="IKA-1",
            display_name="IKA Stirrer",
            device_type="actuator",
            protocol="ika_eurostar_60",
        )
        program_state = RecipeProgramState(
            recipe_program_state_id=1,
            recipe_id=3,
            status="running",
            lease_owner="recipe-worker",
            stop_requested=False,
        )
        program_state.snapshot_json = {
            "bindings": [
                {
                    "actor": "Stirrer-01",
                    "device_id": 1,
                    "device_display_name": "IKA Stirrer",
                    "protocol": "ika_eurostar_60",
                }
            ]
        }
        fake_session = _FakeSessionForProcess(state=state, device=device, program_state=program_state)

        def fake_apply(_dev, _st):
            raise RuntimeError("Device command execution failed.")

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "_apply_desired_ika_state", fake_apply):
                device_manual_runtime._process_manual_state(app, device_id=1, worker_id="worker-x")

        self.assertEqual(program_state.status, "error")
        self.assertIsNone(program_state.lease_owner)
        self.assertIn("Stirrer-01", program_state.last_error or "")
        self.assertIn("IKA Stirrer", program_state.last_error or "")
        self.assertIn("Device command execution failed", program_state.last_error or "")
        self.assertEqual(state.applied_version, 2)
        self.assertEqual(state.queue_status, "error")

    def test_applied_version_set_when_telemetry_valid(self):
        """
        Simulate: desired ON v1, apply succeeds, telemetry is valid.
        applied_version must advance to 1.
        """
        app = self._make_app()
        now = datetime.now(timezone.utc)
        state = DeviceManualState(
            device_id=1,
            queue_status="running",
            desired_version=1,
            applied_version=0,
            desired_is_on=True,
            desired_speed=300,
            lease_owner="worker-x",
        )
        state.watch_expires_at = now + timedelta(seconds=30)
        state.next_poll_at = now - timedelta(seconds=1)

        device = Device(
            device_id=1,
            asset_serial="IKA-1",
            display_name="IKA Stirrer",
            device_type="actuator",
            protocol="ika_eurostar_60",
        )
        fake_session = _FakeSessionForProcess(state=state, device=device)

        def fake_apply(_dev, _st):
            pass  # success

        def fake_read_status(_dev):
            return {"setpoint_rpm": 300.0, "actual_rpm": 280.0, "torque_ncm": 1.2}

        with patch.object(device_manual_runtime, "db", SimpleNamespace(session=fake_session)):
            with patch.object(device_manual_runtime, "_apply_desired_ika_state", fake_apply):
                with patch.object(device_manual_runtime, "_read_ika_status", fake_read_status):
                    device_manual_runtime._process_manual_state(app, device_id=1, worker_id="worker-x")

        self.assertEqual(state.applied_version, 1)
        self.assertIsNone(state.last_error)
        self.assertEqual(state.queue_status, "idle")


if __name__ == "__main__":
    unittest.main()
