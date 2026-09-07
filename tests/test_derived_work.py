from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from shiboken6 import isValid

from choicer_voicer_pack_creator.jobs import JobManager, JobRecord
from choicer_voicer_pack_creator.ui import derived_work
from choicer_voicer_pack_creator.ui.derived_work import DerivedWorkCoordinator


class Handle(QObject):
    finished = Signal()
    state_changed = Signal(str)

    def __init__(self, generation: int, *, immediate_cancel: bool = False):
        super().__init__()
        self.record = JobRecord(
            str(generation), "project", "checks", "Checks", "io", {},
        )
        self.immediate_cancel = immediate_cancel
        self.cancelled = False
        self.cancel_calls = 0

    def cancel(self):
        self.cancelled = True
        self.cancel_calls += 1
        self.record = replace(self.record, cancel_requested=True)
        if self.immediate_cancel:
            self.finish("cancelled")
        else:
            self.record = replace(self.record, state="cancelling")
            self.state_changed.emit("cancelling")

    def finish(self, state="succeeded"):
        self.record = replace(self.record, state=state, error="old error" if state == "failed" else None)
        self.state_changed.emit(state)
        self.finished.emit()


@pytest.fixture
def scheduler(qapp, monkeypatch):
    state = SimpleNamespace(time=0.0, blocked=False, started=[], published=[], handles=[])
    monkeypatch.setattr(derived_work.time, "monotonic", lambda: state.time)
    coordinator = DerivedWorkCoordinator(blocked=lambda: state.blocked)

    def start(generation):
        handle = Handle(generation)
        state.started.append(generation)
        state.handles.append(handle)
        return handle

    def pump(seconds=0.0):
        state.time += seconds
        coordinator._timer.stop()
        coordinator._pump()
        coordinator._timer.stop()

    state.coordinator, state.start, state.pump = coordinator, start, pump
    yield state
    coordinator.close()
    coordinator.deleteLater()
    QCoreApplication.sendPostedEvents(coordinator, QEvent.Type.DeferredDelete)
    for handle in state.handles:
        handle.deleteLater()
        QCoreApplication.sendPostedEvents(handle, QEvent.Type.DeferredDelete)


def request(state, *, delay=250):
    generation = state.coordinator.invalidate("checks")
    state.coordinator.request("checks", state.start, state.published.append, delay_ms=delay)
    return generation


def test_rapid_a_b_c_starts_only_c_after_latest_deadline(scheduler):
    a = request(scheduler)
    scheduler.pump(0.2)
    b = request(scheduler)
    scheduler.pump(0.2)
    c = request(scheduler)
    scheduler.pump(0.2)
    assert scheduler.started == []
    scheduler.pump(0.051)
    assert a < b < c
    assert scheduler.started == [c]
    scheduler.handles[0].finish()
    assert [record.id for record in scheduler.published] == [str(c)]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_invalidation_guards_terminal_and_progress_before_replacement(scheduler, terminal):
    old = request(scheduler, delay=0)
    scheduler.pump()
    current = scheduler.coordinator.invalidate("checks")
    assert scheduler.handles[0].cancelled
    assert not scheduler.coordinator.current("checks", old)
    assert scheduler.coordinator.current("checks", current)
    scheduler.handles[0].finish(terminal)
    assert scheduler.published == []
    assert scheduler.started == [old]


def test_active_and_pending_remain_bounded_with_late_cancellation(scheduler):
    a = request(scheduler, delay=0)
    scheduler.pump()
    request(scheduler, delay=0)
    c = request(scheduler, delay=0)
    scheduler.pump()
    assert scheduler.started == [a]
    assert scheduler.handles[0].cancel_calls == 1
    scheduler.handles[0].finish("cancelled")
    scheduler.pump()
    assert scheduler.started == [a, c]
    assert not scheduler.coordinator._states["checks"].paused
    scheduler.handles[-1].finish()
    assert [record.id for record in scheduler.published] == [str(c)]


def test_parent_destruction_disconnects_late_worker_completion(qapp):
    parent = QObject()
    coordinator = DerivedWorkCoordinator(parent)
    handle = Handle(0)
    published = []
    coordinator.request("checks", lambda _generation: handle, published.append, delay_ms=0)
    coordinator._pump()
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(parent, QEvent.Type.DeferredDelete)
    assert not isValid(coordinator)
    handle.finish()
    assert published == []


