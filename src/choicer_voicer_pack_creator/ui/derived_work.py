"""Qt-thread latest-request-wins scheduling over the application JobManager."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

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
    publishing_deferred: bool = False


class DerivedWorkCoordinator(QObject):
    """Coalesce one pending request and at most one active handle per key.

    Call ``invalidate`` immediately when inputs change, before debounced ``request``.
    Factories run on the Qt thread and return a started JobManager/JobWorker handle
    (or None for no work). Only current terminal records reach ``publish``.
    ``current`` also guards progress/errors consumed outside this coordinator.
    Replacing live work without an intervening ``invalidate`` advances its
    generation too: use the generation passed to the factory for job metadata.

    ``pause`` is an explicit cancel-and-pause until ``resume``; supersession never
    pauses. ``suspend`` / ``resume_suspended`` instead hold starts and publication
    during gestures without discarding valid active work. They are idempotent, and
    resumption starts the complete debounce again, even if no input changed.
    A shared blocked predicate additionally holds starts and all publication.
    """

    changed = Signal()
    resumed = Signal()
    cancelled = Signal(str, object)

    def __init__(
        self, parent: QObject | None = None, *, blocked: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(parent)
        self._blocked = blocked
        self._states: dict[str, _State] = {}
        self._closed = False
        self._suspended = False
        self._pumping = False
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

    def publication_was_deferred(self, key: str) -> bool:
        """During publish, indicate that a terminal result waited behind a blocker."""
        return self._state(key).publishing_deferred

    def invalidate(self, key: str) -> int:
        state = self._state(key)
        # Queued-job cancellation emits finished synchronously. Revoke publication
        # before touching the handle, including anything deferred behind a modal.
        state.generation += 1
        state.serial += 1
        state.pending = None
        state.publication = None
        active = state.active
        if active is not None and not active[1].record.cancel_requested:
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
        if state.starting or any(
            item is not None and item[0].generation == state.generation
            for item in (state.active, state.publication)
        ):
            # A serial alone cannot guard external consumers with key/generation
            # metadata. Revoke it before cancel(), which may finish synchronously.
            state.generation += 1
        state.serial += 1
        delay = delay_ms / 1000
        state.pending = _Request(
            state.generation, state.serial, start, publish, delay, time.monotonic() + delay,
        )
        state.publication = None
        active = state.active
        if active is not None and not active[1].record.cancel_requested:
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

    def dispatch_ready(self) -> None:
        """Admit ready jobs before a finishing job yields its scheduler capacity."""
        self._pump()

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
                self._deliver(state, request, handle.record, deferred=False)
            else:
                state.publication = request, handle.record
        self.changed.emit()
        self._wake()

    @Slot()
    def _handle_finished(self) -> None:
        handle = self.sender()
        for key, state in tuple(self._states.items()):
            if state.active is not None and state.active[1] is handle:
                self._finished(key, *state.active)
                return

    @Slot(str)
    def _handle_state_changed(self, _status: str) -> None:
        handle = self.sender()
        for key, state in tuple(self._states.items()):
            if state.active is None or state.active[1] is not handle:
                continue
            request, active = state.active
            if active.record.cancel_requested and self._valid(state, request):
                # Internal supersession revokes the generation before cancel().
                # A still-current handle was cancelled directly (e.g. Tasks).
                self.pause(key)
                self.cancelled.emit(key, active.record)
            return

    @staticmethod
    def _deliver(
        state: _State, request: _Request, record: JobRecord, *, deferred: bool,
    ) -> None:
        previous = state.publishing_deferred
        state.publishing_deferred = deferred
        try:
            request.publish(record)
        finally:
            state.publishing_deferred = previous

    def _pump(self) -> None:
        if self._pumping:
            self._wake()
            return
        self._pumping = True
        try:
            self._dispatch_ready()
        finally:
            self._pumping = False

    def _dispatch_ready(self) -> None:
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
                    self._deliver(state, request, record, deferred=True)
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
            except (OSError, RuntimeError, ValueError) as error:
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
                handle.state_changed.connect(self._handle_state_changed)
                handle.finished.connect(self._handle_finished)
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
