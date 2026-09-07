"""Qt-thread latest-request-wins scheduling over the application JobManager."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from choicer_voicer_pack_creator.jobs import JobHandle, JobRecord


@dataclass
class _Request:
    generation: int
    serial: int
    start: Callable[[int], JobHandle | None]
    publish: Callable[[JobRecord], None]
    delay: float
    deadline: float


@dataclass
class _State:
    generation: int = 0
    serial: int = 0
    pending: _Request | None = None
    active: tuple[_Request, JobHandle] | None = None
    publication: tuple[_Request, JobRecord] | None = None
    paused: bool = False
    suspended: bool = False
    blocked: bool = False
    starting: bool = False


class DerivedWorkCoordinator(QObject):
    """Coalesce one pending request and at most one active handle per key.

    Call ``invalidate`` immediately when inputs change, before debounced ``request``.
    Factories run on the Qt thread and return a started JobManager/JobWorker handle
    (or None for no work). Only current terminal records reach ``publish``.
    ``current`` also guards progress/errors consumed outside this coordinator.

    ``pause`` is an explicit cancel-and-pause until ``resume``; supersession never
    pauses. ``suspend`` / ``resume_suspended`` instead hold starts and publication
    during gestures without discarding valid active work. They are idempotent, and
    resumption starts the complete debounce again, even if no input changed.
    A shared blocked predicate additionally holds starts and all publication.
    """

    changed = Signal()
    resumed = Signal()

    def __init__(
        self, parent: QObject | None = None, *, blocked: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(parent)
        self._blocked = blocked
        self._states: dict[str, _State] = {}
        self._closed = False
        self._suspended = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._pump)

    def _state(self, key: str) -> _State:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Derived work must be controlled from its owning Qt thread")
        return self._states.setdefault(key, _State())

    def generation(self, key: str) -> int:
        return self._state(key).generation

    def current(self, key: str, generation: int) -> bool:
        state = self._state(key)
        return not self._closed and not state.paused and state.generation == generation

    def invalidate(self, key: str) -> int:
        state = self._state(key)
        # Queued-job cancellation emits finished synchronously. Revoke publication
        # before touching the handle, including anything deferred behind a modal.
        state.generation += 1
        state.serial += 1
        state.pending = None
        state.publication = None
        active = state.active
        if active is not None:
            active[1].cancel()
        self.changed.emit()
        self._wake()
        return state.generation

    def request(
        self, key: str, start: Callable[[int], JobHandle | None],
        publish: Callable[[JobRecord], None], *, delay_ms: int = 250,
    ) -> None:
        if delay_ms < 0:
            raise ValueError("The debounce delay must not be negative")
        state = self._state(key)
        if self._closed:
            return
        state.serial += 1
        delay = delay_ms / 1000
        state.pending = _Request(
            state.generation, state.serial, start, publish, delay, time.monotonic() + delay,
        )
        state.publication = None
        active = state.active
        if active is not None:
            active[1].cancel()
        self.changed.emit()
        self._wake()

    def pause(self, key: str) -> None:
        self._state(key).paused = True
        self.invalidate(key)

    def resume(self, key: str) -> None:
        state = self._state(key)
        state.paused = False
        self._reset_deadline(state)
        self.changed.emit()
        self._wake()

    def suspend(self, key: str | None = None) -> None:
        if key is None:
            self._suspended = True
        else:
            self._state(key).suspended = True
        self._wake()

    def resume_suspended(self, key: str | None = None) -> None:
        if key is None:
            self._suspended = False
            for state in self._states.values():
                self._reset_deadline(state)
            self.resumed.emit()
        else:
            state = self._state(key)
            state.suspended = False
            self._reset_deadline(state)
        self._wake()

    def wake(self) -> None:
        """Finish a shared gesture and reconsider blocked work after a full debounce."""
        self.resume_suspended()

    @staticmethod
    def _reset_deadline(state: _State) -> None:
        if state.pending is not None:
            state.pending.deadline = time.monotonic() + state.pending.delay
        state.blocked = False

    def close(self) -> None:
        self._closed = True
        self._timer.stop()
        for key in tuple(self._states):
            self.invalidate(key)

    def _wake(self) -> None:
        if not self._closed:
            self._timer.start(0)

    def _valid(self, state: _State, request: _Request) -> bool:
        return (
            not self._closed and not state.paused
            and state.generation == request.generation and state.serial == request.serial
        )

    def _finished(self, key: str, request: _Request, handle: JobHandle) -> None:
        state = self._state(key)
        if state.active != (request, handle):
            return
        state.active = None
        if self._valid(state, request):
            if not self._blocked() and not self._suspended and not state.suspended:
                request.publish(handle.record)
            else:
                state.publication = request, handle.record
        self.changed.emit()
        self._wake()

    def _pump(self) -> None:
        if self._closed:
            return
        wait: float | None = None
        for key, state in tuple(self._states.items()):
            if state.paused:
                continue
            if self._blocked() or self._suspended or state.suspended:
                state.blocked = True
                if state.pending is not None or state.publication is not None:
                    wait = 0.05 if wait is None else min(wait, 0.05)
                continue
            if state.blocked:
                self._reset_deadline(state)
            publication, state.publication = state.publication, None
            if publication is not None:
                request, record = publication
                if self._valid(state, request):
                    request.publish(record)
                    self.changed.emit()
            if self._blocked() or self._suspended or state.suspended:
                state.blocked = True
                if state.pending is not None:
                    wait = 0.05 if wait is None else min(wait, 0.05)
                continue
            request = state.pending
            if (
                request is None or state.active is not None or state.starting
                or not self._valid(state, request)
            ):
                continue
            remaining = request.deadline - time.monotonic()
            if remaining > 0:
                wait = remaining if wait is None else min(wait, remaining)
                continue
            state.pending = None
            state.starting = True
            try:
                handle = request.start(request.generation)
            except Exception as error:
                handle = None
                if self._valid(state, request):
                    state.publication = request, JobRecord(
                        f"derived-{key}-{request.serial}", None, key, key, "io",
                        {"derived_key": key, "derived_generation": request.generation},
                        state="failed", error=str(error),
                    )
                    self._wake()
            finally:
                state.starting = False
            if handle is not None:
                state.active = request, handle
                handle.finished.connect(
                    lambda key=key, request=request, handle=handle:
                    self._finished(key, request, handle),
                )
                if not self._valid(state, request):
                    handle.cancel()
                if not handle.record.active:
                    self._finished(key, request, handle)
            self.changed.emit()
        if wait is not None and not self._closed:
            # A reentrant request may already have requested an immediate turn.
            delay = max(1, math.ceil(wait * 1000))
            if not self._timer.isActive() or self._timer.remainingTime() > delay:
                self._timer.start(delay)
