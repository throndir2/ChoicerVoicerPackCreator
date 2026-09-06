"""Application-owned bounded jobs. Construct and control the manager on its Qt thread."""

from __future__ import annotations

import math
import time
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QThread, QTimer, Signal, Slot

from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    cancellation_deferred,
    critical_stage,
    freeze_metadata,
    lease_requests,
    leases,
    operation_scope,
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "blocked"})
_ALL_PROJECTS = object()


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    project_id: str | None
    kind: str
    title: str
    resource_class: str
    source_snapshot: Mapping[str, Any]
    priority: int = 0
    state: str = "queued"
    message: str = "Queued"
    fraction: float | None = None
    created_at: float = 0
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    cancel_requested: bool = False
    detail: Any = None

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL_STATES


class JobHandle(QObject):
    progress = Signal(str, object)
    detail = Signal(object)
    state_changed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, manager: JobManager, record: JobRecord) -> None:
        super().__init__(manager)
        self._manager = weakref.ref(manager)
        self._record = record
        self._cancel = Event()

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def record(self) -> JobRecord:
        return self._record

    def cancel(self) -> None:
        manager = self._manager()
        if manager is None:
            raise RuntimeError("The job manager has been destroyed")
        manager.cancel(self.id)


class JobContext:
    def __init__(self, handle: JobHandle, manager: JobManager) -> None:
        self.job_id = handle.id
        self.project_id = handle.record.project_id
        self.source_snapshot = handle.record.source_snapshot
        self._cancel = handle._cancel
        self._manager = manager
        self._committed = False

    def cancelled(self) -> bool:
        return self._cancel.is_set() and not cancellation_deferred()

    def check_cancelled(self) -> None:
        from choicer_voicer_pack_creator.operations import check_cancelled

        check_cancelled()

    def report(self, message: str, fraction: float | None = None, *, detail: Any = None) -> None:
        if fraction is not None and (not math.isfinite(fraction) or not 0 <= fraction <= 1):
            raise ValueError("Progress fraction must be finite and between 0 and 1")
        if detail is not None:
            # Rich events can establish a plan or close an estimator stage: unlike
            # plain text updates they must reach consumers in their original order.
            self._manager._events.emit((self.job_id, "detail", (message, fraction, detail)))
        else:
            self._manager._post_progress(self.job_id, (message, fraction, None))

    @contextmanager
    def critical_stage(self, message: str):
        with critical_stage(message):
            yield

    def _mark_committed(self) -> None:
        self._committed = True


@dataclass(slots=True)
class _Task:
    handle: JobHandle
    operation: Callable[[JobContext], Any] | None
    requests: tuple
    dependencies: tuple[str, ...]
    lease: str | None = None
    admission: Event = field(default_factory=Event)
    rejected: bool = False


