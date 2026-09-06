from __future__ import annotations

import time
from threading import Event, current_thread, main_thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QMessageBox

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import SourceCaption
from choicer_voicer_pack_creator.operations import SourceSnapshot
from choicer_voicer_pack_creator.ui import main_window, youtube_dialog
from choicer_voicer_pack_creator.youtube import (
    ExistingYouTubeImport,
    YouTubeCancelled,
    YouTubeDownload,
    YouTubeImportConflict,
)


class UnusedMedia:
    pass


def test_workspace_youtube_hides_without_cancel_and_emits_async_completion(
    qtbot, tmp_path, monkeypatch,
):
    started, release = Event(), Event()
    result = make_download(tmp_path)

    def download(*_args, cancelled, **_kwargs):
        started.set()
        assert release.wait(5)
        assert not cancelled()
        return result

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    jobs = JobManager(limits={"network": 1})
    details = {}
    host = QDialog()
    qtbot.addWidget(host)
    host.workspace = SimpleNamespace(tasks_window=SimpleNamespace(
        register_detail=lambda job_id, widget: details.__setitem__(job_id, widget),
    ))
    dialog = youtube_dialog.YouTubeDialog(
        UnusedMedia(), str(tmp_path), host, job_manager=jobs, project_id="pending-project",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    signals = []
    dialog.download_started.connect(lambda: signals.append("started"))
    dialog.accepted.connect(lambda: signals.append("accepted"))
    active_when_accepted = []
    dialog.accepted.connect(lambda: active_when_accepted.append(bool(jobs.active_jobs())))
    dialog.url_edit.setText(result.url)
    try:
        dialog.start_download()
        qtbot.waitUntil(started.is_set)
        assert signals == ["started"]
        assert details == {dialog.worker.job_handle.id: dialog}
        assert not dialog.isModal()
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        dialog.close()
        assert not dialog.isVisible()
        assert not dialog.worker.isInterruptionRequested()
    finally:
        release.set()
        qtbot.waitUntil(lambda: dialog.worker is None)
        jobs.shutdown(wait=True)
    assert signals == ["started", "accepted"]
    assert dialog.download_result == result
    assert jobs.tasks()[0].project_id == "pending-project"
    assert active_when_accepted == [False]


def test_workspace_youtube_validates_destination_on_worker_and_reports_inline(
    qtbot, tmp_path, monkeypatch,
):
    from pathlib import Path

    folder = tmp_path / "missing-destination"
    original_is_dir = Path.is_dir
    checks = []

    def is_dir(path):
        if path == folder:
            checks.append(current_thread() is main_thread())
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_a: pytest.fail("Modal failure"))
    monkeypatch.setattr(
        youtube_dialog, "download_youtube", lambda *_a, **_kw: pytest.fail("Invalid destination"),
    )
    jobs = JobManager(limits={"network": 1})
    dialog = youtube_dialog.YouTubeDialog(
        UnusedMedia(), str(folder), job_manager=jobs, project_id="pending-project",
    )
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    try:
        dialog.start_download()
        assert dialog.worker is not None
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert checks == [False]
        assert "existing folder" in dialog.progress_label.text()
        assert dialog.download_button.isEnabled()
        assert jobs.tasks()[0].state == "failed"
    finally:
        jobs.shutdown(wait=True)


def test_workspace_youtube_explicit_cancel_removes_queued_download(qtbot, tmp_path, monkeypatch):
    started, release = Event(), Event()
    jobs = JobManager(limits={"network": 1})

    def occupy(_context):
        started.set()
        assert release.wait(5)

    jobs.submit("other-project", "download", "Existing transfer", occupy, resource_class="network")
    monkeypatch.setattr(
        youtube_dialog, "download_youtube", lambda *_a, **_kw: pytest.fail("Canceled queued job ran"),
    )
    dialog = youtube_dialog.YouTubeDialog(
        UnusedMedia(), str(tmp_path), job_manager=jobs, project_id="pending-project",
    )
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    try:
        qtbot.waitUntil(started.is_set)
        dialog.start_download()
        assert jobs.active_jobs("pending-project")[0].state == "queued"
        dialog.cancel_download()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert jobs.tasks("pending-project")[0].state == "cancelled"
        assert dialog.download_result is None
    finally:
        release.set()
        qtbot.waitUntil(lambda: not jobs.active_jobs())
        jobs.shutdown(wait=True)


