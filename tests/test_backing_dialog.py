from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.separation import (
    SeparationCancelled,
    SeparationDownloadRequired,
)
from choicer_voicer_pack_creator.ui import backing_dialog, main_window


def test_workspace_backing_download_consent_survives_hidden_review(qtbot, tmp_path, monkeypatch):
    calls = []
    started, release = threading.Event(), threading.Event()
    output = tmp_path / "backing.wav"
    output.write_bytes(b"backing")

    class Manager:
        model_download_bytes = 1024**2

        def __init__(self, _root):
            pass

        def generate(self, *_args, allow_download, cancelled, **_kwargs):
            calls.append(allow_download)
            if not allow_download:
                raise SeparationDownloadRequired("Consent required")
            started.set()
            assert release.wait(5)
            assert not cancelled()
            return output

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: pytest.fail("Modal consent"))
    jobs = JobManager(limits={"cpu": 1})
    details = {}
    host = QDialog()
    qtbot.addWidget(host)
    host.workspace = SimpleNamespace(tasks_window=SimpleNamespace(
        register_detail=lambda job_id, widget: details.__setitem__(job_id, widget),
    ))
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, host,
        job_manager=jobs, project_id="project-a", source_snapshot={"revision": 9},
    )
    qtbot.addWidget(dialog)
    dialog.show()
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(dialog.backing_path))
    active_when_accepted = []
    dialog.accepted.connect(lambda: active_when_accepted.append(bool(jobs.active_jobs())))
    try:
        qtbot.waitUntil(lambda: dialog._pending_consent)
        assert dialog.worker is None
        assert jobs.tasks("project-a")[0].state == "failed"
        assert "Consent required" in jobs.tasks("project-a")[0].error
        box = next(box for box in dialog.findChildren(QMessageBox) if box.isVisible())
        assert not box.isModal()
        assert box.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert calls == [False]
        dialog.close()
        assert not dialog.isVisible()
        box.button(QMessageBox.StandardButton.Yes).click()
        qtbot.waitUntil(started.is_set)
        assert dialog.worker is not None
        assert not dialog.worker.isInterruptionRequested()
        assert jobs.active_jobs("project-a")[0].source_snapshot["revision"] == 9
        assert len(details) == 2
        assert details == {job.id: dialog for job in jobs.tasks("project-a")}
    finally:
        release.set()
        qtbot.waitUntil(lambda: dialog.worker is None)
        jobs.shutdown(wait=True)
    assert calls == [False, True]
    assert accepted == [output]
    assert active_when_accepted == [False]
    assert jobs.tasks("project-a")[-1].state == "succeeded"


def test_workspace_backing_explicit_cancel_stops_job(qtbot, tmp_path, monkeypatch):
    started = threading.Event()

    class Manager:
        def __init__(self, _root):
            pass

        def generate(self, *_args, cancelled, **_kwargs):
            started.set()
            while not cancelled():
                threading.Event().wait(0.01)
            raise SeparationCancelled("Canceled")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    jobs = JobManager(limits={"cpu": 1})
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path,
        job_manager=jobs, project_id="project-a",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        qtbot.waitUntil(started.is_set)
        dialog.close_button.click()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert jobs.tasks("project-a")[0].state == "cancelled"
        assert dialog.backing_path is None
    finally:
        jobs.shutdown(wait=True)


def test_download_consent_retries_only_after_worker_finishes(qtbot, tmp_path, monkeypatch):
    calls = []
    output = tmp_path / "backing.wav"
    output.write_bytes(b"backing")

    class Manager:
        model_download_bytes = 316446953

        def __init__(self, _root):
            pass

        def generate(self, _media, _video, *, allow_download, progress, cancelled):
            calls.append(allow_download)
            progress("Verifying model", None)
            if not allow_download:
                raise SeparationDownloadRequired("Missing model")
            progress("Separating audio", 0.5)
            return output

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    prompts = []

    def consent(parent, title, text, *_args):
        assert parent.worker is None
        prompts.append((title, text))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", consent)
    dialog = backing_dialog.BackingDialog(SimpleNamespace(), tmp_path / "video.mp4", tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted)
    assert calls == [False, True]
    assert len(prompts) == 1
    assert "302 MiB" in prompts[0][1]
    assert dialog.backing_path == output
    assert dialog.worker is None


