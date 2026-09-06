from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog, QFileDialog

from choicer_voicer_pack_creator.export_progress import (
    VIDEO_CONVERSION_STEP,
    ExportProgress,
    ExportStep,
)
from choicer_voicer_pack_creator.exporter import ExportResult
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui import main_window
from choicer_voicer_pack_creator.ui.export_dialog import ExportProgressDialog


def export_result(tmp_path: Path, *, create_zip: bool = True) -> ExportResult:
    return ExportResult(
        tmp_path / "Pack", tmp_path / "Pack.zip" if create_zip else None,
        {"clip_count": 2, "file_count": 10}, {},
        ["The export succeeded, but an old backup could not be removed."],
    )


def test_dialog_shows_live_operation_history_and_elapsed_time(qtbot, tmp_path, monkeypatch):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    elapsed = [3605000]
    monkeypatch.setattr(dialog, "_elapsed", SimpleNamespace(elapsed=lambda: elapsed[0]))
    dialog.report_progress(ExportProgress("Prompt 2/8: preparing still image..."))
    elapsed[0] += 3000
    dialog._timer.timeout.emit()
    assert dialog.progress_label.text() == "Prompt 2/8: preparing still image..."
    assert "Prompt 2/8" in dialog.details.toPlainText()
    assert "[01:00:05]" not in dialog.details.toPlainText()
    assert dialog.elapsed_label.text() == "Elapsed: 01:00:08 | Current step: 00:00:03"
    assert dialog.details.isReadOnly()
    assert dialog.progress_bar.maximum() == 0
    assert dialog.isModal()
    assert dialog.progress_label.textFormat() == Qt.TextFormat.PlainText
    dialog.report_progress(ExportProgress("Testing ZIP integrity..."))
    assert "Current step: 00:00:00" in dialog.elapsed_label.text()
    assert "Prompt 2/8" in dialog.details.toPlainText()
    dialog.show_error("Fixture stopped")
    dialog.worker_finished()


@pytest.mark.parametrize("create_zip", [True, False])
def test_success_stays_visible_and_cannot_close_until_worker_finishes(
    qtbot, tmp_path, create_zip,
):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    result = export_result(tmp_path, create_zip=create_zip)
    for completed in (False, True):
        if completed:
            dialog.show_result(result)
        assert not dialog.close_button.isEnabled()
        dialog.reject()
        dialog.accept()
        dialog.done(QDialog.DialogCode.Accepted)
        dialog.close()
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.isVisible()

    dialog.worker_finished()
    assert dialog.isVisible()
    assert dialog.close_button.isEnabled()
    assert not dialog._timer.isActive()
    assert dialog.progress_label.text() == "Export complete with cleanup notes"
    assert dialog.progress_bar.value() == dialog.progress_bar.maximum() == 1
    details = dialog.details.toPlainText()
    assert str(result.pack_path) in details
    assert "2 prompts / 10 files" in details
    assert result.warnings[0] in details
    assert ("Validated ZIP:" in details) is create_zip
    qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible()


def test_failure_keeps_last_operation_and_full_error_without_claiming_success(qtbot, tmp_path):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.report_progress(ExportProgress("Revalidating published pack: checking audio"))
    dialog.show_error("Publishing failed; rollback was incomplete:\nCould not restore previous ZIP")
    assert not dialog.close_button.isEnabled()
    dialog.worker_finished()
    assert dialog.isVisible()
    assert dialog.progress_bar.maximum() == 1
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Failed"
    assert "rollback was incomplete" in dialog.details.toPlainText()
    assert "Last operation: Revalidating published pack: checking audio" in dialog.details.toPlainText()
    assert "did not complete" in dialog.note_label.text()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()


def test_missing_worker_result_is_reported_as_failure(qtbot, tmp_path):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    dialog.worker_finished()
    assert dialog.progress_bar.format() == "Failed"
    assert "stopped without returning a result" in dialog.details.toPlainText()
    assert dialog.close_button.isEnabled()


