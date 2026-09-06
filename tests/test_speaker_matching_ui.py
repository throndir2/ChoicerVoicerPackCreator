from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QMessageBox

from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    Segment,
)
from choicer_voicer_pack_creator.operations import SourceSnapshot
from choicer_voicer_pack_creator.speaker_matching import (
    SpeakerDownloadRequired,
    SpeakerMatch,
    SpeakerMatchingCancelled,
    SpeakerPreparationRequired,
    SpeakerPreparationResult,
    SpeakerResult,
)
from choicer_voicer_pack_creator.ui import speaker_matching
from choicer_voicer_pack_creator.ui.main_window import MainWindow


@pytest.fixture
def matching(qtbot, tmp_path, monkeypatch):
    state = SimpleNamespace(
        started=threading.Event(), release=threading.Event(), calls=[],
        needs_download=False, thread_ids=[],
        preparations=[],
        cache_misses=0,
    )
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fake media; worker is mocked")

    class FakeManager:
        manifest = {"model": {"sha256": "a" * 64}}
        model_download_bytes = 26_530_550

        def __init__(self, _root):
            pass

        def prepare(self, _media, clips, *, allow_download, progress, cancelled):
            state.preparations.append((clips, allow_download))
            if state.needs_download and not allow_download:
                raise SpeakerDownloadRequired("Permission required")
            return SpeakerPreparationResult(
                SourceSnapshot.capture(clip.path for clip in clips), len(clips), 0, 0,
            )

        def match_cached(self, _media, clips, *, progress, cancelled):
            if state.cache_misses:
                state.cache_misses -= 1
                raise SpeakerPreparationRequired(tuple(clip.segment_id for clip in clips))
            state.calls.append((clips, False))
            state.thread_ids.append(threading.get_ident())
            sources = SourceSnapshot.capture(clip.path for clip in clips)
            state.started.set()
            progress("Comparing voices in the background", 0.5)
            while not state.release.wait(0.01):
                if cancelled():
                    raise SpeakerMatchingCancelled("Canceled")
            if cancelled():
                raise SpeakerMatchingCancelled("Canceled")
            name = next(clip.characters[0] for clip in clips if clip.characters)
            matches = tuple(
                SpeakerMatch(clip.segment_id, name, 0.95) for clip in clips if not clip.characters
            )
            return SpeakerResult(matches, sources, len(clips), 0, 0)

    monkeypatch.setattr(speaker_matching, "SpeakerMatchingManager", FakeManager)
    media = SimpleNamespace(waveform_peaks=lambda *_args, **_kwargs: [])
    window = MainWindow(
        media, settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    reference = Segment(0, 3, caption="Reference dialogue", characters=["Alice"])
    target = Segment(4, 7, caption="Still editing this line")
    other = Segment(8, 11, caption="Another line")
    editor = window.add_project(
        PackProject(
            title="Speaker test", authors=["Tester"], video_path=str(video),
            video_duration=12, segments=[reference, target, other],
        ),
        dirty=False,
    )

    def close(_widget):
        state.release.set()
        for current in window.editors.values():
            current.speaker_matching.close_processing()
        window.setup_consent.cancel_all()
        for record in window.job_manager.active_jobs():
            window.job_manager.cancel(record.id)
        qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
        for box in list(window._decisions):
            box.reject()
        for current in window.editors.values():
            current.dirty = False
            current._recovery_timer.stop()
        window.close()
        qtbot.waitUntil(lambda: window._close_approved, timeout=10000)

    qtbot.addWidget(window, before_close_func=close)
    window.show()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    editor.select_segment(target.id)
    return SimpleNamespace(
        window=window, editor=editor, controls=editor.speaker_matching,
        reference=reference, target=target, other=other, state=state, video=video,
    )


def start(matching, qtbot):
    matching.controls.retry()
    qtbot.waitUntil(matching.state.started.is_set)
    assert matching.controls.worker is not None


def finish(matching, qtbot):
    matching.state.release.set()
    qtbot.waitUntil(lambda: matching.controls.worker is None, timeout=10000)
    matching.controls._timer.stop()


def test_background_matching_preserves_caption_cursor_selection_and_playhead(matching, qtbot):
    editor, controls = matching.editor, matching.controls
    start(matching, qtbot)
    assert editor.editor_splitter.isEnabled()
    assert editor.caption_edit.isEnabled()
    assert len(matching.state.thread_ids) == 1
    assert matching.state.thread_ids[0] != threading.get_ident()
    editor.caption_edit.setFocus()
    editor.caption_edit.setPlainText("I can keep working while voices are compared.")
    cursor = editor.caption_edit.textCursor()
    cursor.setPosition(6)
    cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 4)
    editor.caption_edit.setTextCursor(cursor)
    before = (
        editor.selected_segment_id, editor.current_position(),
        cursor.position(), cursor.anchor(), editor.caption_edit.toPlainText(),
    )
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]
    assert matching.other.characters == ["Alice"]
    assert matching.target.speaker_assignment == "automatic"
    cursor = editor.caption_edit.textCursor()
    assert (
        editor.selected_segment_id, editor.current_position(),
        cursor.position(), cursor.anchor(), editor.caption_edit.toPlainText(),
    ) == before
    assert controls.undo_button.isEnabled()
    assert editor.session.dirty