def make_download(tmp_path):
    folder = tmp_path / "YouTube-abcdefghijk-unique"
    folder.mkdir()
    video = folder / "source.mp4"
    video.write_bytes(b"video")
    return YouTubeDownload(
        video, "Example", 10, "https://www.youtube.com/watch?v=abcdefghijk", "en",
        [SourceCaption(1, 2, "Hello", "YouTube creator (en)")], [],
    )


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("choice", ["reuse", "overwrite", "cancel"])
def test_existing_import_choice_runs_after_worker_and_does_not_block_jobs(
    qtbot, tmp_path, monkeypatch, managed, choice,
):
    result = make_download(tmp_path)
    existing = ExistingYouTubeImport(
        result.video_path.parent, SourceSnapshot.capture((result.video_path.parent,)),
        result.video_path, result.duration, "",
    )
    calls = []

    def download(*_args, existing, overwrite, **_kwargs):
        calls.append((existing, overwrite))
        return YouTubeImportConflict((candidate,)) if existing is None else result

    candidate = existing
    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    jobs = JobManager(limits={"network": 1}) if managed else None
    dialog = youtube_dialog.YouTubeDialog(
        UnusedMedia(), str(tmp_path), job_manager=jobs, project_id="pending-project",
    )
    qtbot.addWidget(dialog)
    dialog.url_edit.setText(result.url)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    try:
        dialog.start_download()
        qtbot.waitUntil(lambda: dialog._conflict_dialog is not None)
        prompt = dialog._conflict_dialog
        assert dialog.worker is None
        assert not prompt.isModal()
        assert prompt.reuse_button.isEnabled()
        assert not accepted
        assert calls == [(None, False)]
        if jobs:
            assert not jobs.active_jobs()
            job = jobs.submit(
                "another-project", "test", "Independent work", lambda _context: True,
                resource_class="network", write_paths=(tmp_path,),
            )
            qtbot.waitUntil(lambda: not job.record.active)
            assert job.record.state == "succeeded"
        if choice == "cancel":
            prompt.reject()
            assert calls == [(None, False)]
            assert dialog.download_result is None
            assert dialog.download_button.isEnabled()
            assert not accepted
        else:
            button = prompt.reuse_button if choice == "reuse" else prompt.overwrite_button
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
            qtbot.waitUntil(lambda: bool(accepted))
            assert calls == [(None, False), (existing, choice == "overwrite")]
            assert dialog.download_result == result
            assert dialog.worker is None
        existing.snapshot.verify()
    finally:
        if jobs:
            jobs.shutdown(wait=True)


def test_conflict_dialog_disables_reuse_for_incomplete_selected_folder(qtbot, tmp_path):
    result = make_download(tmp_path)
    snapshot = SourceSnapshot.capture((result.video_path.parent,))
    incomplete = ExistingYouTubeImport(
        result.video_path.parent, snapshot, None, 0, "No complete source video.",
    )
    complete = ExistingYouTubeImport(
        result.video_path.parent, snapshot, result.video_path, result.duration, "",
    )
    parent = QDialog()
    qtbot.addWidget(parent)
    dialog = youtube_dialog.YouTubeConflictDialog(
        YouTubeImportConflict((incomplete, complete)), parent,
    )
    qtbot.addWidget(dialog)
    assert dialog.selected_import == complete
    assert dialog.reuse_button.isEnabled()
    dialog.folder_combo.setCurrentIndex(0)
    assert not dialog.reuse_button.isEnabled()
    assert dialog.overwrite_button.isEnabled()
    assert dialog.status_label.text() == incomplete.reuse_problem


@pytest.mark.parametrize("cancel_parent", [False, True])
def test_conflict_choice_cannot_resume_canceled_or_stale_import(
    qtbot, tmp_path, monkeypatch, cancel_parent,
):
    result = make_download(tmp_path)
    existing = ExistingYouTubeImport(
        result.video_path.parent, SourceSnapshot.capture((result.video_path.parent,)),
        result.video_path, result.duration, "",
    )
    calls = []
    monkeypatch.setattr(
        youtube_dialog, "download_youtube",
        lambda *_args, **_kwargs: calls.append(True) or YouTubeImportConflict((existing,)),
    )
    current = True
    monkeypatch.setattr(youtube_dialog, "_current_dialog_request", lambda _dialog: lambda: current)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog._conflict_dialog is not None)
    prompt = dialog._conflict_dialog
    if cancel_parent:
        dialog.cancel_download()
        assert dialog.result() == QDialog.DialogCode.Rejected
    else:
        current = False
        prompt.reuse_button.click()
        assert "project or source changed" in dialog.progress_label.text()
    assert calls == [True]
    assert dialog.worker is None
    assert dialog._conflict_dialog is None
    assert dialog.download_result is None


def test_download_dialog_waits_for_worker_before_accepting(qtbot, tmp_path, monkeypatch):
    result = make_download(tmp_path)
    monkeypatch.setattr(youtube_dialog, "download_youtube", lambda *_args, **_kwargs: result)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.download_result == result
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.progress_bar.value() == 1000
    assert dialog.progress_bar.format() == "Ready"


def test_cancel_waits_for_worker_and_does_not_accept_partial_download(
    qtbot, tmp_path, monkeypatch,
):
    def download(*_args, cancelled, progress, **_kwargs):
        while not cancelled():
            time.sleep(0.01)
        progress("Merging downloaded media...", None)
        raise YouTubeCancelled("Canceled")

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog._progress("Previous transfer", 900)
    dialog.start_download()
    assert dialog.progress_bar.maximum() == 0
    assert "Fetching" in dialog.progress_label.text()
    dialog.reject()
    dialog._progress("Late progress", 950)
    assert dialog.progress_label.text().startswith("Canceling")
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.download_result is None
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Canceled"