@pytest.mark.parametrize("outcome", ["success", "failure", "invalid-result"])
def test_main_window_opens_popup_and_retires_worker_only_after_finished(
    qtbot, tmp_path, monkeypatch, outcome,
):
    allow_result = threading.Event()
    allow_finish = threading.Event()
    calls = []

    class Exporter:
        def export(self, project, destination, *, create_zip, progress):
            calls.append(project)
            assert create_zip
            progress(ExportProgress("Prompt 1/1: preparing still image..."))
            assert allow_result.wait(5)
            if outcome == "failure":
                raise RuntimeError("Image conversion failed")
            return export_result(destination) if outcome == "success" else object()

    class HeldWorker(main_window.ExportWorker):
        def run(self):
            super().run()
            allow_finish.wait(5)

    monkeypatch.setattr(main_window, "ExportWorker", HeldWorker)
    monkeypatch.setattr(
        main_window.ExportOptionsDialog, "exec", lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = main_window.MainWindow(SimpleNamespace(), settings=settings)
    qtbot.addWidget(window)
    window.exporter = Exporter()
    monkeypatch.setattr(window, "_confirm_backing_export", lambda: True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    window.project.video_path = str(source)
    window.project.video_duration = 10
    window.project.segments = [Segment(1, 2, "Line", ["Speaker"])]
    window.show()
    window.export_pack()
    dialog = window._export_dialog
    assert dialog is not None
    try:
        qtbot.waitUntil(lambda: dialog.progress_label.text().startswith("Prompt 1/1"))
        assert dialog.isVisible()
        assert not window.action_export.isEnabled()
        assert window.editor_splitter.isEnabled()
        assert window.action_save.isEnabled()
        assert dialog.windowModality() == Qt.WindowModality.NonModal
        assert calls[0] is not window.project
        assert calls[0].to_dict() == window.project.to_dict()
        window.export_pack()
        assert len(calls) == 1
        allow_result.set()
        qtbot.waitUntil(lambda: dialog.progress_bar.maximum() == 1)
        assert window._export_worker is not None
        assert not window.action_export.isEnabled()
        assert dialog.close_button.isEnabled()
        dialog.close()
        assert not dialog.isVisible()
        if outcome == "success":
            assert str(tmp_path / "Pack.zip") in dialog.details.toPlainText()
            assert window.statusBar().currentMessage() == "Exported Pack"
        else:
            expected = (
                "Image conversion failed" if outcome == "failure"
                else "Exporter returned an unexpected result"
            )
            assert expected in dialog.details.toPlainText()
            assert window.statusBar().currentMessage() == "Export failed"
    finally:
        allow_result.set()
        allow_finish.set()
        qtbot.waitUntil(lambda: window._export_worker is None)
        record = next(job for job in window.job_manager.tasks() if job.kind == "export")
        assert record.state == ("succeeded" if outcome == "success" else "failed")
        assert not dialog.isVisible()
        assert dialog.close_button.isEnabled()
        assert window.action_export.isEnabled()
        assert window.editor_splitter.isEnabled()
        dialog.show()
        qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
        assert window._export_dialog is None
        window.dirty = False
        window.close()


def test_worker_reports_early_errors_through_its_failure_signal(qtbot, tmp_path):
    class FailingExporter:
        def export(self, *_args, progress, **_kwargs):
            progress(ExportProgress("Inspecting source video and audio..."))
            raise OSError("Source is unavailable")

    worker = main_window.ExportWorker(FailingExporter(), PackProject(), tmp_path)
    messages = []
    failures = []
    worker.progress.connect(messages.append)
    worker.failed.connect(failures.append)
    worker.run()
    assert messages == [ExportProgress("Inspecting source video and audio...")]
    assert failures == ["Source is unavailable"]


def test_live_progress_updates_both_estimates_without_resetting_step_or_flooding_history(
    qtbot, tmp_path, monkeypatch,
):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    elapsed = [0]
    monkeypatch.setattr(dialog, "_elapsed", SimpleNamespace(elapsed=lambda: elapsed[0]))
    plan = (
        ExportStep(VIDEO_CONVERSION_STEP, "Video conversion", "video", 60),
        ExportStep("prompts", "Prompts", "prompts", 40),
    )
    dialog.report_progress(ExportProgress(
        "Converting full video", VIDEO_CONVERSION_STEP, plan=plan, live=True,
    ))
    assert "about 1m 40s remaining" in dialog.overall_eta_label.text()
    assert "about 1m 0s remaining" in dialog.step_eta_label.text()
    elapsed[0] = 10000
    dialog.report_progress(ExportProgress(
        "Encoded 00:10 / 01:00\nPrompt 2/8 - Alice", VIDEO_CONVERSION_STEP,
        fraction=0.25, position=10, live=True,
    ))
    assert dialog.progress_bar.value() == 250
    assert dialog.overall_bar.value() == 125
    assert "about 30s remaining" in dialog.step_eta_label.text()
    assert "about 1m 10s remaining" in dialog.overall_eta_label.text()
    assert "Current step: 00:00:10" in dialog.elapsed_label.text()
    assert dialog.details.document().blockCount() == 2
    assert dialog.details.toPlainText().startswith("Preparing export...\n")
    assert "Prompt 2/8 - Alice" in dialog.details.toPlainText()

    elapsed[0] = 26000
    dialog._timer.timeout.emit()
    assert "No encoding advance for 00:00:16" in dialog.activity_label.text()
    assert "about 1m 18s remaining" in dialog.step_eta_label.text()
    assert "about 1m 58s remaining" in dialog.overall_eta_label.text()
    assert dialog.overall_bar.value() < 990
    dialog.report_progress(ExportProgress(
        "Encoded 00:30 / 01:00", VIDEO_CONVERSION_STEP, fraction=0.5, position=30, live=True,
    ))
    assert "No encoding advance" not in dialog.activity_label.text()
    assert dialog.details.document().blockCount() == 2
    dialog.report_progress(ExportProgress("Preparing prompt audio", "prompts"))
    assert dialog.progress_bar.maximum() == 0
    assert dialog.overall_bar.maximum() == 1000
    assert "about 40s remaining" in dialog.step_eta_label.text()
    assert "Current step: 00:00:00" in dialog.elapsed_label.text()
    assert dialog.details.document().blockCount() == 3
    dialog.show_result(export_result(tmp_path))
    assert dialog.overall_bar.value() == dialog.overall_bar.maximum() == 1
    assert dialog.step_eta_label.text() == ""
    assert "remaining" not in dialog.overall_eta_label.text()
    dialog.worker_finished()


def test_unmeasured_suboperations_retain_history_and_overdue_estimates_are_honest(
    qtbot, tmp_path, monkeypatch,
):
    dialog = ExportProgressDialog(tmp_path)
    qtbot.addWidget(dialog)
    elapsed = [0]
    monkeypatch.setattr(dialog, "_elapsed", SimpleNamespace(elapsed=lambda: elapsed[0]))
    dialog.report_progress(ExportProgress(
        "Validating staged pack", "validation",
        plan=(ExportStep("validation", "Validation", "validation", 5),),
    ))
    elapsed[0] = 2000
    dialog.report_progress(ExportProgress("Decoding video", "validation"))
    assert "Current step: 00:00:02" in dialog.elapsed_label.text()
    assert "Validating staged pack" in dialog.details.toPlainText()
    assert "Decoding video" in dialog.details.toPlainText()
    elapsed[0] = 6000
    dialog._timer.timeout.emit()
    assert "re-estimating" in dialog.step_eta_label.text()
    assert "re-estimating" in dialog.overall_eta_label.text()
    assert dialog.overall_bar.maximum() == 0
    dialog.show_error("Decode failed")
    assert dialog.overall_bar.value() == 0
    assert dialog.overall_bar.format() == "Failed"
    assert "remaining" not in dialog.overall_eta_label.text()
    dialog.worker_finished()
