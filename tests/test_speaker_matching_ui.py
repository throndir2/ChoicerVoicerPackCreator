from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton

from choicer_voicer_pack_creator import project_checks as core_project_checks
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    Segment,
)
from choicer_voicer_pack_creator.operations import SourceSnapshot
from choicer_voicer_pack_creator.project_io import RecoveryStore
from choicer_voicer_pack_creator.speaker_matching import (
    PREPARATION_BATCH_SIZE,
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
        matches_enabled=True,
        match_error="",
        late_error="",
        ignore_cancel=False,
        hold_preparation=False,
        preparation_started=threading.Event(),
        preparation_release=threading.Event(),
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
            sources = SourceSnapshot.capture(clip.path for clip in clips)
            if state.hold_preparation:
                state.preparation_started.set()
                while not state.preparation_release.wait(0.01):
                    if cancelled():
                        raise SpeakerMatchingCancelled("Canceled")
            if state.needs_download and not allow_download:
                raise SpeakerDownloadRequired("Permission required")
            return SpeakerPreparationResult(
                sources, len(clips), 0, 0,
            )

        def match_cached(self, _media, clips, *, progress, cancelled):
            if state.match_error:
                raise ValueError(state.match_error)
            if state.cache_misses:
                state.cache_misses -= 1
                raise SpeakerPreparationRequired(tuple(clip.segment_id for clip in clips))
            state.calls.append((clips, False))
            state.thread_ids.append(threading.get_ident())
            sources = SourceSnapshot.capture(clip.path for clip in clips)
            state.started.set()
            progress("Comparing voices in the background", 0.5)
            while not state.release.wait(0.01):
                if cancelled() and not state.ignore_cancel:
                    raise SpeakerMatchingCancelled("Canceled")
            progress("Finishing the old voice comparison", 0.9)
            if state.late_error:
                raise ValueError(state.late_error)
            if cancelled() and not state.ignore_cancel:
                raise SpeakerMatchingCancelled("Canceled")
            name = next(clip.characters[0] for clip in clips if clip.characters)
            matches = tuple(
                SpeakerMatch(clip.segment_id, name, 0.95) for clip in clips if not clip.characters
            ) if state.matches_enabled else ()
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
        state.preparation_release.set()
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
    control = matching.editor.processing_dialog.rows["voices"][3]
    assert control.isEnabled()
    control.click()
    qtbot.waitUntil(matching.state.started.is_set)
    assert matching.controls.worker is not None


def finish(matching, qtbot):
    matching.state.release.set()
    qtbot.waitUntil(
        lambda: matching.controls.worker is None and (
            matching.editor._derived_publication_blocked()
            or matching.controls._publication is None
        ),
        timeout=10000,
    )
    matching.controls._timer.stop()


def prepared_clips(matching):
    return [clip for clips, _consent in matching.state.preparations for clip in clips]


def wait_prepared(matching, qtbot, count=3):
    qtbot.waitUntil(lambda: (
        len(matching.controls._prepared_ranges) == count
        and matching.controls.worker is None
    ))


def test_matching_panel_contains_only_the_auto_fill_checkbox(matching):
    editor, controls = matching.editor, matching.controls
    assert controls.enabled_check.text() == "Auto-fill speaker names"
    assert controls.layout().count() == 1
    assert controls.layout().itemAt(0).widget() is controls.enabled_check
    assert not controls.findChildren(QPushButton)
    assert not controls.findChildren(QLabel)
    action = editor.action_clear_speaker_autofill
    assert action.text() == "Clear last auto-filled names"
    assert action in matching.window.project_menu.actions()
    assert action not in editor.project_toolbar.actions()
    assert not action.isEnabled()
    assert not editor.processing_dialog.isVisible()


def test_clear_auto_fill_menu_follows_active_project_and_loading_state(matching, qtbot):
    editor, window = matching.editor, matching.window
    start(matching, qtbot)
    assert not editor.action_clear_speaker_autofill.isEnabled()
    finish(matching, qtbot)
    assert editor.action_clear_speaker_autofill.isEnabled()
    editor._set_loading(True)
    assert not editor.action_clear_speaker_autofill.isEnabled()
    editor._set_loading(False)
    assert editor.action_clear_speaker_autofill.isEnabled()
    other = window.add_project(PackProject(title="Other tab"), dirty=False)
    assert other.action_clear_speaker_autofill in window.project_menu.actions()
    assert not other.action_clear_speaker_autofill.isEnabled()
    assert editor.action_clear_speaker_autofill not in window.project_menu.actions()
    window.tabs.setCurrentWidget(editor)
    assert editor.action_clear_speaker_autofill in window.project_menu.actions()
    editor.action_clear_speaker_autofill.trigger()
    assert matching.target.characters == matching.other.characters == []
    assert not editor.action_clear_speaker_autofill.isEnabled()


def test_processing_dialog_can_restart_completed_matching(matching, qtbot):
    matching.state.matches_enabled = False
    start(matching, qtbot)
    finish(matching, qtbot)
    control = matching.editor.processing_dialog.rows["voices"][3]
    assert control.text() == "Start"
    assert control.isEnabled()
    matching.state.matches_enabled = True
    matching.state.started.clear()
    control.click()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alice"])
    assert len(matching.state.calls) == 2


def test_matching_failure_remains_visible_and_can_be_retried_in_processing(matching, qtbot):
    editor = matching.editor
    matching.state.match_error = "Voice model unavailable"
    control = editor.processing_dialog.rows["voices"][3]
    control.click()
    qtbot.waitUntil(lambda: editor.processing.group_state("voices").state == "failed")
    qtbot.waitUntil(lambda: matching.controls.worker is None)
    assert "Voice model unavailable" in editor.processing.group_state("voices").message
    assert not editor.processing_status.isHidden()
    assert "attention" in editor.processing_status.accessibleName()
    assert not editor.processing_dialog.isVisible()
    assert control.text() == "Retry"
    matching.state.match_error = ""
    start(matching, qtbot)
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]
    assert not editor.processing_status.isHidden()
    assert "Background:" not in editor.processing_status.accessibleName()