def test_declining_download_leaves_no_result(qtbot, tmp_path, monkeypatch):
    calls = []

    class Manager:
        model_download_bytes = 100

        def __init__(self, _root):
            pass

        def generate(self, *_args, allow_download, **_kwargs):
            calls.append(allow_download)
            raise SeparationDownloadRequired("Damaged cache needs consent")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel,
    )
    dialog = backing_dialog.BackingDialog(SimpleNamespace(), tmp_path / "video.mp4", tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: bool(calls) and dialog.worker is None and not dialog.isVisible())
    assert calls == [False]
    assert dialog.backing_path is None


@pytest.mark.parametrize("failure", ["exception", "no-result"])
def test_failed_generation_can_be_retried_without_losing_dialog(qtbot, tmp_path, monkeypatch, failure):
    class Manager:
        def __init__(self, _root):
            pass

        def generate(self, *_args, **_kwargs):
            if failure == "exception":
                raise RuntimeError("Model failed")
            return object()

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    dialog = backing_dialog.BackingDialog(SimpleNamespace(), tmp_path / "video.mp4", tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.retry_button.isVisible())
    assert dialog.worker is None
    assert dialog.backing_path is None
    assert "unchanged" in dialog.progress_label.text()
    assert dialog.close_button.text() == "Close"
    assert dialog.progress_bar.format() == "Failed"
    dialog.close()


