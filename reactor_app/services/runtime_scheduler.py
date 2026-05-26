from __future__ import annotations

import heapq
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .cancellation import CancellationToken, CommandExecutionInterrupted
from .command_model import CommandPriority, DeviceCommand
from .runtime_status import RuntimeStatus


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeCommandInterruptedError(CommandExecutionInterrupted):
    def __init__(self, message: str, *, command: DeviceCommand, status: str, location: str | None = None):
        super().__init__(message, status=status, reason=message, location=location)
        self.command = command


RuntimeCancellationToken = CancellationToken


@dataclass(slots=True)
class ScheduledRuntimeCommand:
    command: DeviceCommand
    execute: Callable[[], Any]
    acquire_lock: bool = True
    control_command_id: int | None = None
    status: str = RuntimeStatus.PENDING
    enqueued_at: datetime = field(default_factory=_now_utc)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker_id: str | None = None
    cancel_requested_at: datetime | None = None
    cancellation_token: CancellationToken = field(default_factory=CancellationToken, repr=False)
    sequence_no: int = 0
    result: Any | None = None
    error: BaseException | None = None
    future: Future[Any] = field(default_factory=Future, repr=False)

    @property
    def command_id(self) -> str:
        return self.command.command_id

    @property
    def device_id(self) -> int:
        return int(self.command.device_id)

    @property
    def priority(self) -> int:
        return int(self.command.priority)

    @property
    def source(self) -> str:
        return str(self.command.source or "")

    def queue_deadline_at(self) -> datetime | None:
        timeout_s = getattr(self.command, "queue_timeout_s", None)
        if timeout_s in (None, ""):
            return None
        return self.command.created_at + timedelta(seconds=max(0.0, float(timeout_s)))

    def execution_deadline_at(self) -> datetime | None:
        timeout_s = getattr(self.command, "execution_timeout_s", None)
        if self.started_at is None or timeout_s in (None, ""):
            return None
        return self.started_at + timedelta(seconds=max(0.0, float(timeout_s)))

    def total_deadline_at(self) -> datetime | None:
        total_deadline_at = getattr(self.command, "total_deadline_at", None)
        if total_deadline_at is None:
            return None
        if total_deadline_at.tzinfo is None or total_deadline_at.tzinfo.utcoffset(total_deadline_at) is None:
            return total_deadline_at.replace(tzinfo=timezone.utc)
        return total_deadline_at.astimezone(timezone.utc)

    def effective_execution_deadline(self) -> tuple[datetime, str, str] | None:
        candidates: list[tuple[datetime, str, str]] = []
        total_deadline_at = self.total_deadline_at()
        if total_deadline_at is not None:
            candidates.append(
                (
                    total_deadline_at,
                    RuntimeStatus.EXPIRED,
                    "Command exceeded its total deadline during execution.",
                )
            )
        execution_deadline_at = self.execution_deadline_at()
        if execution_deadline_at is not None:
            candidates.append(
                (
                    execution_deadline_at,
                    RuntimeStatus.TIMEOUT,
                    "Command exceeded its execution timeout during execution.",
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])