def test_background_matching_preserves_caption_cursor_selection_and_playhead(matching, qtbot):
    editor = matching.editor
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
    assert editor.action_clear_speaker_autofill.isEnabled()
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
    # Comparisons are a single guarded batch: an edit invalidates the old batch
    # immediately, rather than allowing an unchanged target to publish stale work.
    assert matching.other.characters == []


def test_reference_edit_rejects_entire_old_result_then_uses_latest_name(matching, qtbot):
    start(matching, qtbot)
    matching.reference.characters = ["Alicia"]
    matching.editor._set_dirty(True)
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert "Reference speakers changed" in matching.editor.processing.group_state("voices").message
    matching.controls.retry()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alicia"], timeout=10000)
    assert all(
        clip.characters != ("Alice",)
        for clip in matching.state.calls[-1][0] if clip.characters
    )


def test_cancel_keeps_names_and_requires_explicit_resume(matching, qtbot):
    start(matching, qtbot)
    control = matching.editor.processing_dialog.rows["voices"][3]
    assert control.text() == "Cancel"
    control.click()
    qtbot.waitUntil(lambda: matching.controls.worker is None, timeout=10000)
    assert matching.target.characters == []
    assert matching.controls._paused
    assert not matching.controls._timer.isActive()
    assert matching.editor.caption_edit.isEnabled()
    assert "Speaker matching paused." in matching.editor.processing.group_state("voices").message
    assert control.text() == "Resume"
    matching.state.started.clear()
    control.click()
    qtbot.waitUntil(matching.state.started.is_set)
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]


def test_ready_names_wait_for_modal_edit_decisions(matching, qtbot):
    start(matching, qtbot)
    dialog = QDialog(matching.editor)
    qtbot.addWidget(dialog)
    dialog.setModal(True)
    dialog.show()
    finish(matching, qtbot)
    assert matching.controls._publication is not None
    assert matching.target.characters == []
    assert not matching.editor.action_clear_speaker_autofill.isEnabled()
    assert matching.editor.processing.group_state("voices").state == "waiting"
    assert matching.editor.processing_dialog.rows["voices"][3].text() == "Cancel"
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
    assert "Source audio changed" in matching.editor.processing.group_state("voices").message
    assert matching.editor.processing.group_state("voices").state == "failed"
    assert not matching.editor.processing_status.isHidden()
    assert matching.editor.processing_dialog.rows["voices"][3].text() == "Retry"


def test_undo_preserves_manual_correction_and_excludes_restored_blanks(matching, qtbot):
    start(matching, qtbot)
    finish(matching, qtbot)
    matching.editor.speakers_edit.setText("Bob")
    matching.editor._selected_speakers_typed()
    matching.editor.action_clear_speaker_autofill.trigger()
    assert matching.target.characters == ["Bob"]
    assert matching.target.speaker_assignment == "manual"
    assert matching.other.characters == []
    assert matching.other.speaker_assignment == "excluded"
    assert not matching.editor.action_clear_speaker_autofill.isEnabled()
    assert "Latest notice: Cleared 1 auto-filled name(s)." in (
        matching.editor.statusBar().details_text()
    )
    assert matching.editor.edit_history.history.undo_label == "Clear last speaker auto-fill"


