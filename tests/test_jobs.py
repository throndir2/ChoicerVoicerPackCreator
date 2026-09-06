from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QObject, QThread, Slot

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.operations import check_cancelled, path_leases


@pytest.fixture
def manager(qtbot):
    manager = JobManager(limits={"cpu": 2, "io": 2, "network": 1})
    yield manager
    manager.shutdown(wait=True)


def finish(qtbot, manager):
    qtbot.waitUntil(lambda: not manager.active_jobs(), timeout=10000)


def test_projects_run_concurrently_and_results_reach_qt_thread(qtbot, manager):
    started = threading.Barrier(2)
    threads = []

    class Receiver(QObject):
        @Slot(object)
        def receive(self, result):
            threads.append(QThread.currentThread())

    receiver = Receiver()

    def operation(context):
        started.wait(3)
        context.report("Measured", 0.5, detail=("stage", 3))
        return context.project_id

    first = manager.submit("one", "test", "First", operation)
    second = manager.submit("two", "test", "Second", operation)
    first.completed.connect(receiver.receive)
    second.completed.connect(receiver.receive)
    finish(qtbot, manager)
    assert [record.result for record in manager.tasks()] == ["one", "two"]
    assert threads == [manager.thread(), manager.thread()]
    assert len(manager.tasks("one")) == 1
    assert manager.tasks(None) == ()
    assert first.record.detail == ("stage", 3)


def test_same_destination_excludes_alias_and_nested_path(qtbot, manager, tmp_path):
    release = threading.Event()
    started = threading.Event()
    order = []

    def first_operation(context):
        order.append("first")
        started.set()
        release.wait(3)

    first = manager.submit(
        "one", "export", "First", first_operation, write_paths=[tmp_path / "pack"],
    )
    second = manager.submit(
        "two", "export", "Second", lambda context: order.append("second"),
        write_paths=[tmp_path / "pack" / ".." / "pack" / "output.ogv"],
    )
    qtbot.waitUntil(started.is_set)
    assert first.record.state == "running"
    assert second.record.state == "waiting"
    release.set()
    finish(qtbot, manager)
    assert order == ["first", "second"]


def test_concurrent_readers_and_writer_wait(qtbot, manager, tmp_path):
    barrier = threading.Barrier(2)
    release = threading.Event()

    def reader(context):
        barrier.wait(3)
        release.wait(3)

    first = manager.submit("one", "read", "Read", reader, read_paths=[tmp_path / "source"])
    second = manager.submit("two", "read", "Read", reader, read_paths=[tmp_path / "source"])
    writer = manager.submit(
        "three", "write", "Write", lambda context: None,
        resource_class="io", write_paths=[tmp_path],
    )
    qtbot.waitUntil(lambda: first.record.state == second.record.state == "running")
    assert writer.record.state == "waiting"
    release.set()
    finish(qtbot, manager)
    assert writer.record.state == "succeeded"


def test_queued_cancel_never_executes_and_running_cancel_waits_for_cleanup(qtbot, manager):
    started = threading.Event()
    cleanup = threading.Event()
    clean = threading.Event()

    def operation(context):
        started.set()
        try:
            while not context.cancelled():
                time.sleep(0.01)
            context.check_cancelled()
        finally:
            cleanup.wait(3)
            clean.set()

    running = manager.submit("one", "test", "Running", operation, resource_class="network")
    queued = manager.submit(
        "two", "test", "Queued", lambda context: pytest.fail("Cancelled queue ran"),
        resource_class="network",
    )
    qtbot.waitUntil(started.is_set)
    queued.cancel()
    assert queued.record.state == "cancelled"
    running.cancel()
    qtbot.wait(30)
    assert running.record.state == "cancelling"
    assert not clean.is_set()
    cleanup.set()
    finish(qtbot, manager)
    assert running.record.state == "cancelled"
    assert clean.is_set()


def test_failed_job_isolated_and_dependency_explains_block(qtbot, manager):
    def fail(context):
        raise ValueError("source invalid")

    first = manager.submit(None, "setup", "Setup", fail)
    dependent = manager.submit("one", "analysis", "Analyze", lambda ctx: None, depends_on=[first])
    second = manager.submit("two", "test", "Independent", lambda ctx: 42)
    finish(qtbot, manager)
    assert first.record.state == "failed"
    assert "source invalid" in first.record.error
    assert dependent.record.state == "blocked"
    assert "required task" in dependent.record.error
    assert second.record.result == 42
    assert manager.tasks(None) == (first.record,)


def test_global_setup_dependency_waits_then_starts(qtbot, manager):
    release = threading.Event()
    first = manager.submit(None, "setup", "Setup", lambda ctx: release.wait(3))
    dependent = manager.submit("one", "analysis", "Analyze", lambda ctx: 1, depends_on=[first])
    qtbot.waitUntil(lambda: dependent.record.state == "waiting")
    assert dependent.record.message == "Waiting for required tasks"
    release.set()
    finish(qtbot, manager)
    assert dependent.record.result == 1


def test_provenance_detached_and_frozen(qtbot, manager):
    metadata = {"revision": 2, "source": {"paths": ["original"]}}
    handle = manager.submit("one", "test", "Snapshot", lambda ctx: None, source_snapshot=metadata)
    metadata["source"]["paths"].append("changed")
    assert handle.record.source_snapshot["source"]["paths"] == ("original",)
    with pytest.raises(TypeError):
        handle.record.source_snapshot["revision"] = 3
    finish(qtbot, manager)


def test_critical_publication_finishes_successfully_despite_late_cancel(qtbot, manager):
    publishing = threading.Event()
    release = threading.Event()

    def operation(context):
        with context.critical_stage("Publishing; cancellation deferred"):
            publishing.set()
            release.wait(3)
            check_cancelled()
            assert not context.cancelled()
        return "published"

    handle = manager.submit("one", "export", "Export", operation)
    qtbot.waitUntil(publishing.is_set)
    handle.cancel()
    release.set()
    finish(qtbot, manager)
    assert handle.record.state == "succeeded"
    assert handle.record.cancel_requested
    assert handle.record.result == "published"


def test_cancellation_does_not_mask_cleanup_failure(qtbot, manager):
    started = threading.Event()

    def operation(context):
        started.set()
        while not context.cancelled():
            time.sleep(0.01)
        raise OSError("cleanup failed")

    handle = manager.submit("one", "export", "Export", operation)
    qtbot.waitUntil(started.is_set)
    handle.cancel()
    finish(qtbot, manager)
    assert handle.record.state == "failed"
    assert "cleanup failed" in handle.record.error


def test_backend_nested_leases_reuse_job_owner(qtbot, manager, tmp_path):
    def operation(context):
        with path_leases(write_paths=[tmp_path]):
            return "ok"

    handle = manager.submit("one", "export", "Export", operation, write_paths=[tmp_path])
    finish(qtbot, manager)
    assert handle.record.result == "ok"
