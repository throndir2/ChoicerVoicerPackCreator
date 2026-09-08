from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QLabel, QMessageBox

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.separation import (
    SeparationCancelled,
    SeparationDownloadRequired,
)
from choicer_voicer_pack_creator.separation_types import KEEP_SINGING, REMOVE_ALL_VOCALS
from choicer_voicer_pack_creator.ui import backing_dialog, main_window


def test_workspace_backing_download_consent_survives_hidden_review(qtbot, tmp_path, monkeypatch):
    calls = []
    started, release = threading.Event(), threading.Event()
    output = tmp_path / "backing.wav"
    output.write_bytes(b"backing")

    class Manager:
        model_download_bytes = 1024**2

        def __init__(self, _root, *, mode):
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
        auto_start=True,
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
        def __init__(self, _root, *, mode):
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
        auto_start=True,
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

        def __init__(self, _root, *, mode):
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
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, auto_start=True,
    )
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

        def __init__(self, _root, *, mode):
            pass

        def generate(self, *_args, allow_download, **_kwargs):
            calls.append(allow_download)
            raise SeparationDownloadRequired("Damaged cache needs consent")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel,
    )
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, auto_start=True,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: bool(calls) and dialog.worker is None and not dialog.isVisible())
    assert calls == [False]
    assert dialog.backing_path is None


@pytest.mark.parametrize("failure", ["exception", "no-result"])
def test_failed_generation_can_be_retried_without_losing_dialog(qtbot, tmp_path, monkeypatch, failure):
    class Manager:
        def __init__(self, _root, *, mode):
            pass

        def generate(self, *_args, **_kwargs):
            if failure == "exception":
                raise RuntimeError("Model failed")
            return object()

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, auto_start=True,
    )
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
        def __init__(self, _root, *, mode):
            pass

        def generate(self, *_args, cancelled, **_kwargs):
            started.set()
            assert allow_finish.wait(5)
            assert cancelled()
            raise SeparationCancelled("Canceled")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, auto_start=True,
    )
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
    monkeypatch.setattr(backing_dialog, "SeparationManager", lambda _root, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        backing_dialog, "BackingWorker", lambda *_args, **_kwargs: pytest.fail("Dialog was closed"),
    )
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, auto_start=True,
    )
    qtbot.addWidget(dialog)
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert (
        "Separation may leave voices or remove some effects. Audio stays on this computer."
    ) in labels
    assert not any("captions, speakers, timings" in text for text in labels)
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
            self.mode = _kwargs["mode"]
            self.before_start = _kwargs["before_start"]

        def show(self):
            super().show()
            assert self.before_start(self.mode)
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
            self.mode = _kwargs["mode"]
            self.before_start = _kwargs["before_start"]

        def show(self):
            super().show()
            assert self.before_start(self.mode)
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


@pytest.mark.parametrize("initial_mode", [REMOVE_ALL_VOCALS, KEEP_SINGING])
def test_manual_picker_waits_for_generate_and_freezes_request(
    qtbot, tmp_path, monkeypatch, initial_mode,
):
    modes, calls = [], []

    class Manager:
        def __init__(self, _root, *, mode):
            modes.append(mode)

        def generate(self, *_args, **_kwargs):
            calls.append(True)
            raise RuntimeError("Not enough memory")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    dialog = backing_dialog.BackingDialog(
        SimpleNamespace(), tmp_path / "video.mp4", tmp_path, mode=initial_mode,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)
    assert not calls and not modes
    assert dialog.generate_button.isVisible()
    assert dialog.keep_singing_choice.isChecked() is (initial_mode == KEEP_SINGING)
    dialog.keep_singing_choice.setChecked(True)
    assert dialog.license_warning.isVisible()
    assert "CC BY-NC 4.0" in dialog.license_warning.text()
    assert "non-commercial" in dialog.license_warning.text()
    assert "About" in dialog.license_warning.text()
    dialog.remove_vocals_choice.setChecked(True)
    assert not dialog.license_warning.isVisible()
    dialog.keep_singing_choice.setChecked(True)
    assert not modes
    dialog.generate_button.click()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert modes == [KEEP_SINGING]
    assert not dialog.keep_singing_choice.isEnabled()
    assert not dialog.remove_vocals_choice.isEnabled()
    assert not dialog.generate_button.isVisible()
    dialog.retry_button.click()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert calls == [True, True]
    assert modes == [KEEP_SINGING]
    assert dialog.mode == KEEP_SINGING
    dialog.close()