def test_shared_history_restores_speaker_batches_without_immediate_refill(matching, qtbot):
    start(matching, qtbot)
    finish(matching, qtbot)
    editor, controls = matching.editor, matching.controls
    assert editor.edit_history.history.undo_label == "Auto-fill speakers"
    after = editor.project.to_dict()
    editor.action_undo.trigger()
    qtbot.waitUntil(lambda: not editor.edit_history.busy)
    assert editor.project.segment_by_id(matching.target.id).characters == []
    assert controls._paused
    assert not controls._timer.isActive()
    assert not editor.action_clear_speaker_autofill.isEnabled()
    calls = len(matching.state.calls)
    controls._start()
    assert len(matching.state.calls) == calls
    editor.action_redo.trigger()
    qtbot.waitUntil(lambda: not editor.edit_history.busy)
    assert editor.project.to_dict() == after
    assert controls._paused


def test_only_manual_single_speaker_references_and_eligible_targets_are_submitted(matching, qtbot):
    matching.other.characters = ["Alice"]
    matching.other.speaker_assignment = "automatic"
    excluded = Segment(1, 3, speaker_assignment="excluded")
    multi = Segment(1, 3, characters=["Alice", "Bob"])
    grunt = Segment(1, 3, caption="[grunting]")
    reaction = Segment(1, 2, caption="Ugh...")
    dialogue = Segment(1, 2, caption="Yes, my lady!")
    for segment in (excluded, multi, grunt, reaction, dialogue):
        matching.editor.project.add_segment(segment)
    matching.editor._set_dirty(True)
    start(matching, qtbot)
    assert {clip.segment_id for clip in matching.state.calls[0][0]} == {
        matching.reference.id, matching.target.id, dialogue.id,
    }
    finish(matching, qtbot)


@pytest.mark.parametrize("caption", [
    "[grunting]", "(sighing)", "[gasp]...", "Ugh...", "Huh?", "Hmm.", "Ah!", "Uh...",
])
def test_nonverbal_reactions_are_not_voice_evidence(caption):
    assert speaker_matching._nonverbal(caption)


@pytest.mark.parametrize("caption", [
    "Sorry...", "Yes, my lady!", "Ugh... another annoying one", "Ah, there you are!",
])
def test_spoken_dialogue_is_not_filtered_as_a_reaction(caption):
    assert not speaker_matching._nonverbal(caption)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_matching_completion_reports_only_the_applied_count(matching, qtbot, count):
    matching.state.matches_enabled = count > 0
    if count == 1:
        matching.other.speaker_assignment = "excluded"
    start(matching, qtbot)
    finish(matching, qtbot)
    assert f"Latest notice: Filled {count} speaker name(s)." in (
        matching.editor.statusBar().details_text()
    )
    assert f"Filled {count} speaker name(s)." in matching.editor.processing.group_state("voices").message
    assert matching.target.characters == (["Alice"] if count else [])
    assert matching.other.characters == (["Alice"] if count == 2 else [])


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


def test_name_commit_updates_existing_cells_without_rebuilding_the_table(matching, monkeypatch):
    editor = matching.editor
    table = editor.segment_table
    items = [
        [table.item(row, column) for column in range(table.columnCount())]
        for row in range(table.rowCount())
    ]
    selection = editor._selected_table_ids()
    scroll = table.verticalScrollBar().value()
    playhead = editor.current_position()

    def unexpected_refresh(*_args):
        pytest.fail("Naming a segment must not rebuild the segment table")

    monkeypatch.setattr(editor, "_refresh_table", unexpected_refresh)
    editor.speakers_edit.setText("Bob")
    editor._selected_speakers_typed()
    editor.speakers_edit.setCursorPosition(1)
    editor._selected_speakers_changed()

    assert matching.target.characters == ["Bob"]
    assert items[1][3].text() == "Bob"
    assert editor.speakers_edit.cursorPosition() == 1
    assert editor._selected_table_ids() == selection
    assert table.verticalScrollBar().value() == scroll
    assert editor.current_position() == playhead
    assert all(
        table.item(row, column) is item
        for row, cells in enumerate(items)
        for column, item in enumerate(cells)
    )


