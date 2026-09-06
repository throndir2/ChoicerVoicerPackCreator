from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, QSignalBlocker, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.separation import (
    SeparationCancelled,
    SeparationDownloadRequired,
)
from choicer_voicer_pack_creator.ui import backing_dialog, main_window


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
    with QSignalBlocker(window.preserve_video_check):
        window.preserve_video_check.setChecked(True)
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

    class Dialog:
        backing_path = output

        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_window, "BackingDialog", Dialog)
    assert window.generate_backing_track() is accepted
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
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))

    class Dialog:
        backing_path = output

        def __init__(self, *_args):
            pass

        def exec(self):
            window.project = new_project
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "BackingDialog", Dialog)
    assert not window.generate_backing_track()
    assert window.project is new_project
    assert new_project.backing_track_path == ""
    assert original_project.backing_track_path == str(tmp_path / "silent.mp3")
    assert "not attached" in warnings[0]
    assert output.read_bytes() == b"music"
    window.close()


def test_video_import_generates_before_analysis_even_when_generation_canceled(
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
    monkeypatch.setattr(window, "generate_backing_track", lambda: calls.append("backing"))
    monkeypatch.setattr(
        window, "open_analysis_dialog", lambda **_kwargs: calls.append("analysis"),
    )
    window.new_from_video()
    qtbot.waitUntil(lambda: len(calls) == 2)
    assert calls == ["backing", "analysis"]
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
    monkeypatch.setattr(window, "generate_backing_track", lambda: generated.append(True) or True)

    def click_choice():
        box = next(widget for widget in window.findChildren(QMessageBox) if widget.isVisible())
        next(button for button in box.buttons() if button.text() == choice).click()

    QTimer.singleShot(0, click_choice)
    assert window._confirm_backing_export() is (choice != "Cancel")
    assert bool(generated) is (choice == "Generate backing")
    window.close()


def test_pack_zip_entrypoint_uses_durable_import_directory(qtbot, tmp_path, monkeypatch):
    window = make_window(qtbot, tmp_path)
    recovered = PackProject(title="Recovered", segments=list(window.project.segments))
    calls = []
    monkeypatch.setattr(
        window.importer, "import_zip",
        lambda archive, parent: calls.append((archive, parent)) or SimpleNamespace(
            project=recovered, warnings=[],
        ),
    )
    archive = tmp_path / "pack.zip"
    window.open_path(archive)
    assert calls == [(archive, tmp_path / "imported-packs")]
    assert window.project is recovered
    assert window.dirty
    assert "Missing music?" in window.statusBar().currentMessage()
    window.dirty = False
    window.close()