def prepare_mode_window(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    monkeypatch.setattr(window, "_commit_editors", lambda: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.edit_history.reset(dirty=False)
    window.processing.reset()
    return window


@pytest.mark.parametrize("dismiss", ["button", "close", "escape"])
def test_opening_selecting_and_canceling_picker_does_not_change_project(
    qtbot, tmp_path, monkeypatch, dismiss,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        backing_dialog, "SeparationManager", lambda *_args, **_kwargs: pytest.fail("Not started"),
    )
    before = window.project.to_dict()
    processing_before = window.processing.group_state("backing")
    assert window.generate_backing_track()
    dialog = window._backing_dialog
    dialog.keep_singing_choice.setChecked(True)
    qtbot.wait(20)
    assert window.project.to_dict() == before
    assert not window.dirty
    if dismiss == "button":
        dialog.close_button.click()
    elif dismiss == "close":
        dialog.close()
    else:
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert window._backing_dialog is None
    assert window.project.to_dict() == before
    assert not window.edit_history.history.can_undo
    assert window.processing.group_state("backing") == processing_before
    window.close()


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
def test_explicit_mode_preference_survives_failure_and_cancel_with_old_backing(
    qtbot, tmp_path, monkeypatch, outcome,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    started = threading.Event()
    modes = []

    class Manager:
        def __init__(self, _root, *, mode):
            modes.append(mode)

        def generate(self, *_args, cancelled, **_kwargs):
            started.set()
            if outcome == "failure":
                raise RuntimeError("Insufficient memory")
            while not cancelled():
                threading.Event().wait(0.01)
            raise SeparationCancelled("Canceled")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    before = window.project.to_dict()
    try:
        assert window.generate_backing_track()
        dialog = window._backing_dialog
        dialog.keep_singing_choice.setChecked(True)
        assert window.project.to_dict() == before
        dialog.generate_button.click()
        qtbot.waitUntil(started.is_set)
        assert window.project.to_dict() == {**before, "backing_generation_mode": KEEP_SINGING}
        assert window.dirty
        assert window.edit_history.history.labels == ("Change backing generation mode",)
        if outcome == "cancel":
            dialog.cancel_generation()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert window.project.backing_track_path == before["backing_track_path"]
        assert window.project.backing_generation_mode == KEEP_SINGING
        assert modes == [KEEP_SINGING]
        assert (tmp_path / "silent.mp3").read_bytes() == b"original silent backing"
        if outcome == "failure":
            dialog.retry_button.click()
            qtbot.waitUntil(lambda: dialog.worker is None)
            assert modes == [KEEP_SINGING]
            assert window.edit_history.history.labels == ("Change backing generation mode",)
            dialog.cancel_generation()
        assert window.generate_backing_track()
        assert window._backing_dialog.keep_singing_choice.isChecked()
        assert window._backing_dialog.worker is None
        window._backing_dialog.cancel_generation()
    finally:
        window.dirty = False
        window.close()


def test_background_generation_uses_old_backend_without_changing_manual_preference(
    qtbot, tmp_path, monkeypatch,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    window.project.backing_track_path = ""
    window.project.backing_generation_mode = KEEP_SINGING
    window.edit_history.reset(dirty=False)
    modes = []

    class Manager:
        def __init__(self, _root, *, mode):
            modes.append(mode)

        def generate(self, *_args, **_kwargs):
            raise RuntimeError("Not enough memory")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    try:
        assert window.generate_backing_track(background=True)
        dialog = window._backing_dialog
        assert not dialog.isVisible()
        qtbot.waitUntil(lambda: bool(modes) and dialog.worker is None)
        assert modes == [REMOVE_ALL_VOCALS]
        assert dialog.remove_vocals_choice.isChecked()
        assert not dialog.keep_singing_choice.isEnabled()
        assert window.project.backing_generation_mode == KEEP_SINGING
        assert not window.dirty
        dialog.start()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert modes == [REMOVE_ALL_VOCALS]
        assert not window.edit_history.history.can_undo
        dialog.cancel_generation()
    finally:
        window.dirty = False
        window.close()


def test_background_never_regenerates_selected_backing(qtbot, tmp_path, monkeypatch):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: pytest.fail("No confirmation needed"))
    monkeypatch.setattr(
        main_window, "BackingDialog", lambda *_args, **_kwargs: pytest.fail("Keep existing backing"),
    )
    assert not window.generate_backing_track(background=True)
    assert window.project.backing_track_path == str(tmp_path / "silent.mp3")
    window.close()


@pytest.mark.parametrize("change", ["source", "source-request", "project"])
def test_obsolete_picker_cannot_start_or_persist_mode(qtbot, tmp_path, monkeypatch, change):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        backing_dialog, "SeparationManager", lambda *_args, **_kwargs: pytest.fail("Obsolete picker"),
    )
    assert window.generate_backing_track()
    dialog = window._backing_dialog
    dialog.keep_singing_choice.setChecked(True)
    if change == "source":
        window.session.source_revision += 1
    elif change == "source-request":
        window._source_request += 1
    else:
        window.project = PackProject(video_path=window.project.video_path)
    before = window.project.to_dict()
    dialog.generate_button.click()
    assert dialog.worker is None
    assert window.project.to_dict() == before
    assert not window.dirty
    dialog.cancel_generation()
    window.close()


@pytest.mark.parametrize("setup_failure", [False, True])
def test_failed_request_retry_keeps_mode_and_cannot_replace_superseded_preference(
    qtbot, tmp_path, monkeypatch, setup_failure,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    modes, calls = [], []

    class Manager:
        def __init__(self, _root, *, mode):
            modes.append(mode)
            if setup_failure:
                raise RuntimeError("Install the optional CPU runtime")

        def generate(self, *_args, **_kwargs):
            calls.append(True)
            raise RuntimeError("Not enough memory")

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    try:
        assert window.generate_backing_track()
        dialog = window._backing_dialog
        dialog.keep_singing_choice.setChecked(True)
        dialog.generate_button.click()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert window.project.backing_generation_mode == KEEP_SINGING
        assert modes == [KEEP_SINGING]
        assert dialog.retry_button.isVisible()
        if not setup_failure:
            job = next(job for job in window.job_manager.tasks() if job.kind == "backing")
            assert window.tasks_window._can_retry(job.id)
        window.project.backing_generation_mode = REMOVE_ALL_VOCALS
        before = window.project.to_dict()
        if not setup_failure:
            assert not window.tasks_window._can_retry(job.id)
            window.tasks_window._retry[job.id]()
        dialog.retry_button.click()
        assert modes == [KEEP_SINGING]
        assert calls == ([] if setup_failure else [True])
        assert window.project.to_dict() == before
        assert "no longer matches" in dialog.progress_label.text()
        dialog.cancel_generation()
    finally:
        window.dirty = False
        window.close()


@pytest.mark.parametrize("confirm_new", [True, False])
def test_start_captures_latest_backing_selection_and_reconfirms_replacement(
    qtbot, tmp_path, monkeypatch, confirm_new,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    calls, confirmations = [], []
    output = tmp_path / "generated.wav"
    output.write_bytes(b"new backing")
    replacement = tmp_path / "chosen.wav"
    replacement.write_bytes(b"chosen backing")

    def confirm(*_args):
        confirmations.append(True)
        return (
            QMessageBox.StandardButton.Yes if len(confirmations) == 1 or confirm_new
            else QMessageBox.StandardButton.Cancel
        )

    class Manager:
        def __init__(self, _root, *, mode):
            pass

        def generate(self, *_args, **_kwargs):
            calls.append(True)
            return output

    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    try:
        assert window.generate_backing_track()
        dialog = window._backing_dialog
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(replacement), ""))
        window.choose_backing_track()
        revision = window.session.backing_revision
        dialog.generate_button.click()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert confirmations == [True, True]
        assert bool(calls) is confirm_new
        if confirm_new:
            assert window.project.backing_track_path == str(output)
            assert dialog.source_snapshot["backing_revision"] == revision
            assert dialog.source_snapshot["backing_path"] == str(replacement)
        else:
            assert window.project.backing_track_path == str(replacement)
            dialog.cancel_generation()
        assert replacement.read_bytes() == b"chosen backing"
    finally:
        window.dirty = False
        window.close()


@pytest.mark.parametrize("change", ["mode", "history", "source", "source-request", "backing"])
def test_successful_stale_generation_keeps_durable_output_without_attaching(
    qtbot, tmp_path, monkeypatch, change,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    output = tmp_path / "generated.wav"
    output.write_bytes(b"new backing")

    class Manager:
        def __init__(self, _root, *, mode):
            pass

        def generate(self, *_args, **_kwargs):
            started.set()
            assert release.wait(10)
            return output

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    try:
        assert window.generate_backing_track()
        dialog = window._backing_dialog
        dialog.keep_singing_choice.setChecked(True)
        dialog.generate_button.click()
        qtbot.waitUntil(started.is_set)
        initial_revision = window.session.backing_revision
        if change == "mode":
            window.project.backing_generation_mode = REMOVE_ALL_VOCALS
        elif change == "history":
            window.action_undo.trigger()
            qtbot.waitUntil(lambda: not window.edit_history.busy)
            assert window.project.backing_generation_mode == REMOVE_ALL_VOCALS
            window.action_redo.trigger()
            qtbot.waitUntil(lambda: not window.edit_history.busy)
            assert window.project.backing_generation_mode == KEEP_SINGING
            assert window.session.backing_revision > initial_revision
        elif change == "source":
            window.session.source_revision += 1
        elif change == "source-request":
            window._source_request += 1
        else:
            window.clear_backing_track()
        preserved = window.project.to_dict()
        processing_before = window.processing.group_state("backing")
        release.set()
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert window.project.to_dict() == preserved
        if change == "backing":
            assert window.processing.group_state("backing") == processing_before
        assert output.read_bytes() == b"new backing"
        assert "newer source/backing choice was kept" in window.statusBar().currentMessage()
    finally:
        release.set()
        qtbot.waitUntil(lambda: dialog.worker is None)
        window.dirty = False
        window.close()


@pytest.mark.parametrize(
    "change", ["mode", "history", "source", "source-request", "backing", "closed", "retained"],
)
def test_consent_and_retry_cannot_revive_obsolete_generation(
    qtbot, tmp_path, monkeypatch, change,
):
    window = prepare_mode_window(qtbot, tmp_path, monkeypatch)
    calls, modes = [], []
    output = tmp_path / "generated.wav"
    output.write_bytes(b"new backing")

    class Manager:
        model_download_bytes = 446680129
        manifest = {"model": {"sha256": "bandit-checksum"}}

        def __init__(self, _root, *, mode):
            modes.append(mode)

        def generate(self, *_args, allow_download, **_kwargs):
            calls.append(allow_download)
            if not allow_download:
                raise SeparationDownloadRequired("Consent required")
            return output

    monkeypatch.setattr(backing_dialog, "SeparationManager", Manager)
    editor = window.active_editor
    try:
        assert editor.generate_backing_track()
        dialog = editor._backing_dialog
        dialog.keep_singing_choice.setChecked(True)
        dialog.generate_button.click()
        qtbot.waitUntil(lambda: dialog._pending_consent)
        consent = dialog._consent_callback
        request = window.setup_consent._requests[0]
        assert request.components == {
            "separation:bandit-checksum":
                "BandIt singing-preserving model (~426 MiB) — CC BY-NC 4.0, non-commercial use only",
        }
        assert "CC BY-NC 4.0" in window.setup_consent.box.text()
        assert not dialog.keep_singing_choice.isEnabled()
        if change == "mode":
            editor.project.backing_generation_mode = REMOVE_ALL_VOCALS
        elif change == "history":
            editor.action_undo.trigger()
            qtbot.waitUntil(lambda: not editor.edit_history.busy)
            editor.action_redo.trigger()
            qtbot.waitUntil(lambda: not editor.edit_history.busy)
        elif change == "source":
            editor.session.source_revision += 1
        elif change == "source-request":
            editor._source_request += 1
        elif change == "backing":
            editor.clear_backing_track()
        elif change in {"closed", "retained"}:
            monkeypatch.setattr(window, "_retire_closed_editors", lambda: None)
            window._hide_editor(editor, retain=change == "retained")
        preserved = editor.project.to_dict()
        if window.setup_consent.box is not None:
            window.setup_consent.box.button(QMessageBox.StandardButton.Yes).click()
        else:
            consent(True)
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert modes == [KEEP_SINGING]
        if change == "retained":
            assert calls == [False, True]
            assert editor.project.backing_track_path == str(output)
        else:
            assert calls == [False]
            dialog.start()
            assert editor.project.to_dict() == preserved
            assert "no longer matches" in dialog.progress_label.text()
            dialog.cancel_generation()
            qtbot.wait(20)
            assert calls == [False]
    finally:
        for item in window.editors.values():
            item.dirty = False
        window.close()