class RuntimeCommandQueue:
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        on_status_change: Callable[[ScheduledRuntimeCommand, str, dict[str, Any]], None] | None = None,
        on_cancel_requested: Callable[[ScheduledRuntimeCommand, dict[str, Any]], None] | None = None,
    ):
        self._logger = logger or logging.getLogger(__name__)
        self._on_status_change = on_status_change
        self._on_cancel_requested = on_cancel_requested
        self._condition = threading.Condition()
        self._heap: list[tuple[int, int, str]] = []
        self._entries: dict[str, ScheduledRuntimeCommand] = {}
        self._active_by_device: dict[int, str] = {}
        self._sequence = 0

    def enqueue(self, command: ScheduledRuntimeCommand) -> ScheduledRuntimeCommand:
        notifications: list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]] = []
        with self._condition:
            if command.command_id in self._entries:
                raise ValueError(f"Command {command.command_id} is already queued.")

            command.status = RuntimeStatus.PENDING
            command.enqueued_at = _now_utc()
            command.sequence_no = self._sequence
            self._sequence += 1

            notifications.extend(self._preempt_pending_locked(command))
            expired = self._interrupt_if_pending_deadline_exceeded_locked(command, command.enqueued_at)
            if expired is not None:
                notifications.append(expired)
                self._condition.notify_all()
            else:
                self._entries[command.command_id] = command
                heapq.heappush(self._heap, (command.priority, command.sequence_no, command.command_id))
                self._condition.notify_all()
                notifications.append(
                    (
                        command,
                        RuntimeStatus.PENDING,
                        {"enqueued_at": command.enqueued_at.isoformat()},
                    )
                )
        self._emit_status_notifications(notifications)
        return command

    def dequeue_next(
        self,
        device_id: int | None = None,
        *,
        timeout_s: float | None = None,
    ) -> ScheduledRuntimeCommand | None:
        normalized_device_id = None if device_id is None else int(device_id)
        deadline = None if timeout_s is None else (time.monotonic() + max(0.0, float(timeout_s)))

        with self._condition:
            while True:
                deferred: list[tuple[int, int, str]] = []
                selected: ScheduledRuntimeCommand | None = None
                notifications: list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]] = []

                while self._heap:
                    priority, sequence_no, command_id = heapq.heappop(self._heap)
                    item = self._entries.get(command_id)
                    if item is None or item.status != RuntimeStatus.PENDING:
                        continue
                    expired = self._interrupt_if_pending_deadline_exceeded_locked(item, _now_utc())
                    if expired is not None:
                        notifications.append(expired)
                        continue
                    if normalized_device_id is not None and item.device_id != normalized_device_id:
                        deferred.append((priority, sequence_no, command_id))
                        continue
                    if item.device_id in self._active_by_device:
                        deferred.append((priority, sequence_no, command_id))
                        continue
                    selected = item
                    break

                for entry in deferred:
                    heapq.heappush(self._heap, entry)

                if notifications:
                    self._condition.release()
                    try:
                        self._emit_status_notifications(notifications)
                    finally:
                        self._condition.acquire()

                if selected is not None:
                    return selected

                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(timeout=remaining)
                else:
                    self._condition.wait()

    def mark_running(self, command_id: str, *, worker_id: str | None = None) -> ScheduledRuntimeCommand:
        notification: tuple[ScheduledRuntimeCommand, str, dict[str, Any]] | None = None
        with self._condition:
            item = self._require_entry(command_id)
            if item.status != RuntimeStatus.PENDING:
                raise RuntimeError(
                    f"Command {command_id} cannot enter running from status {item.status}."
                )
            current_active = self._active_by_device.get(item.device_id)
            if current_active is not None and current_active != command_id:
                raise RuntimeError(
                    f"Device {item.device_id} is already running command {current_active}."
                )
            item.status = RuntimeStatus.RUNNING
            item.started_at = _now_utc()
            item.worker_id = str(worker_id).strip() or None if worker_id is not None else None
            effective_deadline = item.effective_execution_deadline()
            if effective_deadline is None:
                item.cancellation_token.clear_deadline()
            else:
                deadline_at, deadline_status, deadline_message = effective_deadline
                item.cancellation_token.set_deadline(
                    deadline_at,
                    status=deadline_status,
                    reason=deadline_message,
                    source="runtime_worker",
                )
            self._active_by_device[item.device_id] = command_id
            self._condition.notify_all()
            notification = (
                item,
                RuntimeStatus.RUNNING,
                {
                    "started_at": item.started_at.isoformat(),
                    "worker_id": item.worker_id,
                },
            )
        if notification is not None:
            self._emit_status_notifications([notification])
        return item

    def mark_completed(self, command_id: str, result: Any) -> ScheduledRuntimeCommand:
        notification: tuple[ScheduledRuntimeCommand, str, dict[str, Any]] | None = None
        with self._condition:
            item = self._require_entry(command_id)
            item.status = RuntimeStatus.COMPLETED
            item.result = result
            item.error = None
            item.finished_at = _now_utc()
            self._active_by_device.pop(item.device_id, None)
            self._entries.pop(command_id, None)
            if not item.future.done():
                item.future.set_result(result)
            self._condition.notify_all()
            notification = (
                item,
                RuntimeStatus.COMPLETED,
                {"finished_at": item.finished_at.isoformat()},
            )
        if notification is not None:
            self._emit_status_notifications([notification])
        return item

    def mark_failed(
        self,
        command_id: str,
        error: BaseException,
        *,
        status: str = RuntimeStatus.FAILED,
    ) -> ScheduledRuntimeCommand:
        notification: tuple[ScheduledRuntimeCommand, str, dict[str, Any]] | None = None
        with self._condition:
            item = self._require_entry(command_id)
            item.status = status
            item.error = error
            item.finished_at = _now_utc()
            self._active_by_device.pop(item.device_id, None)
            self._entries.pop(command_id, None)
            if not item.future.done():
                item.future.set_exception(error)
            self._condition.notify_all()
            notification = (
                item,
                status,
                self._error_payload(item.finished_at, error),
            )
        if notification is not None:
            self._emit_status_notifications([notification])
        return item

    def cancel_pending(
        self,
        device_id: int | None = None,
        source: str | None = None,
        *,
        priority_gt: int | None = None,
        status: str = RuntimeStatus.CANCELLED,
        reason: str | None = None,
    ) -> list[ScheduledRuntimeCommand]:
        normalized_device_id = None if device_id is None else int(device_id)
        normalized_source = None if source is None else str(source).strip().lower()

        notifications: list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]] = []
        with self._condition:
            cancelled, notifications = self._cancel_locked(
                lambda item: (
                    item.status == RuntimeStatus.PENDING
                    and (normalized_device_id is None or item.device_id == normalized_device_id)
                    and (normalized_source is None or item.source.strip().lower() == normalized_source)
                    and (priority_gt is None or item.priority > int(priority_gt))
                ),
                status=status,
                reason=reason,
            )
        self._emit_status_notifications(notifications)
        return cancelled

    def clear_device_queue(self, device_id: int) -> list[ScheduledRuntimeCommand]:
        return self.cancel_pending(device_id=device_id)

    def request_cancellation(
        self,
        device_id: int | None = None,
        source: str | None = None,
        *,
        priority_gt: int | None = None,
        reason: str | None = None,
    ) -> list[ScheduledRuntimeCommand]:
        normalized_device_id = None if device_id is None else int(device_id)
        normalized_source = None if source is None else str(source).strip().lower()
        notifications: list[tuple[ScheduledRuntimeCommand, dict[str, Any]]] = []
        with self._condition:
            for item in list(self._entries.values()):
                if item.status != RuntimeStatus.RUNNING:
                    continue
                if normalized_device_id is not None and item.device_id != normalized_device_id:
                    continue
                if normalized_source is not None and item.source.strip().lower() != normalized_source:
                    continue
                if priority_gt is not None and item.priority <= int(priority_gt):
                    continue
                if item.cancellation_token.is_cancelled():
                    continue
                item.cancel_requested_at = _now_utc()
                item.cancellation_token.cancel(reason)
                notifications.append(
                    (
                        item,
                        {
                            "cancel_requested_at": item.cancel_requested_at.isoformat(),
                            "reason": reason,
                        },
                    )
                )
            if notifications:
                self._condition.notify_all()
        self._emit_cancel_requested(notifications)
        return [item for item, _payload in notifications]

    def wake_all(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _require_entry(self, command_id: str) -> ScheduledRuntimeCommand:
        item = self._entries.get(command_id)
        if item is None:
            raise KeyError(f"Unknown command {command_id}.")
        return item

    def _preempt_pending_locked(
        self,
        command: ScheduledRuntimeCommand,
    ) -> list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]]:
        if command.priority <= int(CommandPriority.SAFETY):
            _cancelled, notifications = self._cancel_locked(
                lambda item: (
                    item.status == RuntimeStatus.PENDING
                    and item.device_id == command.device_id
                    and item.command_id != command.command_id
                    and item.priority > command.priority
                ),
                status=RuntimeStatus.PREEMPTED,
                reason=(
                    f"Command was preempted by high-priority "
                    f"{command.command.command_type}."
                ),
            )
            return notifications

        if command.priority == int(CommandPriority.POLLING):
            _cancelled, notifications = self._cancel_locked(
                lambda item: (
                    item.status == RuntimeStatus.PENDING
                    and item.device_id == command.device_id
                    and item.command_id != command.command_id
                    and item.priority == int(CommandPriority.POLLING)
                ),
                status=RuntimeStatus.SKIPPED,
                reason="Polling command was superseded by a newer poll.",
            )
            return notifications

        if command.priority < int(CommandPriority.POLLING):
            _cancelled, notifications = self._cancel_locked(
                lambda item: (
                    item.status == RuntimeStatus.PENDING
                    and item.device_id == command.device_id
                    and item.priority == int(CommandPriority.POLLING)
                ),
                status=RuntimeStatus.SKIPPED,
                reason=(
                    f"Polling command was skipped because "
                    f"{command.command.command_type} is waiting."
                ),
            )
            return notifications
        return []

    def _cancel_locked(
        self,
        predicate: Callable[[ScheduledRuntimeCommand], bool],
        *,
        status: str,
        reason: str | None,
    ) -> tuple[list[ScheduledRuntimeCommand], list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]]]:
        now = _now_utc()
        cancelled: list[ScheduledRuntimeCommand] = []
        notifications: list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]] = []

        for item in list(self._entries.values()):
            if not predicate(item):
                continue
            message = reason or f"Command was {status} before execution."
            interrupted = RuntimeCommandInterruptedError(
                message,
                command=item.command,
                status=status,
            )
            item.status = status
            item.error = interrupted
            item.finished_at = now
            self._entries.pop(item.command_id, None)
            if not item.future.done():
                item.future.set_exception(interrupted)
            cancelled.append(item)
            notifications.append(
                (
                    item,
                    status,
                    {
                        "finished_at": now.isoformat(),
                        "message": message,
                    },
                )
            )

        if cancelled:
            self._condition.notify_all()
        return cancelled, notifications

    def _interrupt_if_pending_deadline_exceeded_locked(
        self,
        item: ScheduledRuntimeCommand,
        now: datetime,
    ) -> tuple[ScheduledRuntimeCommand, str, dict[str, Any]] | None:
        total_deadline_at = item.total_deadline_at()
        if total_deadline_at is not None and total_deadline_at <= now:
            return self._interrupt_item_locked(
                item,
                status=RuntimeStatus.EXPIRED,
                message="Command exceeded its total deadline before execution.",
                now=now,
            )

        queue_deadline_at = item.queue_deadline_at()
        if queue_deadline_at is not None and queue_deadline_at <= now:
            return self._interrupt_item_locked(
                item,
                status=RuntimeStatus.TIMEOUT,
                message="Command exceeded its queue timeout before execution.",
                now=now,
            )
        return None

    def _interrupt_item_locked(
        self,
        item: ScheduledRuntimeCommand,
        *,
        status: str,
        message: str,
        now: datetime,
    ) -> tuple[ScheduledRuntimeCommand, str, dict[str, Any]]:
        interrupted = RuntimeCommandInterruptedError(message, command=item.command, status=status)
        item.status = status
        item.error = interrupted
        item.finished_at = now
        self._entries.pop(item.command_id, None)
        if not item.future.done():
            item.future.set_exception(interrupted)
        return (
            item,
            status,
            {
                "finished_at": now.isoformat(),
                "message": message,
            },
        )

    def _error_payload(self, finished_at: datetime, error: BaseException) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "finished_at": finished_at.isoformat(),
            "message": str(error),
        }
        location = str(getattr(error, "location", "") or "").strip()
        if location:
            payload["location"] = location
        reason = str(getattr(error, "reason", "") or "").strip()
        if reason and reason != payload["message"]:
            payload["reason"] = reason
        return payload

    def _emit_status_notifications(
        self,
        notifications: list[tuple[ScheduledRuntimeCommand, str, dict[str, Any]]],
    ) -> None:
        if self._on_status_change is None:
            return
        for item, status, payload in notifications:
            try:
                self._on_status_change(item, status, payload)
            except Exception:
                self._logger.warning(
                    "Runtime queue status callback failed for command_id=%s status=%s.",
                    item.command_id,
                    status,
                    exc_info=True,
                )

    def _emit_cancel_requested(
        self,
        notifications: list[tuple[ScheduledRuntimeCommand, dict[str, Any]]],
    ) -> None:
        if self._on_cancel_requested is None:
            return
        for item, payload in notifications:
            try:
                self._on_cancel_requested(item, payload)
            except Exception:
                self._logger.warning(
                    "Runtime queue cancel callback failed for command_id=%s.",
                    item.command_id,
                    exc_info=True,
                )