@pytest.mark.parametrize("edit", ["name", "clear", "range", "range_back", "delete"])
def test_background_result_never_overwrites_changed_target(matching, qtbot, edit):
    editor, target = matching.editor, matching.target
    start(matching, qtbot)
    if edit == "name":
        editor.speakers_edit.setText("Bob")
        editor._selected_speakers_typed()
    elif edit == "clear":
        editor.speakers_edit.setText("Bob")
        editor._selected_speakers_typed()
        editor.speakers_edit.clear()
        editor._selected_speakers_typed()
        assert target.speaker_assignment == "excluded"
    elif edit in {"range", "range_back"}:
        target.start = 4.5
        editor._set_dirty(True)
        if edit == "range_back":
            target.start = 4
            editor._set_dirty(True)
    else:
        editor.project.remove_segment(target.id)
        editor._set_dirty(True)
    finish(matching, qtbot)
    assert target.characters == (["Bob"] if edit == "name" else [])
    assert matching.other.characters == ([] if edit == "name" else ["Alice"])


def test_reference_edit_rejects_entire_old_result_then_uses_latest_name(matching, qtbot):
    start(matching, qtbot)
    matching.reference.characters = ["Alicia"]
    matching.editor._set_dirty(True)
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert "Reference speakers changed" in matching.controls.status.text()
    matching.controls.retry()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alicia"], timeout=10000)
    assert all(
        clip.characters != ("Alice",)
        for clip in matching.state.calls[-1][0] if clip.characters
    )


def test_cancel_keeps_names_and_requires_explicit_resume(matching, qtbot):
    start(matching, qtbot)
    matching.controls.cancel()
    qtbot.waitUntil(lambda: matching.controls.worker is None, timeout=10000)
    assert matching.target.characters == []
    assert matching.controls._paused
    assert not matching.controls._timer.isActive()
    assert matching.editor.caption_edit.isEnabled()


def test_ready_names_wait_for_modal_edit_decisions(matching, qtbot):
    start(matching, qtbot)
    dialog = QDialog(matching.editor)
    qtbot.addWidget(dialog)
    dialog.setModal(True)
    dialog.show()
    finish(matching, qtbot)
    assert matching.controls._publication is not None
    assert matching.target.characters == []
    matching.editor.dirty = False
    dialog.reject()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alice"])
    assert matching.editor.dirty


def test_source_replacement_discards_matching_result(matching, qtbot):
    start(matching, qtbot)
    matching.video.write_bytes(b"Different source content and size")
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert matching.other.characters == []
    assert "Source audio changed" in matching.controls.status.text()


def test_undo_preserves_manual_correction_and_excludes_restored_blanks(matching, qtbot):
    start(matching, qtbot)
    finish(matching, qtbot)
    matching.editor.speakers_edit.setText("Bob")
    matching.editor._selected_speakers_typed()
    matching.controls.undo()
    assert matching.target.characters == ["Bob"]
    assert matching.target.speaker_assignment == "manual"
    assert matching.other.characters == []
    assert matching.other.speaker_assignment == "excluded"
    assert not matching.controls.undo_button.isEnabled()


def test_only_manual_single_speaker_references_and_eligible_targets_are_submitted(matching, qtbot):
    matching.other.characters = ["Alice"]
    matching.other.speaker_assignment = "automatic"
    excluded = Segment(1, 3, speaker_assignment="excluded")
    multi = Segment(1, 3, characters=["Alice", "Bob"])
    grunt = Segment(1, 3, caption="[grunting]")
    for segment in (excluded, multi, grunt):
        matching.editor.project.add_segment(segment)
    matching.editor._set_dirty(True)
    start(matching, qtbot)
    assert {clip.segment_id for clip in matching.state.calls[0][0]} == {
        matching.reference.id, matching.target.id,
    }
    finish(matching, qtbot)


