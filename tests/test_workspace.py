from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox
from shiboken6 import isValid

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.project_session import canonical_project_path
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.ui.main_window import MainWindow, WaveformWorker


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
        qtbot.waitUntil(lambda current=current: not current.job_manager.active_jobs())
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


@pytest.mark.parametrize("unhandled", [False, True])
def test_waveform_task_failure_preserves_request_identity_and_error(
    workspace, qtbot, tmp_path, unhandled,
):
    class FailedMedia:
        def waveform_peaks(self, *_args, **_kwargs):
            raise RuntimeError("Synthetic waveform failure")

    class FailedWorker(WaveformWorker):
        def run(self):
            raise RuntimeError("Synthetic waveform failure")

    worker_type = FailedWorker if unhandled else WaveformWorker
    source = str(tmp_path / "missing.mp4")
    worker = worker_type(FailedMedia(), 7, source, 1)
    worker.setParent(workspace.active_editor)
    worker.configure_job(
        workspace.job_manager, workspace.active_editor.session.id, "waveform", "Waveform",
    )
    failures = []
    worker.failed.connect(lambda *values: failures.append(values))
    worker.start()
    qtbot.waitUntil(lambda: not worker.isRunning())
    assert worker.job_handle.record.state == "failed"
    assert worker.job_handle.record.error.endswith("Synthetic waveform failure")
    expected = (
        "RuntimeError: Synthetic waveform failure" if unhandled else "Synthetic waveform failure"
    )
    assert failures == [(7, source, expected)]


@pytest.mark.parametrize("close_workspace", [False, True])
def test_close_keeps_qobjects_alive_until_operation_cleanup_finishes(
    workspace, qtbot, close_workspace,
):
    editor = workspace.active_editor
    cleanup_started, cleanup_release = threading.Event(), threading.Event()
    completed_on = []

    class Worker(JobWorker):
        canceled = Signal()

        def run(self):
            while not self.isInterruptionRequested():
                QThread.msleep(5)
            cleanup_started.set()
            assert cleanup_release.wait(5)
            self.canceled.emit()

    worker = Worker()
    worker.setParent(editor)
    worker.configure_job(workspace.job_manager, editor.session.id, "scan", "Held cleanup")
    worker.finished.connect(lambda: completed_on.append(QThread.currentThread()))
    worker.start()
    qtbot.waitUntil(lambda: worker.job_handle.record.state == "running")
    if close_workspace:
        assert not workspace.close()
        box = workspace._decisions[-1]
        button = box.button(QMessageBox.StandardButton.Yes)
    else:
        workspace.close_project_tab(workspace.tabs.indexOf(editor))
        box = workspace._decisions[-1]
        button = next(b for b in box.buttons() if b.objectName() == "projectCloseCancelTasks")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(cleanup_started.is_set)
    assert workspace.isVisible()
    assert isValid(editor) and isValid(worker) and isValid(workspace.job_manager)
    assert worker.job_handle.record.active
    assert not completed_on
    if not close_workspace:
        workspace.tasks_panel.refresh()
        for row in range(workspace.tasks_panel.table.rowCount()):
            if workspace.tasks_panel.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == worker.job_handle.id:
                workspace.tasks_panel.table.selectRow(row)
                break
        assert not workspace.tasks_panel.project_button.isEnabled()
        workspace.tasks_panel._show_project()
        assert workspace.tabs.indexOf(editor) == -1
    cleanup_release.set()
    qtbot.waitUntil(lambda: bool(completed_on))
    assert completed_on == [workspace.thread()]
    if close_workspace:
        qtbot.waitUntil(lambda: not workspace.isVisible())
        assert not workspace.job_manager.active_jobs()
    else:
        qtbot.waitUntil(lambda: not isValid(editor))
        assert editor.session.id not in workspace.editors
        assert not isValid(worker)
        assert workspace.isVisible()


def test_reopening_cancelled_pending_load_keeps_new_request_identity(
    workspace, qtbot, tmp_path, monkeypatch,
):
    path = tmp_path / "pending.cvpack.json"
    ProjectStore.save(PackProject(title="Saved document"), path)
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []
    original = ProjectStore.load

    def load(source):
        index = len(calls)
        calls.append(source)
        started[index].set()
        assert release[index].wait(5)
        return original(source)

    monkeypatch.setattr(ProjectStore, "load", load)
    workspace.open_path(path)
    old = workspace.active_editor
    qtbot.waitUntil(started[0].is_set)
    workspace.close_project_tab(workspace.tabs.indexOf(old))
    box = workspace._decisions[-1]
    button = next(b for b in box.buttons() if b.objectName() == "projectCloseCancelTasks")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    workspace.open_path(path)
    new = workspace.active_editor
    assert new is not old
    qtbot.waitUntil(started[1].is_set)
    release[0].set()
    qtbot.waitUntil(lambda: old.session.id not in workspace.editors)
    assert workspace._opening_paths[canonical_project_path(path)] == new.session.id
    count = workspace.tabs.count()
    workspace.open_path(path)
    assert workspace.active_editor is new
    assert workspace.tabs.count() == count
    release[1].set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert new.project.title == "Saved document"
    assert not new.dirty