def test_dialog_labels_indeterminate_stages_and_restores_transfer_percentage(qtbot, tmp_path):
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog._progress("Downloading YouTube video — total size unknown", -1)
    assert dialog.progress_bar.maximum() == 0
    assert "not measurable" in dialog.progress_label.text()
    dialog._progress("Downloading YouTube audio — estimated combined transfer progress", 940)
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 940
    assert dialog.progress_bar.format() == "Transfers: %p%"
    assert "estimated" in dialog.progress_label.text()
    for stage in ("Merging", "Checking", "Publishing"):
        dialog._progress(stage, -1)
        assert dialog.progress_bar.maximum() == 0
        assert "not measurable" in dialog.progress_label.text()


def test_worker_does_not_round_unfinished_transfers_to_100_percent(qtbot, tmp_path, monkeypatch):
    result = make_download(tmp_path)

    def download(*_args, progress, **_kwargs):
        for value in (None, 0, 0.5, 0.9999, 1):
            progress("Progress", value)
        return result

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    worker = youtube_dialog.YouTubeWorker(UnusedMedia(), result.url, tmp_path, "auto")
    values = []
    worker.progress.connect(lambda _message, value: values.append(value))
    worker.run()
    assert values == [-1, 0, 500, 999, 1000]


def test_download_failure_stops_indeterminate_progress_and_allows_retry(qtbot, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: errors.append(_args[-1]))

    def download(*_args, progress, **_kwargs):
        progress("Merging downloaded media...", None)
        raise OSError("Merge failed")

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert errors == ["Merge failed\n\nUse Save Diagnostic Bundle to collect logs for support."]
    assert dialog.download_result is None
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Failed"
    assert dialog.download_button.isEnabled()


def test_invalid_url_never_starts_worker(qtbot, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://example.com/video")
    dialog.start_download()
    assert dialog.worker is None
    assert warnings


def test_main_window_loads_download_and_starts_caption_comparison(
    qtbot, tmp_path, monkeypatch,
):
    result = make_download(tmp_path)

    class ImportDialog(QDialog):
        download_result = result

        def __init__(self, *_args, **_kwargs):
            super().__init__(_args[2])

        def show(self):
            super().show()
            QTimer.singleShot(0, self.accept)

    monkeypatch.setattr(main_window, "YouTubeDialog", ImportDialog)
    window = main_window.MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")
    qtbot.addWidget(window)
    scans = []
    backing_runs = []
    monkeypatch.setattr(
        main_window.ProjectEditor, "generate_backing_track",
        lambda editor: backing_runs.append(editor.project.video_path),
    )
    monkeypatch.setattr(
        main_window.ProjectEditor, "open_analysis_dialog",
        lambda _self, **kwargs: scans.append(kwargs),
    )
    window.new_from_youtube()
    qtbot.waitUntil(lambda: bool(scans))
    assert scans == [{"initial_scan": True, "auto_start": True}]
    assert backing_runs == [str(result.video_path)]
    assert window.project.video_path == str(result.video_path)
    assert window.project.source_captions == result.captions
    assert window.project.source_url == result.url
    assert window.dirty
    window._set_busy(True, "Exporting")
    assert not window.action_export.isEnabled()
    assert window.action_new.isEnabled()
    assert window.action_youtube.isEnabled()
    assert window.editor_splitter.isEnabled()
    window._set_busy(False, "Ready")
    assert window.action_export.isEnabled()
    window.dirty = False
    window.close()


@pytest.mark.parametrize("clear_field", [False, True])
def test_unspecified_download_folder_defaults_to_windows_downloads(
    qtbot, tmp_path, monkeypatch, clear_field,
):
    downloads = tmp_path / "Downloads"
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(downloads)
    )
    result = make_download(tmp_path)
    folders = []

    def download(_media, _url, folder, *_args, **_kwargs):
        folders.append(folder)
        return result

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), "")
    qtbot.addWidget(dialog)
    assert dialog.folder_edit.text() == str(downloads)
    assert not downloads.exists()
    if clear_field:
        dialog.folder_edit.setText("   ")
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert downloads.is_dir()
    assert folders == [downloads]
    assert dialog.folder_edit.text() == str(downloads)


def test_download_default_does_not_replace_explicit_or_remembered_folder(
    qtbot, tmp_path, monkeypatch,
):
    downloads = tmp_path / "Downloads"
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(downloads)
    )
    result = make_download(tmp_path)
    folders = []
    monkeypatch.setattr(
        youtube_dialog, "download_youtube",
        lambda _media, _url, folder, *_args, **_kwargs: folders.append(folder) or result,
    )
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(custom))
    qtbot.addWidget(dialog)
    assert dialog.folder_edit.text() == str(custom)
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert folders == [custom]
    assert not downloads.exists()


def test_invalid_custom_destination_is_reported_instead_of_silently_using_downloads(
    qtbot, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(tmp_path)
    )
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: errors.append(_args[-1]))
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path / "missing"))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog.start_download()
    assert dialog.worker is None
    assert errors == ["The media destination must be an existing folder."]