def test_typing_does_not_start_model_until_name_is_committed(matching, qtbot):
    matching.editor.select_segment(matching.reference.id)
    matching.editor.speakers_edit.setText("Al")
    matching.editor._selected_speakers_typed()
    matching.controls._start()
    assert not matching.state.started.is_set()
    matching.editor.speakers_edit.setText("Alicia")
    matching.editor._selected_speakers_typed()
    matching.editor._selected_speakers_changed()
    matching.controls._timer.stop()
    matching.controls._start()
    qtbot.waitUntil(matching.state.started.is_set)
    finish(matching, qtbot)
    assert matching.target.characters == ["Alicia"]


def test_short_reference_prepares_targets_but_does_not_match(matching, qtbot):
    matching.reference.end = 0.5
    matching.controls.retry()
    qtbot.waitUntil(lambda: "1.5 seconds" in matching.controls.status.text())
    assert not matching.state.started.is_set()
    assert matching.state.calls == []
    assert {(clip.start, clip.end) for clip in matching.state.preparations[0][0]} == {
        (matching.target.start, matching.target.end), (matching.other.start, matching.other.end),
    }
    assert matching.window.setup_consent.box is None


def test_immediate_retry_does_not_disable_future_debounce(matching, qtbot):
    start(matching, qtbot)
    matching.target.start += 0.1
    matching.editor._set_dirty(True)
    finish(matching, qtbot)
    assert matching.controls._timer.interval() == 900
    assert len(matching.state.calls) == 1


def test_download_consent_is_nonmodal_and_does_not_disable_editor(matching, qtbot):
    matching.state.needs_download = True
    matching.controls.retry()
    qtbot.waitUntil(lambda: matching.window.setup_consent.box is not None)
    box = matching.window.setup_consent.box
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert matching.editor.editor_splitter.isEnabled()
    assert not matching.state.started.is_set()
    box.reject()
    qtbot.waitUntil(lambda: not matching.controls._pending_consent)
    assert matching.target.characters == []
    assert matching.controls._paused
    assert matching.state.preparations[0][1] is False


def test_cancel_removes_only_speaker_download_request(matching, qtbot):
    matching.state.needs_download = True
    matching.controls.retry()
    qtbot.waitUntil(lambda: matching.controls._pending_consent)
    replies = []
    matching.window.setup_consent.request(
        matching.editor.session.id, {"backing:model": "Backing model"},
        replies.append, lambda: True,
    )
    matching.controls.cancel()
    assert not matching.controls._pending_consent
    assert replies == []
    assert "Speaker-matching model" not in matching.window.setup_consent.box.text()
    matching.window.setup_consent.box.reject()


def test_explicit_retyping_can_confirm_a_one_character_automatic_name(matching):
    matching.target.characters = ["A"]
    matching.target.speaker_assignment = "automatic"
    matching.editor._sync_selected_editor()
    matching.editor._selected_speakers_typed()
    assert matching.target.speaker_assignment == "manual"


def test_new_project_generation_cannot_receive_old_results(matching, qtbot):
    start(matching, qtbot)
    original = matching.editor.project
    matching.editor._set_project(PackProject(title="Replacement"), None, False)
    finish(matching, qtbot)
    assert matching.editor.project is not original
    assert matching.target.characters == []
    assert matching.editor.project.segments == []


def test_disabling_auto_matching_is_persisted_and_cancels_current_pass(matching, qtbot):
    start(matching, qtbot)
    matching.controls.enabled_check.setChecked(False)
    qtbot.waitUntil(lambda: matching.controls.worker is None, timeout=10000)
    assert not matching.editor.project.auto_speaker_matching
    assert matching.target.characters == []
    assert not PackProject.from_dict(matching.editor.project.to_dict()).auto_speaker_matching


def test_canceling_exit_publishes_result_completed_during_exit_question(matching, qtbot):
    start(matching, qtbot)
    matching.window.close()
    qtbot.waitUntil(lambda: matching.window._closing)
    decision = next(box for box in matching.window._decisions if box.windowTitle() == "Tasks are still running")
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert matching.controls._publication is not None
    decision.button(QMessageBox.StandardButton.Cancel).click()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alice"])
    assert not matching.window._closing
    assert matching.editor.dirty