class RuntimeWorker:
    def __init__(
        self,
        queue: RuntimeCommandQueue,
        *,
        logger: logging.Logger | None = None,
        name: str | None = None,
        idle_wait_s: float = 0.1,
    ):
        self._queue = queue
        self._logger = logger or logging.getLogger(__name__)
        self._name = name or "runtime-worker"
        self._idle_wait_s = max(0.01, float(idle_wait_s))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def worker_id(self) -> str:
        return self._name

    def start(self) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.process_loop,
            name=self._name,
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_s: float | None = 5.0) -> None:
        self._stop_event.set()
        self._queue.wake_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def process_next(self, *, timeout_s: float = 0.1) -> bool:
        if self._stop_event.is_set():
            return False

        item = self._queue.dequeue_next(timeout_s=timeout_s)
        if item is None:
            return False

        self._queue.mark_running(item.command_id, worker_id=self.worker_id)
        preflight_error = self._preflight_interrupt(item)
        if preflight_error is not None:
            self._queue.mark_failed(item.command_id, preflight_error, status=preflight_error.status)
            return True
        try:
            result = item.execute()
        except RuntimeCommandInterruptedError as exc:
            self._queue.mark_failed(item.command_id, exc, status=exc.status)
        except CommandExecutionInterrupted as exc:
            interrupted = RuntimeCommandInterruptedError(
                str(exc),
                command=item.command,
                status=exc.status,
                location=exc.location,
            )
            self._queue.mark_failed(item.command_id, interrupted, status=interrupted.status)
        except Exception as exc:
            self._queue.mark_failed(item.command_id, exc, status=self._error_status(exc))
            self._logger.warning(
                "Runtime worker %s failed command_id=%s device_id=%s type=%s",
                self._name,
                item.command_id,
                item.device_id,
                item.command.command_type,
                exc_info=True,
            )
        else:
            self._queue.mark_completed(item.command_id, result)
        return True

    def _preflight_interrupt(self, item: ScheduledRuntimeCommand) -> RuntimeCommandInterruptedError | None:
        try:
            item.cancellation_token.throw_if_interrupted(location="runtime_worker.preflight", now=_now_utc())
        except CommandExecutionInterrupted as exc:
            return RuntimeCommandInterruptedError(
                str(exc),
                command=item.command,
                status=exc.status,
                location=exc.location,
            )

        now = _now_utc()
        total_deadline_at = item.total_deadline_at()
        if total_deadline_at is not None and total_deadline_at <= now:
            return RuntimeCommandInterruptedError(
                "Command exceeded its total deadline before execution started.",
                command=item.command,
                status=RuntimeStatus.EXPIRED,
                location="runtime_worker.preflight",
            )

        execution_deadline_at = item.execution_deadline_at()
        if execution_deadline_at is not None and execution_deadline_at <= now:
            return RuntimeCommandInterruptedError(
                "Command exceeded its execution timeout before execution started.",
                command=item.command,
                status=RuntimeStatus.TIMEOUT,
                location="runtime_worker.preflight",
            )
        return None

    def _error_status(self, error: BaseException) -> str:
        status = str(getattr(error, "status", "") or "").strip().lower()
        if status in RuntimeStatus.TERMINAL or status in RuntimeStatus.ERROR_STATES:
            return status
        command = getattr(error, "command", None)
        command_status = str(getattr(command, "status", "") or "").strip().lower()
        if command_status in RuntimeStatus.TERMINAL or command_status in RuntimeStatus.ERROR_STATES:
            return command_status
        details = getattr(error, "details", None)
        if isinstance(details, dict):
            runtime_status = str(details.get("runtime_status") or "").strip().lower()
            if runtime_status in RuntimeStatus.TERMINAL or runtime_status in RuntimeStatus.ERROR_STATES:
                return runtime_status
        return RuntimeStatus.FAILED

    def process_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_next(timeout_s=self._idle_wait_s)
            except Exception:
                self._logger.exception("Runtime worker %s loop crashed.", self._name)
                time.sleep(self._idle_wait_s)