def test_direct_handle_cancel_pauses_before_editing_and_late_completion(scheduler):
    request(scheduler, delay=0)
    scheduler.pump()
    cancelled = []
    scheduler.coordinator.cancelled.connect(lambda key, record: cancelled.append((key, record.id)))
    old = scheduler.handles[0]
    old.cancel()
    assert scheduler.coordinator._states["checks"].paused
    assert cancelled == [("checks", old.record.id)]
    latest = request(scheduler, delay=0)
    old.finish()
    scheduler.pump()
    assert len(scheduler.started) == 1
    assert scheduler.published == []
    scheduler.coordinator.resume("checks")
    scheduler.pump()
    assert scheduler.started[-1] == latest


@pytest.mark.parametrize("immediate", [False, True])
def test_same_generation_supersession_revokes_status_before_cancellation(scheduler, immediate):
    old_generation = request(scheduler, delay=0)
    scheduler.pump()
    old = scheduler.handles[0]
    old.immediate_cancel = immediate
    cancel = old.cancel
    guards = []

    def cancel_with_status():
        guards.append(scheduler.coordinator.current("checks", old_generation))
        cancel()

    old.cancel = cancel_with_status
    scheduler.coordinator.request("checks", scheduler.start, scheduler.published.append, delay_ms=0)
    latest = scheduler.coordinator.generation("checks")
    assert latest > old_generation
    assert guards == [False]
    if not immediate:
        old.finish("cancelled")
    scheduler.pump()
    assert scheduler.started == [old_generation, latest]
    # A consumer holding the old record remains stale after its replacement starts.
    assert not scheduler.coordinator.current("checks", old_generation)
    old.finish("failed")
    assert scheduler.published == []
    scheduler.handles[-1].finish()
    assert [record.id for record in scheduler.published] == [str(latest)]


def test_completion_continuation_keeps_generation_and_reports_deferred_delivery(scheduler):
    deliveries = []

    def publish(record):
        deliveries.append(scheduler.coordinator.publication_was_deferred("checks"))
        scheduler.coordinator.request(
            "checks", scheduler.start, scheduler.published.append, delay_ms=0,
        )

    scheduler.coordinator.request("checks", scheduler.start, publish, delay_ms=0)
    scheduler.pump()
    scheduler.blocked = True
    scheduler.handles[0].finish()
    scheduler.blocked = False
    scheduler.pump()
    assert deliveries == [True]
    assert not scheduler.coordinator.publication_was_deferred("checks")
    assert scheduler.started == [0, 0]
    scheduler.handles[-1].finish()
    assert len(scheduler.published) == 1


def test_synchronous_queued_cancellation_cannot_publish_or_erase_replacement(scheduler):
    def start(generation):
        handle = Handle(generation, immediate_cancel=True)
        scheduler.handles.append(handle)
        return handle

    scheduler.coordinator.request("checks", start, scheduler.published.append, delay_ms=0)
    scheduler.pump()
    latest = request(scheduler, delay=0)
    assert scheduler.published == []
    scheduler.pump()
    assert scheduler.started == [latest]


def test_a_b_a_has_distinct_versions_even_with_identical_final_inputs(scheduler):
    a = request(scheduler, delay=0)
    scheduler.pump()
    request(scheduler)
    restored_a = request(scheduler)
    scheduler.handles[0].finish()
    assert not scheduler.coordinator.current("checks", a)
    assert scheduler.coordinator.current("checks", restored_a)
    assert scheduler.published == []


def test_explicit_pause_requires_resume_but_supersession_does_not(scheduler):
    request(scheduler, delay=0)
    scheduler.pump()
    scheduler.coordinator.pause("checks")
    scheduler.handles[0].finish("cancelled")
    scheduler.coordinator.request("checks", scheduler.start, scheduler.published.append, delay_ms=250)
    scheduler.pump(1)
    assert len(scheduler.started) == 1
    scheduler.coordinator.resume("checks")
    scheduler.pump(0.2)
    assert len(scheduler.started) == 1
    scheduler.pump(0.051)
    assert len(scheduler.started) == 2


def test_new_unchanged_gesture_restarts_full_debounce(scheduler):
    request(scheduler)
    scheduler.pump(0.2)
    scheduler.coordinator.suspend()
    scheduler.pump(1)
    assert scheduler.started == []
    scheduler.coordinator.wake()
    scheduler.pump(0.2)
    assert scheduler.started == []
    scheduler.pump(0.051)
    assert len(scheduler.started) == 1