def test_new_edits_in_discarded_document_require_a_fresh_exit_decision(workspace, qtbot):
    first = workspace.active_editor
    first._set_project(PackProject(title="First dirty"), None, True)
    second = workspace.add_project(PackProject(title="Second dirty"), dirty=True)
    assert not workspace.close()
    box = workspace._decisions[-1]
    assert box.property("projectId") == first.session.id
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    box = workspace._decisions[-1]
    assert box.property("projectId") == second.session.id
    first.title_edit.setText("New edits after discard decision")
    first._commit_editors()
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: any(
        decision.property("projectId") == first.session.id for decision in workspace._decisions
    ))
    assert workspace.isVisible()
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert first.dirty
    assert first.project.title == "New edits after discard decision"
    assert not workspace._closing


def test_cancelling_queued_close_save_keeps_workspace_open_and_retryable(
    workspace, qtbot, tmp_path, monkeypatch,
):
    editor = workspace.active_editor
    editor._set_project(PackProject(title="Unsaved"), None, True)
    release = threading.Event()
    blocker = workspace.job_manager.submit(
        editor.session.id, "recovery", "Held recovery", lambda _ctx: release.wait(5),
        resource_class="io", resource_keys=(f"document-save:{editor.session.id}",),
    )
    qtbot.waitUntil(lambda: blocker.record.state == "running")
    destination = tmp_path / "saved.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
    workspace.close()
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Save), Qt.MouseButton.LeftButton)
    save = next(record for record in workspace.job_manager.tasks(editor.session.id) if record.kind == "save")
    assert save.active and save.state != "running"
    workspace.tasks_panel.show()
    workspace.tasks_panel.refresh()
    workspace.tasks_panel.table.selectRow(0)
    qtbot.waitUntil(workspace.tasks_panel.cancel_button.isVisible)
    qtbot.mouseClick(workspace.tasks_panel.cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: workspace.job_manager.handle(save.id).record.state == "cancelled")
    qtbot.waitUntil(lambda: not workspace._closing)
    assert editor.dirty and workspace.isVisible()
    assert not destination.exists()
    release.set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    workspace.close()
    box = workspace._decisions[-1]
    assert box.property("projectId") == editor.session.id
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)


def test_source_probe_applies_latest_request_only_to_its_own_document(
    workspace, qtbot, tmp_path, monkeypatch,
):
    first = workspace.active_editor
    first._set_project(PackProject(title="A"), None, True)
    older, newer = tmp_path / "older.mp4", tmp_path / "newer.mp4"
    old_started, old_release = threading.Event(), threading.Event()

    class ProbeMedia(QuietMedia):
        def probe(self, path):
            if path == older:
                old_started.set()
                assert old_release.wait(5)
            return SimpleNamespace(
                duration=2, video_codec="h264", audio_codec="aac", pixel_format="yuv420p",
                audio_sample_rate=48000, audio_channels=2, fps=24, height=240,
            )

    first.media = ProbeMedia()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(older), ""))
    first.choose_source_video()
    qtbot.waitUntil(old_started.is_set)
    assert first.project.video_path == ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(newer), ""))
    first.choose_source_video()
    second = workspace.add_project(PackProject(title="B"), dirty=False)
    qtbot.waitUntil(lambda: first.project.video_path == str(newer))
    old_release.set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert first.project.video_path == str(newer)
    assert second.project.video_path == ""
    assert workspace.active_editor is second


def test_backing_completion_keeps_newer_choice_and_never_targets_active_tab(
    workspace, qtbot, tmp_path, monkeypatch,
):
    from choicer_voicer_pack_creator.ui import main_window

    class BackingReview(QDialog):
        def __init__(self, _media, _video, _root, parent, **_kwargs):
            super().__init__(parent)
            self.backing_path = tmp_path / "generated.wav"

    monkeypatch.setattr(main_window, "BackingDialog", BackingReview)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic")
    a = workspace.active_editor
    a._set_project(PackProject(
        title="A", video_path=str(source), video_duration=1,
    ), None, False)
    assert a.generate_backing_track()
    first = a._backing_dialog
    b = workspace.add_project(PackProject(title="B"), dirty=False)
    first.accept()
    assert a.project.backing_track_path == str(tmp_path / "generated.wav")
    assert b.project.backing_track_path == ""
    assert workspace.active_editor is b
    a.project.backing_track_path = ""
    assert a.generate_backing_track()
    second = a._backing_dialog
    chosen = tmp_path / "chosen.wav"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(chosen), ""))
    a.choose_backing_track()
    second.accept()
    assert a.project.backing_track_path == str(chosen)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())


