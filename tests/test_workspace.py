from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QSettings, Qt, QThread, Signal

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class QuietMedia:
    def waveform_peaks(self, _path, _duration, *, cancelled):
        return [] if cancelled() else [0.5]


@pytest.fixture
def workspace(qtbot, tmp_path):
    window = MainWindow(
        QuietMedia(),
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    window.show()
    yield window
    for record in window.job_manager.active_jobs():
        window.job_manager.cancel(record.id)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    for box in list(window._decisions):
        box.reject()
    for editor in window.editors.values():
        editor.dirty = False
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())


def test_running_project_does_not_block_edit_save_or_open(workspace, qtbot, tmp_path):
    window = workspace
    a = window.active_editor
    a._set_project(PackProject(title="A", authors=["Author"]), None, False)
    release = threading.Event()

    def export(context):
        while not release.wait(0.01):
            context.check_cancelled()
        return tmp_path / "A"

    job = window.job_manager.submit(a.session.id, "export", "Export A", export)
    qtbot.waitUntil(lambda: job.record.state == "running")
    b = window.add_project(PackProject(title="B", authors=["Author"]), dirty=False)
    b.title_edit.selectAll()
    qtbot.keyClicks(b.title_edit, "B edited")
    assert b.dirty and b.project.title == "B edited"
    destination = tmp_path / "B.cvpack.json"
    assert window.save_editor(b, destination=destination)
    qtbot.waitUntil(lambda: destination.is_file() and not b.dirty)
    source = tmp_path / "C.cvpack.json"
    ProjectStore.save(PackProject(title="C", authors=["Author"]), source)
    window.open_path(source)
    count = window.tabs.count()
    window.open_path(source)
    assert window.tabs.count() == count
    qtbot.waitUntil(lambda: window.active_editor.project.title == "C")
    assert job.record.active
    window.focus_project(a.session.id)
    window.focus_project(b.session.id)
    assert b.project.title == "B edited"
    assert not job.record.cancel_requested
    release.set()
    qtbot.waitUntil(lambda: not job.record.active)
    assert window.active_editor is b


def test_tabs_keep_selection_range_and_zoom(workspace):
    window = workspace
    first = window.active_editor
    segment = Segment(1, 4, "First line", ["A"])
    first._set_project(
        PackProject(title="A", authors=["Author"], video_duration=10, segments=[segment]),
        None, False,
    )
    first.select_segment(segment.id)
    first.mark_in_spin.setValue(2)
    first.mark_out_spin.setValue(5)
    first.zoom_slider.setValue(35)
    second = window.add_project(PackProject(title="B", authors=["Author"]), dirty=False)
    window.focus_project(first.session.id)
    assert first.selected_segment_id == segment.id
    assert first.mark_in_spin.value() == 2
    assert first.mark_out_spin.value() == 5
    assert first.zoom_slider.value() == 35
    assert second.project.segments == []


def test_save_snapshot_cannot_clear_newer_edits(workspace, qtbot, tmp_path, monkeypatch):
    window = workspace
    editor = window.active_editor
    editor._set_project(PackProject(title="Revision N", authors=["Author"]), None, True)
    started, release = threading.Event(), threading.Event()
    original = ProjectStore.save

    def save(snapshot, destination):
        started.set()
        assert release.wait(5)
        original(snapshot, destination)

    monkeypatch.setattr(ProjectStore, "save", save)
    path = tmp_path / "project.cvpack.json"
    assert window.save_editor(editor, destination=path)
    qtbot.waitUntil(started.is_set)
    editor.title_edit.selectAll()
    qtbot.keyClicks(editor.title_edit, "Revision N plus one")
    release.set()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert editor.dirty
    assert editor.project.title == "Revision N plus one"
    assert ProjectStore.load(path).title == "Revision N"
    assert editor.session.saved_revision < editor.session.revision


