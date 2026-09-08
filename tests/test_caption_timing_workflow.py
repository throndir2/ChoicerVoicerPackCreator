from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from choicer_voicer_pack_creator import analysis
from choicer_voicer_pack_creator.analysis import AnalysisCancelled, AnalysisResult, detect_hardware
from choicer_voicer_pack_creator.caption_timing_types import CaptionTimingResult
from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import AnalysisDraftRow, PackProject, SourceCaption
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.ui import analysis_dialog


def timing_result():
    captions = (
        SourceCaption(0.85, 2.2, "First complete phrase.", "YouTube - word alignment"),
        SourceCaption(3, 4, "Uncertain phrase.", "YouTube - word alignment"),
    )
    timing = CaptionTimingResult(captions, (0.92, None), ("", "Opening word not corroborated"))
    return AnalysisResult([], 0, 0, None, None, None, detect_hardware(), list(captions), timing)


@pytest.fixture
def dialog(qtbot, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator import caption_timing_runtime

    class Manager:
        model_name = "Test timing model"
        download_bytes = 1500 * 1024**2
        component_key = "timing:test-checksum"

        def __init__(self, _data_root):
            pass

    monkeypatch.setattr(caption_timing_runtime, "CaptionTimingManager", Manager)
    value = analysis_dialog.AnalysisDialog(
        object(), tmp_path / "video.mp4", 10, tmp_path / "data", 0,
        source_captions=[SourceCaption(1, 2, "Original caption", "YouTube creator (en)")],
    )
    qtbot.addWidget(value)
    return value


@pytest.mark.parametrize("approved", [False, True])
def test_word_alignment_requires_permission_and_passes_flags(dialog, monkeypatch, approved):
    calls = []
    monkeypatch.setattr(dialog, "_start_worker", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_: QMessageBox.StandardButton.Yes if approved else QMessageBox.StandardButton.Cancel,
    )
    dialog.start_alignment()
    assert len(calls) == int(approved)
    if approved:
        assert calls[0]["align_captions"]
        assert calls[0]["allow_alignment_download"]
        assert calls[0]["refine"]
        assert not calls[0]["use_whisper"]
    assert not dialog._pending_refine
    assert dialog.source_captions[0].text == "Original caption"
    assert not dialog.local_table.rowCount()


def test_alignment_uncertainty_is_visible_unchecked_and_persisted(dialog):
    dialog._completed(timing_result())
    assert dialog.refined_table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert dialog.refined_table.item(1, 0).checkState() == Qt.CheckState.Unchecked
    assert "Opening word not corroborated" in dialog.refined_table.item(1, 4).text()
    assert "1 need timing review" in dialog.refined_status.text()
    review = dialog.review_state()
    restored = PackProject.from_dict(PackProject(analysis_review=review).to_dict()).analysis_review
    assert restored == review
    assert restored.refined_rows[0].confidence == 0.92
    assert not restored.refined_rows[1].checked
    assert dialog.source_captions[0].start == 1


def test_alignment_result_keeps_edits_made_while_waiting_for_permission(dialog, monkeypatch):
    questions = []

    def show(_parent, _kind, _title, _text, callback=None, *_args):
        questions.append(callback)

    monkeypatch.setattr(analysis_dialog, "show_message", show)
    monkeypatch.setattr(dialog, "_start_worker", lambda **_kwargs: None)
    dialog._populate_rows(dialog.refined_table, [
        AnalysisDraftRow("1", "2", "Draft before request", "YouTube"),
    ])
    dialog.start_alignment()
    questions.pop(0)(True)
    dialog.refined_table.item(0, 3).setText("Human edit during model permission")
    questions.pop(0)(True)
    dialog._completed(timing_result())
    assert dialog.refined_table.item(0, 3).text() == "Human edit during model permission"
    assert True in dialog._pending_results
    assert not dialog.apply_refined_result_button.isHidden()


def test_cancel_during_model_permission_cannot_start_late_alignment(dialog, monkeypatch):
    questions, starts = [], []
    monkeypatch.setattr(
        analysis_dialog, "show_message",
        lambda _p, _k, _t, _m, callback=None, *_a: questions.append(callback),
    )
    monkeypatch.setattr(dialog, "_start_worker", lambda **kwargs: starts.append(kwargs))
    dialog.start_alignment()
    dialog.cancel_scan()
    assert not dialog._pending_refine
    questions[0](True)
    assert not starts
    assert not dialog._pending_refine


def test_cancel_retires_replacement_confirmation_even_after_retry(dialog, monkeypatch):
    questions, starts = [], []
    monkeypatch.setattr(
        analysis_dialog, "show_message",
        lambda _p, _k, _t, _m, callback=None, *_a: questions.append(callback),
    )
    monkeypatch.setattr(dialog, "_start_worker", lambda **kwargs: starts.append(kwargs))
    dialog._populate_rows(dialog.refined_table, [
        AnalysisDraftRow("1", "2", "Existing draft", "YouTube"),
    ])
    dialog.start_alignment()
    old_confirmation = questions.pop(0)
    dialog.cancel_scan()
    assert dialog.align_button.isEnabled()
    dialog.start_alignment()
    new_confirmation = questions.pop(0)
    old_confirmation(True)
    assert not starts
    assert not questions
    new_confirmation(True)
    questions.pop(0)(True)
    assert len(starts) == 1


def test_managed_alignment_shares_whisper_admission_and_preserves_other_draft(
    dialog, qtbot, monkeypatch,
):
    manager = JobManager(limits={"cpu": 2, "io": 1, "network": 1})
    release = Event()
    calls = []
    holder = manager.submit(
        None, "analysis", "Existing transcription", lambda _context: release.wait(10),
        resource_keys=("whisper-inference",),
    )
    dialog.job_manager = manager
    dialog.project_id = "timing-project"
    dialog._populate_rows(dialog.local_table, [
        AnalysisDraftRow("1", "3", "Human-edited local transcript", "Whisper"),
    ])
    before = dialog.review_state().local_rows

    def analyze(*_args, **kwargs):
        calls.append(kwargs)
        return timing_result()

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(
        analysis_dialog, "show_message",
        lambda _p, _k, _t, _m, callback=None, *_a: callback(True) if callback else None,
    )
    try:
        qtbot.waitUntil(lambda: holder.record.state == "running")
        dialog.start_alignment()
        worker = dialog.refinement_worker
        assert worker is not None
        qtbot.waitUntil(lambda: worker.job_handle.record.state == "waiting")
        assert not calls
        release.set()
        qtbot.waitUntil(lambda: dialog.refinement_worker is None)
        assert len(calls) == 1
        assert calls[0]["align_captions"]
        assert calls[0]["allow_alignment_download"]
        assert dialog.review_state().local_rows == before
        assert dialog.refined_table.rowCount() == 2
        assert dialog.refined_table.item(1, 0).checkState() == Qt.CheckState.Unchecked
    finally:
        release.set()
        manager.shutdown(cancel=True, wait=True)


def test_failed_alignment_keeps_existing_review(dialog, monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *_: QMessageBox.StandardButton.Ok)
    dialog._completed(timing_result())
    before = dialog.review_state()
    dialog._failed("Alignment model could not be verified")
    assert dialog.review_state() == before


def test_inconsistent_timing_result_does_not_replace_draft(dialog, monkeypatch):
    errors = []
    monkeypatch.setattr(dialog, "_failed", errors.append)
    value = timing_result()
    dialog._completed(replace(value, caption_timing=replace(value.caption_timing, review_reasons=())))
    assert errors == ["Word alignment returned inconsistent review data"]
    assert dialog.refined_table.rowCount() == 0


@pytest.mark.parametrize("cancelled", [False, True])
def test_analysis_routes_original_captions_to_alignment_and_preserves_cancellation(
    tmp_path, monkeypatch, cancelled,
):
    from choicer_voicer_pack_creator import caption_timing_runtime

    video = tmp_path / "video.mp4"
    video.write_bytes(b"immutable synthetic media")
    cues = [SourceCaption(1, 2, "Whole original phrase", "YouTube")]
    original = cues[0].to_dict()
    calls = []
    value = timing_result()

    def improve(wav, received, duration, data_root, **kwargs):
        calls.append((wav, received, duration, data_root, kwargs))
        if cancelled:
            raise OperationCancelled("User canceled alignment")
        return value.caption_timing

    monkeypatch.setattr(caption_timing_runtime, "improve_caption_audio", improve)
    monkeypatch.setattr(analysis, "extract_analysis_audio", lambda *_: None)
    monkeypatch.setattr(analysis, "scan_audio_activity", lambda *_args, **_kwargs: ([], None))
    arguments = dict(
        sensitivity="balanced", use_whisper=False, model_key="base", language="en",
        progress=lambda *_: None, cancelled=lambda: False, source_captions=cues,
        align_captions=True, allow_alignment_download=True,
    )
    if cancelled:
        with pytest.raises(AnalysisCancelled):
            analysis.analyze_video(object(), video, 10, tmp_path, **arguments)
    else:
        result = analysis.analyze_video(object(), video, 10, tmp_path, **arguments)
        assert result.caption_timing == value.caption_timing
        assert result.refined_captions == value.refined_captions
        assert result.suggestions == []
    assert calls[0][1] == cues
    assert calls[0][-1]["allow_download"]
    assert cues[0].to_dict() == original


@pytest.mark.parametrize("captions", [None, []])
def test_alignment_requires_original_caption_evidence_before_processing(tmp_path, captions):
    with pytest.raises(analysis.AnalysisError, match="original imported captions"):
        analysis.analyze_video(
            object(), tmp_path / "missing.mp4", 10, tmp_path,
            sensitivity="balanced", use_whisper=False, model_key="base", language="en",
            progress=lambda *_: None, cancelled=lambda: False,
            source_captions=captions, align_captions=True,
        )