class RuntimeCommandScheduler:
    def __init__(
        self,
        *,
        worker_count: int = 2,
        queue: RuntimeCommandQueue | None = None,
        logger: logging.Logger | None = None,
        worker_name_prefix: str = "runtime-worker",
        idle_wait_s: float = 0.1,
    ):
        self._logger = logger or logging.getLogger(__name__)
        self._queue = queue or RuntimeCommandQueue(logger=self._logger)
        self._worker_count = max(1, int(worker_count))
        self._worker_name_prefix = worker_name_prefix
        self._idle_wait_s = max(0.01, float(idle_wait_s))
        self._workers: list[RuntimeWorker] = []
        self._guard = threading.Lock()

    @property
    def queue(self) -> RuntimeCommandQueue:
        return self._queue

    def start(self) -> None:
        with self._guard:
            if any(worker.is_running() for worker in self._workers):
                return
            self._workers = [
                RuntimeWorker(
                    self._queue,
                    logger=self._logger,
                    name=f"{self._worker_name_prefix}-{index + 1}",
                    idle_wait_s=self._idle_wait_s,
                )
                for index in range(self._worker_count)
            ]
            for worker in self._workers:
                worker.start()

    def stop(self, *, cancel_pending: bool = True, timeout_s: float | None = 5.0) -> None:
        with self._guard:
            workers = list(self._workers)
            self._workers = []

        if cancel_pending:
            self._queue.cancel_pending(
                status=RuntimeStatus.CANCELLED,
                reason="Runtime scheduler stopped before command execution.",
            )
            self._queue.request_cancellation(
                reason="Runtime scheduler stopped while command execution was in progress.",
            )
        self._queue.wake_all()

        for worker in workers:
            worker.stop(timeout_s=timeout_s)

    def is_running(self) -> bool:
        with self._guard:
            return any(worker.is_running() for worker in self._workers)

    def submit(
        self,
        command: ScheduledRuntimeCommand,
        *,
        wait: bool = True,
        timeout_s: float | None = None,
    ) -> Any:
        self.start()
        queued = self._queue.enqueue(command)
        if not wait:
            return queued.future
        return queued.future.result(timeout=timeout_s)

    def cancel_pending(
        self,
        device_id: int | None = None,
        source: str | None = None,
        *,
        priority_gt: int | None = None,
        status: str = RuntimeStatus.CANCELLED,
        reason: str | None = None,
    ) -> list[ScheduledRuntimeCommand]:
        return self._queue.cancel_pending(
            device_id=device_id,
            source=source,
            priority_gt=priority_gt,
            status=status,
            reason=reason,
        )

    def clear_device_queue(self, device_id: int) -> list[ScheduledRuntimeCommand]:
        return self._queue.clear_device_queue(device_id)

    def request_cancellation(
        self,
        device_id: int | None = None,
        source: str | None = None,
        *,
        priority_gt: int | None = None,
        reason: str | None = None,
    ) -> list[ScheduledRuntimeCommand]:
        return self._queue.request_cancellation(
            device_id=device_id,
            source=source,
            priority_gt=priority_gt,
            reason=reason,
        )