def test_typing_observes_only_the_edited_segment_and_defers_validation(
    matching, qtbot, monkeypatch,
):
    editor = matching.editor
    observed = []
    validations = []
    capture = speaker_matching._SegmentState.capture
    validate = PackProject.validate

    def record_capture(cls, segment):
        observed.append(segment.id)
        return capture(segment)

    def record_validation(project):
        if project.video_path == editor.project.video_path:
            validations.append(True)
        return validate(project)

    monkeypatch.setattr(speaker_matching._SegmentState, "capture", classmethod(record_capture))
    monkeypatch.setattr(PackProject, "validate", record_validation)
    for text in ("B", "Bo", "Bob"):
        editor.speakers_edit.setText(text)
        editor._selected_speakers_typed()
    assert observed == [matching.target.id] * 3
    assert validations == []
    assert matching.target.characters == ["Bob"]
    assert editor.dirty
    qtbot.waitUntil(lambda: not editor.project_checks.pending)
    assert validations == [True]
    editor.speakers_edit.setText("Bobby")
    editor._selected_speakers_typed()
    editor._selected_speakers_changed()
    qtbot.waitUntil(lambda: not editor.project_checks.pending)
    assert validations == [True, True]
    assert not editor._validation_timer.isActive()


def test_new_segment_drag_observes_only_its_state_and_validates_once_on_release(
    matching, qtbot, monkeypatch,
):
    editor = matching.editor
    editor.mark_in_spin.setValue(3)
    editor.mark_out_spin.setValue(4)
    editor.add_segment()
    segment = editor.selected_segment()
    assert segment is not None
    editor.dirty = False
    observed, validations, audits = [], [], []
    capture = speaker_matching._SegmentState.capture
    validate = PackProject.validate
    audit = core_project_checks.audit_timeline_overlaps

    def record_capture(cls, item):
        observed.append(item.id)
        return capture(item)

    def record_validation(project):
        if project.video_path == editor.project.video_path:
            validations.append(True)
        return validate(project)

    def record_audit(segments):
        audits.append(True)
        return audit(segments)

    monkeypatch.setattr(speaker_matching._SegmentState, "capture", classmethod(record_capture))
    monkeypatch.setattr(PackProject, "validate", record_validation)
    monkeypatch.setattr(core_project_checks, "audit_timeline_overlaps", record_audit)
    editor._timeline_range_edit_started(segment.id, 3, 4)
    for index in range(1, 26):
        editor._timeline_range_changed(segment.id, 3 + index / 10, 4 + index / 10)
    assert observed == [segment.id] * 25
    assert validations == audits == []
    assert editor.dirty
    assert (editor.mark_in_spin.value(), editor.mark_out_spin.value()) == (5.5, 6.5)
    assert (editor.timeline.mark_in, editor.timeline.mark_out) == (5.5, 6.5)
    assert editor.segment_table.item(editor._row_for_segment(segment.id), 1).text() == "00:05.500"

    # Let the idle timer fire while the mouse would still be held down.
    qtbot.wait(600)
    assert validations == audits == []
    assert editor.project_checks.pending
    editor._timeline_range_edit_finished(segment.id, 3, 4, 5.5, 6.5)
    qtbot.waitUntil(lambda: not editor.project_checks.pending)
    assert validations == audits == [True]
    assert not editor._validation_timer.isActive()
    assert segment.audio_mode == "video"
    assert segment.audio_path == ""
    assert segment.source_range_known
    assert segment.characters == []
    assert "overlap" in editor.processing_status.accessibleName()
    row = editor._row_for_segment(segment.id)
    assert editor.segment_table.item(row, 0).background().style() != Qt.BrushStyle.NoBrush


@pytest.mark.parametrize("cached", [False, True])
def test_voice_jobs_wait_for_drag_release_even_when_the_mouse_pauses(matching, qtbot, cached):
    editor, controls, target = matching.editor, matching.controls, matching.target
    if cached:
        controls._activated = True
    else:
        controls._preprocess = True
    editor._timeline_range_edit_started(target.id, 4, 7)
    editor._timeline_range_changed(target.id, 4.25, 7.25)
    qtbot.wait(1100)
    assert controls.worker is None
    assert controls._timer.isActive()
    assert matching.state.preparations == matching.state.calls == []
    editor._timeline_range_changed(target.id, 4.5, 7.5)
    editor._timeline_range_edit_finished(target.id, 4, 7, 4.5, 7.5)
    if cached:
        qtbot.waitUntil(matching.state.started.is_set)
        clips = matching.state.calls[0][0]
        finish(matching, qtbot)
    else:
        qtbot.waitUntil(lambda: bool(matching.state.preparations))
        wait_prepared(matching, qtbot)
        clips = prepared_clips(matching)
    assert (4.5, 7.5) in {(clip.start, clip.end) for clip in clips}
    assert (4.25, 7.25) not in {(clip.start, clip.end) for clip in clips}


