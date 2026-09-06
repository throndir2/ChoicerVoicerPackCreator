from __future__ import annotations

import gc
import subprocess
import sys
import threading
import time
import weakref

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QThread, Slot

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.operations import check_cancelled, path_leases


@pytest.fixture
def manager(qtbot):
    manager = JobManager(limits={"cpu": 2, "io": 2, "network": 1})
    yield manager
    manager.shutdown(wait=True)
    manager.deleteLater()
    QCoreApplication.sendPostedEvents(manager, QEvent.Type.DeferredDelete)


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


def test_progress_is_coalesced_without_inventing_final_percentage(qtbot, manager):
    reported = threading.Event()

    def operation(context):
        for index in range(10000):
            context.report("Measured items", index / 10000)
        reported.set()

    received = []
    handle = manager.submit("one", "test", "Progress", operation)
    handle.progress.connect(lambda message, fraction: received.append(fraction))
    manager._schedule()
    assert reported.wait(3)
    finish(qtbot, manager)
    assert received == [0.9999]
    assert handle.record.fraction is None
    assert handle.record.detail is None


def test_rich_progress_retains_plan_and_stage_events(qtbot, manager):
    reported = threading.Event()

    def operation(context):
        context.report("Plan", detail=("plan", 3))
        context.report("Encoding", detail=("encoding", 1))
        context.report("Probing")
        context.report("Publishing", detail=("publication", 2))
        reported.set()

    received = []
    handle = manager.submit("one", "export", "Export", operation)
    handle.detail.connect(received.append)
    manager._schedule()
    assert reported.wait(3)
    finish(qtbot, manager)
    assert received == [("plan", 3), ("encoding", 1), ("publication", 2)]
    assert handle.record.detail == ("publication", 2)


def test_shutdown_cancels_queue_without_starting_it(qtbot, manager):
    handles = [
        manager.submit("one", "test", "Queued", lambda ctx: pytest.fail("Queue started"))
        for _ in range(5)
    ]
    manager.shutdown(wait=True)
    assert all(handle.record.state == "cancelled" for handle in handles)


def test_worker_system_exit_is_a_failure_not_success(qtbot, manager):
    def operation(context):
        raise SystemExit(7)

    handle = manager.submit("one", "test", "Exit", operation)
    finish(qtbot, manager)
    assert handle.record.state == "failed"
    assert handle.record.error == "SystemExit: 7"


def test_headless_manager_uses_qtcore_without_widgets():
    script = """
import sys
from PySide6.QtCore import QCoreApplication, QTimer
from choicer_voicer_pack_creator.jobs import JobManager
try:
    JobManager()
except RuntimeError as error:
    assert "QtCore" in str(error)
else:
    raise AssertionError("Missing event loop was accepted")
app = QCoreApplication([])
manager = JobManager()
handle = manager.submit("headless", "test", "Core only", lambda context: 42)
handle.finished.connect(app.quit)
QTimer.singleShot(5000, app.quit)
app.exec()
manager.shutdown(wait=True)
assert handle.record.state == "succeeded", handle.record
assert handle.record.result == 42
assert "PySide6.QtWidgets" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_shutdown_manager_is_retired_on_its_owner_thread(qtbot):
    manager = JobManager()
    manager.submit("one", "test", "Complete", lambda context: 1)
    finish(qtbot, manager)
    manager.shutdown(wait=True)
    destroyed_on = []
    manager.destroyed.connect(
        lambda: destroyed_on.append(threading.get_ident()), Qt.ConnectionType.DirectConnection,
    )
    reference = weakref.ref(manager)
    owner_thread = threading.get_ident()
    del manager
    assert reference() is None
    assert destroyed_on == [owner_thread]


def test_shutdown_from_running_notification_releases_unstarted_job(qtbot, manager, tmp_path):
    executed = []
    handle = manager.submit(
        "one", "export", "Starting", lambda context: executed.append(True),
        write_paths=[tmp_path],
    )
    handle.state_changed.connect(
        lambda state: manager.shutdown(wait=True) if state == "running" else None,
    )
    manager._schedule()
    assert handle.record.state == "cancelled"
    assert not manager.active_jobs()
    assert not any(manager._running.values())
    assert not executed

    other = JobManager()
    try:
        following = other.submit("two", "export", "Next", lambda ctx: 42, write_paths=[tmp_path])
        finish(qtbot, other)
        assert following.record.result == 42
    finally:
        other.shutdown(wait=True)
        other.deleteLater()
        QCoreApplication.sendPostedEvents(other, QEvent.Type.DeferredDelete)


def test_rejected_executor_admission_never_runs_or_leaks_reservations(
    qtbot, manager, tmp_path, monkeypatch,
):
    queued = []
    executed = []

    def rejected(function, *args):
        queued.append(lambda: function(*args))
        raise RuntimeError("executor rejected submission")

    with monkeypatch.context() as patch:
        patch.setattr(manager._executor, "submit", rejected)
        handle = manager.submit(
            "one", "export", "Rejected", lambda context: executed.append(True),
            write_paths=[tmp_path],
        )
        manager._schedule()
    assert handle.record.state == "failed"
    assert "executor rejected submission" in handle.record.error
    assert not manager.active_jobs()
    assert not any(manager._running.values())
    queued[0]()
    assert not executed
    following = manager.submit("two", "export", "Next", lambda ctx: 42, write_paths=[tmp_path])
    finish(qtbot, manager)
    assert following.record.result == 42


def test_real_executor_start_failure_does_not_retain_qt_owner(qtbot, monkeypatch, tmp_path):
    manager = JobManager()
    executed = []

    def fail_start():
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(manager._executor, "_adjust_thread_count", fail_start)
        handle = manager.submit(
            "one", "export", "Rejected", lambda context: executed.append(True),
            write_paths=[tmp_path],
        )
        manager._schedule()
    assert handle.record.state == "failed"
    assert not manager.active_jobs()
    assert not any(manager._running.values())
    manager.shutdown(wait=True)
    destroyed_on = []
    manager.destroyed.connect(
        lambda: destroyed_on.append(threading.get_ident()), Qt.ConnectionType.DirectConnection,
    )
    reference = weakref.ref(manager)
    owner_thread = threading.get_ident()
    del manager
    assert reference() is None
    collector = threading.Thread(target=gc.collect)
    collector.start()
    collector.join()
    assert destroyed_on == [owner_thread]
    assert not executed