class JobManager(QObject):
    """Keep this manager alive until active_jobs() is empty before destroying Qt.

    Before destroying its owner, call shutdown(wait=True) to join the now-idle
    executor and drain queued completions. Handles weakly reference their manager
    so Python cyclic GC cannot retire Qt objects from a later worker thread.

    Limits bound simultaneous operations, not threads in external tools. The default
    single CPU job reserves room for playback; I/O and network work can overlap it.
    All public methods and signals run on the owning Qt thread. Worker events use an
    explicit queued bridge; widgets never own the execution lifetime.
    """

    changed = Signal(object)
    _events = Signal(object)
    _dispatch = Signal()

    def __init__(
        self, parent: QObject | None = None, *, limits: Mapping[str, int] | None = None,
    ) -> None:
        if QCoreApplication.instance() is None:
            raise RuntimeError("JobManager requires a running QtCore application/event loop")
        super().__init__(parent)
        self.limits = dict(limits if limits is not None else {"cpu": 1, "io": 2, "network": 2})
        if not self.limits or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in self.limits.values()
        ):
            raise ValueError("Each resource budget must be a positive integer")
        self._executor = ThreadPoolExecutor(
            max_workers=sum(self.limits.values()), thread_name_prefix="pack-job",
        )
        self._tasks: dict[str, _Task] = {}
        self._running = dict.fromkeys(self.limits, 0)
        self._closed = False
        self._progress_lock = Lock()
        self._pending_progress: dict[str, tuple] = {}
        self._events.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._dispatch.connect(self._schedule, Qt.ConnectionType.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._schedule)

    def _assert_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("JobManager must be controlled from its owning Qt thread")

    def submit(
        self,
        project_id: str | None,
        kind: str,
        title: str,
        operation: Callable[[JobContext], Any],
        *,
        resource_class: str = "cpu",
        resource_keys: Iterable[str] = (),
        read_paths: Iterable[str | Path] = (),
        write_paths: Iterable[str | Path] = (),
        source_snapshot: Mapping[str, Any] | None = None,
        depends_on: Iterable[str | JobHandle] = (),
        priority: int = 0,
    ) -> JobHandle:
        self._assert_thread()
        if self._closed:
            raise RuntimeError("Job manager is shutting down")
        if resource_class not in self.limits:
            raise ValueError(f"Unknown resource class: {resource_class}")
        if not callable(operation):
            raise TypeError("Job operation must be callable")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("Job priority must be an integer")
        dependencies = tuple(item.id if isinstance(item, JobHandle) else item for item in depends_on)
        if any(item not in self._tasks for item in dependencies):
            raise ValueError("Job dependencies must already belong to this manager")
        record = JobRecord(
            uuid.uuid4().hex, project_id, kind, title, resource_class,
            freeze_metadata(source_snapshot or {}), priority=priority, created_at=time.time(),
        )
        handle = JobHandle(self, record)
        self._tasks[record.id] = _Task(
            handle, operation, lease_requests(read_paths, write_paths, resource_keys), dependencies,
        )
        self.changed.emit(record)
        self._timer.start()
        self._dispatch.emit()
        return handle

    def tasks(self, project_id: str | None | object = _ALL_PROJECTS) -> tuple[JobRecord, ...]:
        self._assert_thread()
        return tuple(
            task.handle.record for task in self._tasks.values()
            if project_id is _ALL_PROJECTS or task.handle.record.project_id == project_id
        )

    def active_jobs(self, project_id: str | None | object = _ALL_PROJECTS) -> tuple[JobRecord, ...]:
        return tuple(record for record in self.tasks(project_id) if record.active)

    def handle(self, job_id: str) -> JobHandle:
        self._assert_thread()
        return self._tasks[job_id].handle

    def _update(self, handle: JobHandle, **changes: Any) -> None:
        previous = handle.record
        record = handle._record = replace(previous, **changes)
        if record.state != previous.state:
            handle.state_changed.emit(record.state)
        self.changed.emit(handle.record)

    def cancel(self, job_id: str) -> None:
        self._assert_thread()
        handle = self._tasks[job_id].handle
        if not handle.record.active:
            return
        handle._cancel.set()
        if handle.record.state in {"queued", "waiting"}:
            self._update(handle, cancel_requested=True)
            self._finish(handle, "cancelled", None, None)
        else:
            self._update(
                handle, state="cancelling", cancel_requested=True,
                message="Cancellation requested; waiting for safe cleanup or publication",
                fraction=None,
            )
        self._schedule()

    @Slot()
    def _schedule(self) -> None:
        self._assert_thread()
        if self._closed:
            self._timer.stop()
            return
        # Stable ordering keeps equal-priority work FIFO. Running jobs are never preempted.
        for task in sorted(self._tasks.values(), key=lambda task: -task.handle.record.priority):
            handle = task.handle
            if handle.record.state not in {"queued", "waiting"}:
                continue
            dependencies = [self._tasks[item].handle.record for item in task.dependencies]
            if any(item.state in TERMINAL_STATES - {"succeeded"} for item in dependencies):
                self._finish(handle, "blocked", None, "A required task did not succeed")
                continue
            if any(item.state != "succeeded" for item in dependencies):
                self._waiting(handle, "Waiting for required tasks")
                continue
            resource = handle.record.resource_class
            if self._running[resource] >= self.limits[resource]:
                self._waiting(handle, f"Waiting for {resource} capacity")
                continue
            token = leases.acquire(handle.id, task.requests)
            if token is None:
                self._waiting(handle, "Waiting for shared files or components")
                continue
            task.lease = token
            self._running[resource] += 1
            self._update(handle, state="running", message="Starting", started_at=time.time())
            # Direct Qt listeners may cancel or shut down the executor while the
            # starting state is being announced, before any worker was admitted.
            if self._closed or handle._cancel.is_set():
                self._reject_start(task, "cancelled", None)
                continue
            context = copy_context()
            try:
                self._executor.submit(
                    context.run, JobManager._run_queued, weakref.ref(self), handle.id,
                )
            except Exception as error:
                self._reject_start(task, "failed", f"{type(error).__name__}: {error}")
            else:
                task.admission.set()
        if not self.active_jobs():
            self._timer.stop()

    def _waiting(self, handle: JobHandle, message: str) -> None:
        if handle.record.state != "waiting" or handle.record.message != message:
            self._update(handle, state="waiting", message=message)

    def _reject_start(self, task: _Task, state: str, error: str | None) -> None:
        task.rejected = True
        task.admission.set()
        if task.lease is not None:
            leases.release(task.lease)
            task.lease = None
        self._running[task.handle.record.resource_class] -= 1
        self._finish(task.handle, state, None, error)

    @staticmethod
    def _run_queued(manager_ref: weakref.ReferenceType[JobManager], job_id: str) -> None:
        # A failed thread start can leave this item in an executor with no workers
        # to drain it. The queue must not own a bound method/Qt manager reference.
        manager = manager_ref()
        if manager is not None:
            manager._execute(manager._tasks[job_id])

    def _execute(self, task: _Task) -> None:
        # submit() can fail after putting work on the executor's internal queue.
        # Such a work item must never execute application code or release twice.
        task.admission.wait()
        if task.rejected:
            return
        context = JobContext(task.handle, self)
        state, result, error = "succeeded", None, None
        try:
            with operation_scope(
                cancelled=context._cancel.is_set, progress=context.report,
                owner=context.job_id, committed=context._mark_committed,
            ):
                assert task.operation is not None
                result = task.operation(context)
                if not context._committed:
                    context.check_cancelled()
        except OperationCancelled:
            state = "cancelled"
        except BaseException as failure:
            # Cleanup failures remain failures even after a cancellation request.
            state, error = "failed", f"{type(failure).__name__}: {failure}"
        finally:
            # Services finish subprocess and staging cleanup before releasing leases.
            if task.lease is not None:
                leases.release(task.lease)
            self._events.emit((context.job_id, "finished", (state, result, error)))

    def _post_progress(self, job_id: str, value: tuple) -> None:
        with self._progress_lock:
            pending = job_id in self._pending_progress
            self._pending_progress[job_id] = value
            if not pending:
                self._events.emit((job_id, "progress", None))

    @Slot(object)
    def _receive(self, event: tuple) -> None:
        job_id, kind, value = event
        handle = self._tasks[job_id].handle
        if kind in {"progress", "detail"}:
            if kind == "progress":
                with self._progress_lock:
                    value = self._pending_progress.pop(job_id)
            if not handle.record.active:
                return
            message, fraction, detail = value
            self._update(
                handle, message=message, fraction=fraction,
                detail=detail if detail is not None else handle.record.detail,
            )
            handle.progress.emit(message, fraction)
            if detail is not None:
                handle.detail.emit(detail)
        else:
            self._running[handle.record.resource_class] -= 1
            self._finish(handle, *value)
            self._schedule()

    def _finish(self, handle: JobHandle, state: str, result: Any, error: str | None) -> None:
        self._tasks[handle.id].operation = None
        self._update(
            handle, state=state, finished_at=time.time(), result=result, error=error,
            message=error or state.capitalize(), fraction=None,
        )
        if state == "succeeded":
            handle.completed.emit(result)
        elif state in {"failed", "blocked"}:
            handle.failed.emit(error or state)
        handle.finished.emit()

    def shutdown(self, *, cancel: bool = True, wait: bool = False) -> None:
        self._assert_thread()
        if not cancel and self.active_jobs():
            raise RuntimeError("Wait for active jobs before shutting down without cancellation")
        self._closed = True
        self._timer.stop()
        if cancel:
            for record in self.active_jobs():
                self.cancel(record.id)
        self._executor.shutdown(wait=wait)
        if wait:
            QCoreApplication.sendPostedEvents(self, QEvent.Type.MetaCall)