@pytest.mark.parametrize("restore_range", [False, True])
def test_inflight_matches_wait_for_drag_and_reject_moved_target(matching, qtbot, restore_range):
    editor, controls, target = matching.editor, matching.controls, matching.target
    start(matching, qtbot)
    editor._timeline_range_edit_started(target.id, 4, 7)
    editor._timeline_range_changed(target.id, 4.5, 7.5)
    finish(matching, qtbot)
    assert controls._publication is None
    assert target.characters == matching.other.characters == []
    if restore_range:
        editor._timeline_range_changed(target.id, 4, 7)
    editor._timeline_range_edit_finished(target.id, 4, 7, target.start, target.end)
    qtbot.waitUntil(lambda: controls._publication is None)
    assert target.characters == []
    assert matching.other.characters == []
    assert not controls._paused


@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_snapshot_waits_for_final_range(matching, qtbot, tmp_path, cancel):
    editor, target = matching.editor, matching.target
    store = RecoveryStore(tmp_path / "drag-recovery.json")
    editor.recovery_store = store
    editor._timeline_range_edit_started(target.id, 4, 7)
    editor._timeline_range_changed(target.id, 4.5, 7.5)
    qtbot.wait(1000)
    assert not store.path.exists()
    assert editor._recovery_timer.isActive()
    if cancel:
        editor._timeline_range_changed(target.id, 4, 7)
    editor._timeline_range_edit_finished(target.id, 4, 7, target.start, target.end)
    if cancel:
        assert not editor.dirty
        assert not editor._recovery_timer.isActive()
        qtbot.waitUntil(lambda: not matching.window.job_manager.active_jobs())
        assert not store.path.exists()
    else:
        qtbot.waitUntil(lambda: store.path.exists() and not matching.window.job_manager.active_jobs())
        snapshot = store.load()
        assert snapshot is not None
        recovered = snapshot.project.segment_by_id(target.id)
        assert recovered is not None
        assert (recovered.start, recovered.end) == (4.5, 7.5)


def test_rapid_committed_names_are_coalesced_and_unchanged_focus_does_not_rematch(
    matching, qtbot,
):
    editor, controls = matching.editor, matching.controls
    controls.prepare()
    wait_prepared(matching, qtbot)
    editor.select_segment(matching.reference.id)
    for text in ("Bob", "Bobby"):
        editor.speakers_edit.setText(text)
        editor._selected_speakers_typed()
        editor._selected_speakers_changed()
        assert controls._timer.isActive()
        assert controls._timer.interval() == 900
        qtbot.wait(100)
        assert matching.state.calls == []
    qtbot.waitUntil(matching.state.started.is_set)
    finish(matching, qtbot)
    assert len(matching.state.calls) == 1
    assert len(matching.state.preparations) == 1
    assert matching.target.characters == ["Bobby"]
    editor._selected_speakers_changed()
    assert not controls._timer.isActive()


def test_large_match_batch_and_undo_do_not_search_the_project_or_table_per_name(
    matching, qtbot, monkeypatch,
):
    editor = matching.editor
    added = [Segment(12 + index * 3, 14 + index * 3, caption="Dialogue") for index in range(400)]
    editor.project.segments.extend(added)
    editor.project.video_duration = added[-1].end
    editor._set_dirty(True)
    editor._refresh_table(matching.target.id)
    matching.controls._prepared_ranges.update(
        (editor.project.video_path, segment.start, segment.end)
        for segment in editor.project.segments
    )
    start(matching, qtbot)
    lookups = []
    segment_by_id = PackProject.segment_by_id

    def record_lookup(project, identity):
        if project is editor.project:
            lookups.append(identity)
        return segment_by_id(project, identity)

    def unexpected_row_search(*_args):
        pytest.fail("A batch should scan table rows once, not search them for each name")

    monkeypatch.setattr(PackProject, "segment_by_id", record_lookup)
    monkeypatch.setattr(editor, "_row_for_segment", unexpected_row_search)
    finish(matching, qtbot)
    assert len(lookups) < 10
    assert all(segment.characters == ["Alice"] for segment in added)
    assert all(editor.segment_table.item(row, 3).font().italic() for row in range(1, 403))
    lookups.clear()
    matching.controls.undo()
    assert len(lookups) < 10
    assert all(not segment.characters and segment.speaker_assignment == "excluded" for segment in added)
    assert all(editor.segment_table.item(row, 3).text() == "" for row in range(1, 403))


