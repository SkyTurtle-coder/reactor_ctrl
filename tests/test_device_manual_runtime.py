import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy.exc import OperationalError

from reactor_app.models import Device, DeviceManualState
from reactor_app.services import device_manual_runtime


class _FakeSessionForProcess:
    def __init__(self, *, state, device):
        self._state = state
        self._device = device
        self.commit_calls = 0

    def get(self, model, _device_id):
        if model is DeviceManualState:
            return self._state
        if model is Device:
            return self._device
        raise AssertionError(f"Unexpected model lookup: {model}")

    def commit(self):
        self.commit_calls += 1


class _FakeSessionForClaim:
    def __init__(self, *, candidates, claim_side_effects):
        self._query_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

        self._candidates_query = MagicMock()
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
            device_manual_runtime._process_manual_state(app, device_id=2, worker_id="worker-1")

        self.assertEqual(state.queue_status, "idle")
        self.assertIsNone(state.lease_owner)
        self.assertIsNone(state.lease_expires_at)
        self.assertEqual(fake_session.commit_calls, 1)

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


if __name__ == "__main__":
    unittest.main()