def test_save_path_cannot_be_shared_by_two_documents(workspace, qtbot, tmp_path):
    first = workspace.active_editor
    first._set_project(PackProject(title="A", authors=["Author"]), None, True)
    second = workspace.add_project(PackProject(title="B", authors=["Author"]), dirty=True)
    path = tmp_path / "shared.cvpack.json"
    assert workspace.save_editor(first, destination=path)
    assert not workspace.save_editor(second, destination=path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert ProjectStore.load(path).title == "A"
    assert second.dirty


def test_keep_processing_hides_tab_not_document_or_job(workspace, qtbot):
    editor = workspace.active_editor
    editor._set_project(PackProject(title="Working", authors=["Author"]), None, True)
    release = threading.Event()

    def scan(context):
        while not release.wait(0.01):
            context.check_cancelled()
        return "ready"

    job = workspace.job_manager.submit(editor.session.id, "analysis", "Scanning", scan)
    qtbot.waitUntil(lambda: job.record.state == "running")
    workspace.close_project_tab(workspace.tabs.indexOf(editor))
    box = workspace._decisions[-1]
    assert box.windowModality() == Qt.WindowModality.NonModal
    keep = next(button for button in box.buttons() if button.text() == "Keep processing")
    qtbot.mouseClick(keep, Qt.MouseButton.LeftButton)
    assert editor.session.hidden
    assert workspace.tabs.indexOf(editor) == -1
    assert editor.session.id in workspace.editors
    assert job.record.active and not job.record.cancel_requested
    release.set()
    qtbot.waitUntil(lambda: job.record.state == "succeeded")
    assert workspace.active_editor is not editor
    workspace.focus_project(editor.session.id)
    assert workspace.active_editor is editor
    assert editor.dirty


def test_per_document_recovery_and_workspace_restore(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    recovery = RecoveryStore(tmp_path / "recovery-v2.json")
    window = MainWindow(QuietMedia(), settings=settings, recovery_store=recovery)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    a = window.active_editor
    a._set_project(PackProject(title="Recovery A", authors=["A"]), None, True)
    b = window.add_project(PackProject(title="Recovery B", authors=["B"]), dirty=True)
    a._write_recovery_snapshot()
    b._write_recovery_snapshot()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert a.recovery_store.load().project.title == "Recovery A"
    assert b.recovery_store.load().project.title == "Recovery B"
    a._clear_recovery_snapshot()
    a.dirty = False
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert a.recovery_store.load() is None
    assert b.recovery_store.load().project.title == "Recovery B"
    window.save_workspace_state()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    restored = MainWindow(QuietMedia(), settings=settings, recovery_store=recovery)
    qtbot.addWidget(restored)
    qtbot.waitUntil(lambda: any(
        editor.project.title == "Recovery B" for editor in restored.editors.values()
    ))
    recovered = restored.editor_for_project(b.session.id)
    assert recovered.dirty
    assert recovered.project.title == "Recovery B"
    for current in (window, restored):
        current.recovery_store = None
        current.workspace_store = None
        for editor in current.editors.values():
            editor._recovery_timer.stop()
            editor.dirty = False
        qtbot.waitUntil(lambda: not current.job_manager.active_jobs())
        current.close()


def test_managed_worker_cancellation_is_terminal_on_gui_thread(workspace, qtbot):
    threads = []

    class Worker(JobWorker):
        failed = Signal(str)
        canceled = Signal()

        def run(self):
            try:
                while not self.isInterruptionRequested():
                    QThread.msleep(5)
                raise OperationCancelled("Stopped")
            except OperationCancelled:
                self.canceled.emit()

    worker = Worker()
    worker.configure_job(workspace.job_manager, workspace.active_editor.session.id, "scan", "Scan")
    worker.finished.connect(lambda: threads.append(QThread.currentThread()))
    worker.start()
    qtbot.waitUntil(lambda: worker.job_handle.record.state == "running")
    worker.requestInterruption()
    qtbot.waitUntil(lambda: bool(threads))
    assert worker.job_handle.record.state == "cancelled"
    assert threads == [workspace.thread()]
    assert not worker.isRunning()
    assert worker.wait(0)