def test_name_commit_refreshes_overlap_highlights_without_replacing_cells(matching, qtbot):
    editor = matching.editor
    matching.target.start, matching.target.end = matching.reference.start, matching.reference.end
    editor._set_dirty(True)
    editor._refresh_table(matching.target.id)
    qtbot.waitUntil(lambda: not editor.project_checks.pending)
    item = editor.segment_table.item(1, 3)
    assert item.background().style() != Qt.BrushStyle.NoBrush
    for name, warned in (("Bob", False), ("Alice", True)):
        editor.speakers_edit.setText(name)
        editor._selected_speakers_typed()
        editor._selected_speakers_changed()
        qtbot.waitUntil(lambda: not editor.project_checks.pending)
        assert editor.segment_table.item(1, 3) is item
        assert (item.background().style() != Qt.BrushStyle.NoBrush) == warned
        assert ("Potential timeline overlap" in item.toolTip()) == warned


def test_changing_a_reference_name_back_still_invalidates_an_inflight_result(matching, qtbot):
    editor = matching.editor
    editor.select_segment(matching.reference.id)
    start(matching, qtbot)
    for name in ("Bob", "Alice"):
        editor.speakers_edit.setText(name)
        editor._selected_speakers_typed()
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert "Reference speakers changed" in matching.editor.processing.group_state("voices").message


