from __future__ import annotations

import threading
from datetime import datetime, timezone

from .runtime_status import RuntimeStatus


def as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CommandExecutionInterrupted(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: str,
        reason: str | None = None,
        location: str | None = None,
    ):
        super().__init__(message)
        self.status = str(status or RuntimeStatus.CANCELLED).strip().lower() or RuntimeStatus.CANCELLED
        self.reason = str(reason).strip() if reason not in (None, "") else None
        self.location = str(location).strip() if location not in (None, "") else None


class CancellationToken:
    def __init__(
        self,
        *,
        deadline: datetime | None = None,
        deadline_status: str = RuntimeStatus.TIMEOUT,
        deadline_reason: str | None = None,
        deadline_source: str | None = None,
    ) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._deadline = as_utc_datetime(deadline)
        self._deadline_status = str(deadline_status or RuntimeStatus.TIMEOUT).strip().lower() or RuntimeStatus.TIMEOUT
        self._deadline_reason = str(deadline_reason).strip() if deadline_reason not in (None, "") else None
        self._deadline_source = str(deadline_source).strip() if deadline_source not in (None, "") else None

    def cancel(self, reason: str | None = None) -> None:
        with self._lock:
            self._reason = str(reason).strip() if reason not in (None, "") else None
            self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def deadline(self) -> datetime | None:
        with self._lock:
            return self._deadline

    @property
    def deadline_status(self) -> str:
        with self._lock:
            return self._deadline_status

    @property
    def deadline_source(self) -> str | None:
        with self._lock:
            return self._deadline_source

    def set_deadline(
        self,
        deadline: datetime | None,
        *,
        status: str = RuntimeStatus.TIMEOUT,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        with self._lock:
            self._deadline = as_utc_datetime(deadline)
            self._deadline_status = str(status or RuntimeStatus.TIMEOUT).strip().lower() or RuntimeStatus.TIMEOUT
            self._deadline_reason = str(reason).strip() if reason not in (None, "") else None
            self._deadline_source = str(source).strip() if source not in (None, "") else None

    def clear_deadline(self) -> None:
        with self._lock:
            self._deadline = None
            self._deadline_status = RuntimeStatus.TIMEOUT
            self._deadline_reason = None
            self._deadline_source = None

    def time_remaining(self, *, now: datetime | None = None) -> float | None:
        deadline = self.deadline
        if deadline is None:
            return None
        current_time = as_utc_datetime(now) or datetime.now(timezone.utc)
        return max(0.0, (deadline - current_time).total_seconds())

    def is_expired(self, *, now: datetime | None = None) -> bool:
        remaining = self.time_remaining(now=now)
        return remaining is not None and remaining <= 0.0

    def throw_if_cancelled(self, *, location: str | None = None) -> None:
        if not self.is_cancelled():
            return
        message = self.reason or "Command execution was cancelled."
        raise CommandExecutionInterrupted(
            message,
            status=RuntimeStatus.CANCELLED,
            reason=self.reason,
            location=location,
        )

    def throw_if_expired(self, *, location: str | None = None, now: datetime | None = None) -> None:
        if not self.is_expired(now=now):
            return
        with self._lock:
            status = self._deadline_status
            reason = self._deadline_reason
            source = self._deadline_source
        message = reason or "Command deadline expired during execution."
        if source:
            message = f"{message} ({source})"
        raise CommandExecutionInterrupted(
            message,
            status=status,
            reason=reason,
            location=location or source,
        )

    def throw_if_interrupted(self, *, location: str | None = None, now: datetime | None = None) -> None:
        self.throw_if_cancelled(location=location)
        self.throw_if_expired(location=location, now=now)