def test_discarded_tab_reopens_saved_contents_not_hidden_edits(
    workspace, qtbot, tmp_path,
):
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    workspace.open_path(path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    editor = workspace.active_editor
    editor.title_edit.setText("Discard this")
    editor._commit_editors()
    old_id = editor.session.id
    workspace.close_project_tab(workspace.tabs.indexOf(editor))
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: old_id not in workspace.editors)
    workspace.open_path(path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert workspace.project.title == "Saved"
    assert workspace.active_editor.session.id != old_id


def test_legacy_recovery_is_preserved_on_dismissal_and_migrates_after_acceptance(qtbot, tmp_path):
    recovery = RecoveryStore(tmp_path / "recovery-v2.json")
    recovery.save(PackProject(title="Legacy edits"), None)
    windows = []
    for accept in (False, True):
        window = MainWindow(
            QuietMedia(),
            settings=QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat),
            recovery_store=recovery, analysis_data_root=tmp_path / "analysis",
        )
        windows.append(window)
        qtbot.addWidget(window)
        window.show()
        qtbot.waitUntil(lambda window=window: bool(window._decisions))
        box = next(box for box in window._decisions if box.windowTitle() == "Recover previous workspace?")
        qtbot.mouseClick(
            box.button(QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No),
            Qt.MouseButton.LeftButton,
        )
        qtbot.waitUntil(lambda window=window: not window.job_manager.active_jobs())
        if accept:
            assert not recovery.path.exists()
            assert window.project.title == "Legacy edits"
            assert window.active_editor.recovery_store.load().project.title == "Legacy edits"
        else:
            assert recovery.load().project.title == "Legacy edits"
        window.workspace_store = None
        for editor in window.editors.values():
            editor._recovery_timer.stop()
            editor.dirty = False
        window.close()


@pytest.mark.integration
def test_visible_tabs_remain_usable_during_real_ffmpeg_export(qtbot, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator.export_progress import ExportProgress
    from choicer_voicer_pack_creator.exporter import PackExporter
    from choicer_voicer_pack_creator.media import MediaTools

    media = MediaTools()
    video, backing = tmp_path / "synthetic.mp4", tmp_path / "backing.wav"
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "3", "-c:v", "mpeg4", "-c:a", "aac", str(video),
    ], "Creating synthetic workspace fixture")
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", "3", str(backing),
    ], "Creating synthetic backing fixture")
    window = MainWindow(
        media, settings=QSettings(str(tmp_path / "native.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    window.show()
    a = window.active_editor
    a._set_project(PackProject(
        title="Synthetic A", authors=["Synthetic fixture"],
        video_path=str(video), video_duration=3, backing_track_path=str(backing),
        video_height=240, video_fps=24, segments=[Segment(0.4, 1.3, "Synthetic line", ["Actor"])],
    ), None, True)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    released = threading.Event()
    exporter = PackExporter(media)

    class HeldExporter:
        def export(self, project, destination, *, create_zip, progress):
            progress(ExportProgress("Ready for concurrent workspace interaction"))
            assert released.wait(10)
            return exporter.export(project, destination, create_zip=create_zip, progress=progress)

    a.exporter = HeldExporter()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    a.action_export.trigger()
    qtbot.waitUntil(lambda: a._export_worker is not None and any(
        record.kind == "export" and record.state == "running"
        for record in window.job_manager.tasks(a.session.id)
    ))
    b = window.add_project(PackProject(title="B", authors=["Synthetic fixture"]), dirty=False)
    b.title_edit.selectAll()
    qtbot.keyClicks(b.title_edit, "Editable while A exports")
    saved = tmp_path / "B.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(saved), ""))
    b.action_save.trigger()
    qtbot.waitUntil(lambda: not b.dirty and saved.is_file())
    c = tmp_path / "C.cvpack.json"
    ProjectStore.save(PackProject(title="Opened C", authors=["Synthetic fixture"]), c)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(c), ""))
    b.action_open.trigger()
    qtbot.waitUntil(lambda: window.active_editor.project.title == "Opened C")
    bar = window.tabs.tabBar()
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(window.tabs.indexOf(a)).center())
    assert window.active_editor is a
    a._export_dialog.close()
    assert not a._export_dialog.isVisible()
    assert a._export_worker.isRunning()
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(window.tabs.indexOf(b)).center())
    assert window.active_editor is b
    assert b.title_edit.text() == "Editable while A exports"
    assert window.grab().save(str(tmp_path / "workspace-during-export.png"))
    released.set()
    qtbot.waitUntil(lambda: a._export_worker is None, timeout=60000)
    exported = next(record for record in window.job_manager.tasks(a.session.id) if record.kind == "export")
    assert exported.state == "succeeded", exported.error
    assert exported.result.pack_path.is_dir()
    assert exported.result.zip_path.is_file()
    assert window.active_editor is b
    for editor in window.editors.values():
        editor.dirty = False
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