def test_close_waits_for_canceled_worker(qtbot, tmp_path, monkeypatch):
    started = threading.Event()
    allow_finish = threading.Event()

    class Manager:
        def __init__(self, _root):
            pass

        def generate(self, *_args, cancelled, **_kwargs):
            started.set()
            assert allow_finish.wait(5)
            assert cancelled()
            raise SeparationCancelled("Canceled")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    dialog = backing_dialog.BackingDialog(SimpleNamespace(), tmp_path / "video.mp4", tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        qtbot.waitUntil(started.is_set)
        worker = dialog.worker
        dialog.close()
        assert dialog.isVisible()
        assert dialog.worker is worker
        assert not dialog.close_button.isEnabled()
        assert worker.isInterruptionRequested()
    finally:
        allow_finish.set()
        qtbot.waitUntil(lambda: dialog.worker is None)
    assert not dialog.isVisible()
    assert dialog.backing_path is None


def test_dismissed_dialog_does_not_start_scheduled_worker(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(backing_dialog, "SeparationManager", lambda _root: SimpleNamespace())
    monkeypatch.setattr(
        backing_dialog, "BackingWorker", lambda *_args, **_kwargs: pytest.fail("Dialog was closed"),
    )
    dialog = backing_dialog.BackingDialog(SimpleNamespace(), tmp_path / "video.mp4", tmp_path)
    qtbot.addWidget(dialog)
    dialog.reject()
    qtbot.wait(20)
    assert dialog.worker is None


def make_window(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = main_window.MainWindow(
        SimpleNamespace(), settings=settings, analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    source = tmp_path / "source.ogv"
    source.write_bytes(b"original video")
    prompt = tmp_path / "001.mp3"
    prompt.write_bytes(b"original prompt")
    old_backing = tmp_path / "silent.mp3"
    old_backing.write_bytes(b"original silent backing")
    window.project.video_path = str(source)
    window.project.video_duration = 10
    window.project.backing_track_path = str(old_backing)
    window.project.preserve_source_video = True
    window.project.segments = [
        Segment(1.2, 3.4, "Carefully edited dialogue", ["Nahida"], audio_mode="file",
                audio_path=str(prompt), source_range_known=False),
    ]
    window.project.source_captions = [SourceCaption(1, 3, "Original draft", "YouTube")]
    return window


@pytest.mark.parametrize("accepted", [True, False])
def test_regeneration_changes_only_backing_selection(qtbot, tmp_path, monkeypatch, accepted):
    window = make_window(qtbot, tmp_path)
    output = tmp_path / "generated.wav"
    output.write_bytes(b"music")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(window, "_commit_editors", lambda: None)
    before = window.project.to_dict()
    before_files = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    class Dialog(QDialog):
        backing_path = output

        def __init__(self, *_args, **_kwargs):
            super().__init__(_args[3])

        def show(self):
            super().show()
            QTimer.singleShot(0, self.accept if accepted else self.reject)

    monkeypatch.setattr(main_window, "BackingDialog", Dialog)
    assert window.generate_backing_track()
    qtbot.waitUntil(lambda: window._backing_dialog is None)
    expected = {**before, "backing_track_path": str(output)} if accepted else before
    assert window.project.to_dict() == expected
    for path, content in before_files.items():
        assert path.read_bytes() == content
    assert window.dirty is accepted
    assert window._backing_dialog is None
    window.dirty = False
    window.close()


def test_existing_backing_is_not_replaced_without_confirmation(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    monkeypatch.setattr(window, "_commit_editors", lambda: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel,
    )
    monkeypatch.setattr(
        main_window, "BackingDialog",
        lambda *_args: pytest.fail("Must not generate after declining replacement"),
    )
    before = window.project.to_dict()
    assert not window.generate_backing_track()
    assert window.project.to_dict() == before
    window.close()


def test_late_backing_result_cannot_attach_to_different_project(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    original_project = window.project
    new_project = PackProject(title="Different project")
    output = tmp_path / "generated.wav"
    output.write_bytes(b"music")
    monkeypatch.setattr(window, "_commit_editors", lambda: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes,
    )
    class Dialog(QDialog):
        backing_path = output

        def __init__(self, *_args, **_kwargs):
            super().__init__(_args[3])

        def show(self):
            super().show()
            window.project = new_project
            QTimer.singleShot(0, self.accept)

    monkeypatch.setattr(main_window, "BackingDialog", Dialog)
    assert window.generate_backing_track()
    qtbot.waitUntil(lambda: window._backing_dialog is None)
    assert window.project is new_project
    assert new_project.backing_track_path == ""
    assert original_project.backing_track_path == str(tmp_path / "silent.mp3")
    assert "kept" in window.statusBar().currentMessage()
    assert output.read_bytes() == b"music"
    window.close()


def test_video_import_starts_analysis_without_waiting_for_backing(
    qtbot, tmp_path, monkeypatch,
):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    window = main_window.MainWindow(
        SimpleNamespace(probe=lambda _path: SimpleNamespace(duration=10)),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
    monkeypatch.setattr(main_window.WaveformWorker, "start", lambda _self: None)
    calls = []
    monkeypatch.setattr(
        main_window.ProjectEditor, "generate_backing_track",
        lambda _self, **kwargs: calls.append(("backing", kwargs)),
    )
    monkeypatch.setattr(
        main_window.ProjectEditor, "open_analysis_dialog",
        lambda _self, **_kwargs: calls.append("analysis"),
    )
    window.new_from_video()
    qtbot.waitUntil(lambda: len(calls) == 2)
    assert calls == ["analysis", ("backing", {"background": True})]
    assert window.project.video_path == str(source)
    window.dirty = False
    window.close()


def test_stale_import_handoff_does_not_process_new_project(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    monkeypatch.setattr(
        window, "generate_backing_track", lambda: pytest.fail("Must not process stale import"),
    )
    monkeypatch.setattr(
        window, "open_analysis_dialog", lambda **_kwargs: pytest.fail("Must not analyze stale import"),
    )
    window._finish_new_import(PackProject())
    window.close()


@pytest.mark.parametrize("choice", ["Generate backing", "Export without music", "Cancel"])
def test_export_without_backing_requires_explicit_choice(qtbot, tmp_path, monkeypatch, choice):
    window = make_window(qtbot, tmp_path)
    window.project.backing_track_path = ""
    generated = []
    monkeypatch.setattr(
        window, "generate_backing_track",
        lambda **kwargs: generated.append(kwargs.get("after_success")) or True,
    )

    def click_choice():
        box = next(widget for widget in window.findChildren(QMessageBox) if widget.isVisible())
        next(button for button in box.buttons() if button.text() == choice).click()

    QTimer.singleShot(0, click_choice)
    assert window._confirm_backing_export() is (choice == "Export without music")
    assert bool(generated) is (choice == "Generate backing")
    if generated:
        assert callable(generated[0])
    window.close()


def test_pack_zip_entrypoint_uses_durable_import_directory(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    recovered = PackProject(title="Recovered", segments=list(window.project.segments))
    calls = []
    monkeypatch.setattr(
        type(window.importer), "import_zip",
        lambda _self, archive, parent: calls.append((archive, parent)) or SimpleNamespace(
            project=recovered, warnings=[],
        ),
    )
    archive = tmp_path / "pack.zip"
    window.open_path(archive)
    qtbot.waitUntil(lambda: window.project is recovered)
    assert calls == [(archive, tmp_path / "imported-packs")]
    assert window.project is recovered
    assert window.dirty
    assert window.project.backing_track_path == ""
    window.dirty = False
    window.close()