def test_reenable_during_cancellation_resumes_after_cleanup(matching, qtbot):
    start(matching, qtbot)
    matching.controls.enabled_check.setChecked(False)
    matching.controls.enabled_check.setChecked(True)
    assert matching.controls._resume_requested
    qtbot.waitUntil(lambda: len(matching.state.calls) == 2, timeout=10000)
    assert not matching.controls._paused
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]
    assert matching.controls.enabled_check.isChecked()


def test_unnamed_segments_are_prepared_before_any_name_is_entered(matching, qtbot):
    matching.reference.characters = []
    matching.editor._set_dirty(True)
    matching.controls.prepare()
    qtbot.waitUntil(lambda: bool(matching.state.preparations) and matching.controls.worker is None)
    assert matching.state.calls == []
    assert all(not clip.characters for clip in matching.state.preparations[0][0])
    assert matching.editor.processing.group_state("voices").state == "ready"
    assert matching.editor.project.segments[0].characters == []

    matching.editor.select_segment(matching.reference.id)
    matching.editor.speakers_edit.setText("Alice")
    matching.editor._selected_speakers_typed()
    matching.editor._selected_speakers_changed()
    qtbot.waitUntil(matching.state.started.is_set)
    assert matching.controls.worker.job_handle.record.resource_class == "io"
    finish(matching, qtbot)
    assert len(matching.state.preparations) == 1
    assert matching.target.characters == ["Alice"]


def test_draft_ranges_are_prepared_before_becoming_segments(matching, qtbot):
    editor = matching.editor
    editor._set_project(PackProject(
        video_path=str(matching.video), video_duration=12,
        analysis_review=AnalysisReview(local_rows=[
            AnalysisDraftRow("0.000", "3.000", "First line", "Whisper"),
            AnalysisDraftRow("4.000", "7.000", "Second line", "Whisper"),
            AnalysisDraftRow("unfinished", "9", "Editing", "Whisper"),
        ]),
    ), None, False)
    matching.controls.prepare()
    qtbot.waitUntil(lambda: bool(matching.state.preparations) and matching.controls.worker is None)
    assert not editor.project.segments
    assert [(clip.start, clip.end) for clip in matching.state.preparations[0][0]] == [
        (0.0, 3.0), (4.0, 7.0),
    ]
    assert matching.state.calls == []
    editor.project.segments = [Segment(0, 3, characters=["Alice"]), Segment(4, 7)]
    editor._set_dirty(True)
    start(matching, qtbot)
    finish(matching, qtbot)
    assert len(matching.state.preparations) == 1
    assert editor.project.segments[1].characters == ["Alice"]


def test_preparation_continues_while_typing_without_publishing_names(matching, qtbot):
    matching.editor.speakers_edit.setText("Typing")
    matching.editor._selected_speakers_typed()
    assert matching.controls._typing
    matching.controls.prepare()
    qtbot.waitUntil(lambda: bool(matching.state.preparations) and matching.controls.worker is None)
    assert matching.state.calls == []
    assert matching.other.characters == []


def test_cached_name_matching_does_not_wait_for_backing_cpu_job(matching, qtbot):
    matching.controls.prepare()
    qtbot.waitUntil(lambda: bool(matching.state.preparations) and matching.controls.worker is None)
    release, started = threading.Event(), threading.Event()

    def backing(_context):
        started.set()
        release.wait(10)

    job = matching.window.job_manager.submit(
        matching.editor.session.id, "backing", "Backing", backing,
        source_snapshot={"source_revision": matching.editor.session.source_revision},
    )
    try:
        qtbot.waitUntil(started.is_set)
        start(matching, qtbot)
        finish(matching, qtbot)
        assert job.record.state == "running"
        assert matching.target.characters == ["Alice"]
        assert len(matching.state.preparations) == 1
    finally:
        release.set()
        qtbot.waitUntil(lambda: not job.record.active)


def test_missing_cached_signature_returns_to_cpu_preparation(matching, qtbot):
    matching.controls.prepare()
    qtbot.waitUntil(lambda: bool(matching.state.preparations) and matching.controls.worker is None)
    matching.state.cache_misses = 1
    start(matching, qtbot)
    assert len(matching.state.preparations) == 2
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]
    assert matching.editor.processing.group_state("voices").state == "ready"