def test_blocked_terminal_is_deferred_and_invalidated_without_publication(scheduler):
    request(scheduler, delay=0)
    scheduler.pump()
    scheduler.blocked = True
    scheduler.handles[0].finish("failed")
    scheduler.pump()
    assert scheduler.published == []
    request(scheduler)
    scheduler.blocked = False
    scheduler.pump()
    assert scheduler.published == []


def test_blocked_predicate_resets_pending_deadline_and_holds_valid_result(scheduler):
    request(scheduler)
    scheduler.blocked = True
    scheduler.pump(1)
    scheduler.blocked = False
    scheduler.pump()
    scheduler.pump(0.251)
    assert len(scheduler.started) == 1
    scheduler.blocked = True
    scheduler.handles[0].finish()
    assert scheduler.published == []
    scheduler.blocked = False
    scheduler.pump()
    assert len(scheduler.published) == 1


def test_close_discards_active_pending_and_deferred_work(scheduler):
    request(scheduler, delay=0)
    scheduler.pump()
    scheduler.blocked = True
    scheduler.handles[0].finish()
    scheduler.coordinator.close()
    scheduler.blocked = False
    scheduler.pump(10)
    assert scheduler.published == []
    assert not scheduler.coordinator.current("checks", 1)


def test_factory_may_have_no_work_or_reentrantly_invalidate(scheduler):
    def start(_generation):
        scheduler.coordinator.invalidate("checks")
        return Handle(0, immediate_cancel=True)

    scheduler.coordinator.request("checks", start, scheduler.published.append, delay_ms=0)
    scheduler.pump()
    assert scheduler.published == []
    scheduler.coordinator.request("checks", lambda _generation: None, scheduler.published.append, delay_ms=0)
    scheduler.pump()
    assert scheduler.coordinator._states["checks"].active is None


def test_factory_failure_is_published_only_when_current_and_unblocked(scheduler):
    def start(_generation):
        raise RuntimeError("Unavailable")

    scheduler.coordinator.request("checks", start, scheduler.published.append, delay_ms=0)
    scheduler.pump()
    scheduler.blocked = True
    scheduler.pump()
    assert scheduler.published == []
    scheduler.blocked = False
    scheduler.pump()
    assert scheduler.published[0].state == "failed"
    assert scheduler.published[0].error == "Unavailable"


def test_publication_reentrantly_suspending_stops_other_pending_keys(scheduler):
    def publish(record):
        scheduler.published.append(record)
        scheduler.coordinator.suspend()

    scheduler.coordinator.request("checks", scheduler.start, publish, delay_ms=0)
    scheduler.pump()
    scheduler.blocked = True
    scheduler.handles[0].finish()
    scheduler.coordinator.request("other", scheduler.start, scheduler.published.append, delay_ms=0)
    scheduler.blocked = False
    scheduler.pump()
    assert len(scheduler.started) == 1
    assert len(scheduler.published) == 1
    scheduler.coordinator.wake()
    scheduler.pump()
    assert len(scheduler.started) == 2


def test_coordinator_respects_shared_job_capacity(qtbot):
    manager = JobManager(limits={"cpu": 1})
    coordinator = DerivedWorkCoordinator()
    started, release = threading.Event(), threading.Event()
    calls, published = [], []

    def occupied(_context):
        started.set()
        assert release.wait(10)

    manager.submit("other", "backing", "Backing", occupied)
    qtbot.waitUntil(started.is_set)

    def start(generation):
        return manager.submit(
            "project", "checks", "Checks",
            lambda _context: calls.append(generation),
        )

    try:
        for _ in range(3):
            coordinator.invalidate("checks")
            coordinator.request("checks", start, published.append, delay_ms=0)
            qtbot.waitUntil(lambda: coordinator._states["checks"].active is not None)
        assert calls == []
        assert len(manager.active_jobs()) == 2
        release.set()
        qtbot.waitUntil(lambda: bool(published))
        assert calls == [3]
    finally:
        coordinator.close()
        release.set()
        qtbot.waitUntil(lambda: not manager.active_jobs())
        manager.shutdown(wait=True)
        coordinator.deleteLater()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(coordinator, QEvent.Type.DeferredDelete)
        QCoreApplication.sendPostedEvents(manager, QEvent.Type.DeferredDelete)