def test_short_reference_prepares_targets_but_does_not_match(matching, qtbot):
    matching.reference.end = 0.5
    matching.controls.retry()
    qtbot.waitUntil(lambda: "0.75 seconds" in matching.editor.processing.group_state("voices").message)
    assert not matching.state.started.is_set()
    assert matching.state.calls == []
    assert {(clip.start, clip.end) for clip in prepared_clips(matching)} == {
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
    assert matching.editor.processing.group_state("voices").state == "off"
    assert matching.editor.processing_dialog.rows["voices"][3].text() == "Start"


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
    wait_prepared(matching, qtbot)
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
    wait_prepared(matching, qtbot, 2)
    assert not editor.project.segments
    wait_prepared(matching, qtbot, 2)
    assert [(clip.start, clip.end) for clip in prepared_clips(matching)] == [
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
    wait_prepared(matching, qtbot)
    assert matching.state.calls == []
    assert matching.other.characters == []


def test_cached_name_matching_does_not_wait_for_backing_cpu_job(matching, qtbot):
    matching.controls.prepare()
    wait_prepared(matching, qtbot)
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
    wait_prepared(matching, qtbot)
    matching.state.cache_misses = 1
    start(matching, qtbot)
    assert len(matching.state.preparations) == 2
    finish(matching, qtbot)
    assert matching.target.characters == ["Alice"]
    assert matching.editor.processing.group_state("voices").state == "ready"


def test_editing_a_range_in_a_batch_retains_other_work_and_its_old_range_cache(matching, qtbot):
    controls, state = matching.controls, matching.state
    state.hold_preparation = True
    controls.prepare()
    qtbot.waitUntil(state.preparation_started.is_set)
    handle = controls.worker.job_handle
    assert len(state.preparations[0][0]) == 3
    assert state.preparations[0][0][0].start == matching.reference.start
    matching.target.start = 4.5
    matching.editor._set_dirty(True, segment=matching.target)
    assert not handle.record.cancel_requested
    state.preparation_release.set()
    wait_prepared(matching, qtbot, 4)
    assert [(clip.start, clip.end) for clip in prepared_clips(matching)] == [
        (0, 3), (4, 7), (8, 11), (4.5, 7),
    ]
    assert not controls._paused


def test_reference_rename_does_not_cancel_preparation_or_discard_cached_audio(matching, qtbot):
    controls, state = matching.controls, matching.state
    state.hold_preparation = True
    controls.prepare()
    qtbot.waitUntil(state.preparation_started.is_set)
    handle = controls.worker.job_handle
    comparison_generation = controls._generation
    matching.reference.characters = ["Alicia"]
    matching.editor._set_dirty(True, segment=matching.reference)
    assert controls._generation > comparison_generation
    assert not handle.record.cancel_requested
    state.preparation_release.set()
    qtbot.waitUntil(lambda: len(controls._prepared_ranges) == 3 and controls.worker is None)
    controls.retry()
    qtbot.waitUntil(state.started.is_set)
    finish(matching, qtbot)
    assert len(state.preparations) == 1
    assert matching.target.characters == ["Alicia"]


def test_editing_a_prepared_range_reuses_all_unrelated_fingerprints(matching, qtbot):
    controls = matching.controls
    controls.prepare()
    qtbot.waitUntil(lambda: len(controls._prepared_ranges) == 3 and controls.worker is None)
    matching.target.start = 4.5
    matching.editor._set_dirty(True, segment=matching.target)
    qtbot.waitUntil(lambda: len(controls._prepared_ranges) == 4 and controls.worker is None)
    assert [(clip.start, clip.end) for clip in prepared_clips(matching)] == [
        (0, 3), (4, 7), (8, 11), (4.5, 7),
    ]


def test_preparation_uses_bounded_batches_and_keeps_unrelated_active_batch(matching, qtbot):
    editor, controls, state = matching.editor, matching.controls, matching.state
    count = PREPARATION_BATCH_SIZE * 2 + 3
    added = [
        Segment(12 + index * 3, 14 + index * 3, caption="More dialogue")
        for index in range(count - len(editor.project.segments))
    ]
    editor.project.segments.extend(added)
    editor.project.video_duration = added[-1].end
    editor._set_dirty(True)
    state.hold_preparation = True
    counts = []
    matching.window.job_manager.changed.connect(lambda _record: counts.append(sum(
        record.kind == "speaker-preparation"
        for record in matching.window.job_manager.active_jobs(editor.session.id)
    )))
    controls.prepare()
    qtbot.waitUntil(state.preparation_started.is_set)
    handle = controls.worker.job_handle
    last = added[-1]
    old_start = last.start
    last.start += 0.25
    editor._set_dirty(True, segment=last)
    assert not handle.record.cancel_requested
    assert all(clip.start != old_start for clip in state.preparations[0][0])
    state.preparation_release.set()
    wait_prepared(matching, qtbot, count)
    assert [len(clips) for clips, _consent in state.preparations] == [
        PREPARATION_BATCH_SIZE, PREPARATION_BATCH_SIZE, 3,
    ]
    assert max(counts) == 1
    assert not any(clip.start == old_start for clip in prepared_clips(matching))
    assert (editor.project.video_path, last.start, last.end) in controls._prepared_ranges


@pytest.mark.parametrize("late_error", ["", "Superseded model failed"])
def test_superseded_worker_never_publishes_old_progress_failure_or_pause(matching, qtbot, late_error):
    controls, state = matching.controls, matching.state
    state.ignore_cancel = True
    start(matching, qtbot)
    old = controls.worker.job_handle
    old_generation = old.record.source_snapshot["derived_generation"]
    matching.reference.characters = ["Alicia"]
    matching.editor._set_dirty(True, segment=matching.reference)
    assert not controls.derived_work.current("speakers", old_generation)
    assert old.record.cancel_requested
    messages = []
    matching.editor.processing.changed.connect(
        lambda: messages.append(matching.editor.processing.group_state("voices")),
    )
    state.late_error = late_error
    finish(matching, qtbot)
    assert matching.target.characters == []
    assert not controls._paused
    assert all(value.state not in {"failed", "cancelled", "cancelling"} for value in messages)
    assert not any("Finishing the old voice comparison" in value.message for value in messages)
    assert not any(late_error and late_error in value.message for value in messages)
    state.late_error = ""
    controls.name_committed(force=True)
    qtbot.waitUntil(lambda: matching.target.characters == ["Alicia"])
    assert len(state.calls) == 2


def test_source_identity_is_reverified_off_thread_after_modal_wait(
    matching, qtbot, monkeypatch,
):
    threads = []
    verify = SourceSnapshot.verify

    def observed_verify(snapshot):
        threads.append(threading.get_ident())
        return verify(snapshot)

    monkeypatch.setattr(SourceSnapshot, "verify", observed_verify)
    start(matching, qtbot)
    dialog = QDialog(matching.editor)
    qtbot.addWidget(dialog)
    dialog.setModal(True)
    dialog.show()
    finish(matching, qtbot)
    assert matching.controls._publication is not None
    matching.video.write_bytes(b"Changed after scoring finished while publication was deferred")
    dialog.reject()
    qtbot.waitUntil(lambda: matching.editor.processing.group_state("voices").state == "failed")
    assert matching.target.characters == matching.other.characters == []
    assert threads
    assert threading.get_ident() not in threads
    assert "Source audio changed" in matching.editor.processing.group_state("voices").message


@pytest.mark.parametrize("source_changed", [False, True])
def test_verification_completed_behind_modal_requires_another_source_check(
    matching, qtbot, monkeypatch, source_changed,
):
    checked, finish_verification = threading.Event(), threading.Event()
    verified_threads = []
    run = speaker_matching.SpeakerWorker.run

    def controlled_run(worker):
        verification = worker.verification is not None
        run(worker)
        if verification:
            verified_threads.append(threading.get_ident())
            if len(verified_threads) == 1:
                checked.set()
                assert finish_verification.wait(10)

    monkeypatch.setattr(speaker_matching.SpeakerWorker, "run", controlled_run)
    dialog = QDialog(matching.editor)
    qtbot.addWidget(dialog)
    dialog.setModal(True)
    try:
        start(matching, qtbot)
        matching.state.release.set()
        qtbot.waitUntil(checked.is_set)
        # The second source check has succeeded, but its JobRecord cannot finish
        # until this barrier opens. Make that completion wait behind a new modal.
        dialog.show()
        finish_verification.set()
        qtbot.waitUntil(lambda: matching.controls.worker is None)
        assert matching.controls._publication is not None
        assert matching.target.characters == []
        if source_changed:
            matching.video.write_bytes(b"Source changed after verification, before publication")
        dialog.reject()
        if source_changed:
            qtbot.waitUntil(lambda: matching.editor.processing.group_state("voices").state == "failed")
            assert matching.target.characters == matching.other.characters == []
            assert "Source audio changed" in matching.editor.processing.group_state("voices").message
        else:
            qtbot.waitUntil(lambda: matching.target.characters == ["Alice"])
        assert len(verified_threads) == 2
        assert threading.get_ident() not in verified_threads
    finally:
        finish_verification.set()
        dialog.reject()


def test_history_replay_pauses_until_name_edit_or_explicit_resume(matching, qtbot):
    controls = matching.controls
    start(matching, qtbot)
    controls.history_replayed()
    finish(matching, qtbot)
    assert controls._paused and controls._history_paused
    assert matching.target.characters == []
    controls.changed(segment=matching.target)
    assert not controls._timer.isActive()
    matching.reference.characters = ["Alicia"]
    controls.name_typed(matching.reference)
    controls.changed(segment=matching.reference)
    assert not controls._paused and not controls._history_paused
    controls.name_committed()
    qtbot.waitUntil(lambda: matching.target.characters == ["Alicia"])
    controls.cancel()
    controls.name_typed(matching.reference)
    assert controls._paused
    assert not controls._history_paused


def test_closing_source_clears_debounced_voice_work_without_submission(matching, qtbot):
    matching.controls.retry()
    matching.controls.close_processing()
    qtbot.wait(20)
    assert matching.state.preparations == matching.state.calls == []
    assert not matching.controls._timer.isActive()
    assert matching.target.characters == []


def test_task_window_cancellation_is_an_explicit_voice_pause(matching, qtbot):
    start(matching, qtbot)
    matching.controls.worker.job_handle.cancel()
    qtbot.waitUntil(lambda: matching.controls.worker is None)
    assert matching.controls._paused
    assert not matching.controls._timer.isActive()
    assert matching.editor.processing.group_state("voices").state == "cancelled"
    assert matching.target.characters == []


@pytest.mark.parametrize("late_error", ["", "Cancelled inference failed late"])
def test_task_cancel_then_edit_stays_paused_through_late_completion(matching, qtbot, late_error):
    matching.state.ignore_cancel = True
    start(matching, qtbot)
    matching.controls.worker.job_handle.cancel()
    assert matching.controls._paused
    matching.reference.characters = ["New name"]
    matching.editor._set_dirty(True, segment=matching.reference)
    matching.state.late_error = late_error
    matching.state.release.set()
    qtbot.waitUntil(lambda: matching.controls.worker is None)
    assert matching.controls._paused
    assert not matching.controls._timer.isActive()
    assert len(matching.state.calls) == 1
    assert matching.editor.processing.group_state("voices").state == "cancelled"
    assert matching.target.characters == []
    matching.state.late_error = ""
    matching.controls.retry()
    qtbot.waitUntil(lambda: matching.target.characters == ["New name"], timeout=10000)
