from __future__ import annotations

from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings, Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QToolButton,
)

from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    Segment,
)
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.separation_types import KEEP_SINGING, REMOVE_ALL_VOCALS
from choicer_voicer_pack_creator.ui.export_options_dialog import ExportOptions
from choicer_voicer_pack_creator.ui.history import CONFIRM_DELETE_SETTING, ReplayView
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="History", authors=["Author"], video_path="missing.mp4", video_duration=20,
            segments=[Segment(1, 3, "Original line", ["Alice"])],
            auto_speaker_matching=False,
        ), None, mark_dirty=False,
    )
    window.select_segment(window.project.segments[0].id)
    monkeypatch.setattr(window.speaker_matching, "name_committed", Mock())
    settings.setValue(CONFIRM_DELETE_SETTING, False)
    yield window
    qtbot.waitUntil(lambda: not window.edit_history.busy, timeout=10000)
    for editor in window.editors.values():
        editor.dirty = False
    window.close()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)


def restore(window, qtbot, *, redo=False):
    (window.action_redo if redo else window.action_undo).trigger()
    qtbot.waitUntil(lambda: not window.edit_history.busy, timeout=10000)


def test_backing_mode_history_restores_preference_and_invalidates_pending_requests(window, qtbot):
    original_path = window.project.backing_track_path
    window.project.backing_generation_mode = KEEP_SINGING
    window.session.backing_revision += 1
    window._set_dirty(True, history_label="Change backing generation mode", fields_only=True)
    revision = window.session.backing_revision
    restore(window, qtbot)
    assert window.project.backing_generation_mode == REMOVE_ALL_VOCALS
    assert window.session.backing_revision > revision
    revision = window.session.backing_revision
    restore(window, qtbot, redo=True)
    assert window.project.backing_generation_mode == KEEP_SINGING
    assert window.session.backing_revision > revision
    assert window.project.backing_track_path == original_path
    window.duplicate_segment()
    revision = window.session.backing_revision
    restore(window, qtbot)
    assert window.project.backing_generation_mode == KEEP_SINGING
    assert window.session.backing_revision > revision


def test_delete_restores_original_id_media_and_selection(window, qtbot):
    segment = window.project.segments[0]
    segment.audio_mode = "file"
    segment.audio_path = "preserved.mp3"
    segment.image_path = "preserved.png"
    segment.source_range_known = False
    window._set_project(window.project, None, mark_dirty=False)
    window.select_segment(segment.id)
    before = window.project.to_dict()
    window.delete_segment()
    assert not window.project.segments
    assert window.edit_history.history.labels == ("Delete segment",)
    restore(window, qtbot)
    assert window.project.to_dict() == before
    assert window.selected_segment_id == segment.id
    assert window.timeline.mark_segment_id == segment.id
    assert window.caption_edit.toPlainText() == segment.caption
    assert not window.dirty
    restore(window, qtbot, redo=True)
    assert not window.project.segments
    assert not window.selected_segment_id
    assert window.segment_table.rowCount() == 0
    assert window.dirty


@pytest.mark.parametrize("confirmed", [True, False])
def test_dont_ask_again_saved_only_after_confirmation(window, qtbot, monkeypatch, confirmed):
    window.settings.setValue(CONFIRM_DELETE_SETTING, True)

    def confirm(box):
        assert box.checkBox().text() == "Don't ask again"
        assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
        box.checkBox().setChecked(True)
        return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    window.delete_segment()
    reopened_settings = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert reopened_settings.value(CONFIRM_DELETE_SETTING, True, type=bool) is not confirmed
    if confirmed:
        restore(window, qtbot)
        monkeypatch.setattr(QMessageBox, "exec", lambda *_: pytest.fail("Preference was not kept"))
        window.delete_segment()
    else:
        assert not window.edit_history.history.can_undo
        assert not window.dirty


def test_history_dialog_and_preference_do_not_change_project(window, qtbot):
    before = window.project.to_dict()
    window.duplicate_segment()
    window.action_history.trigger()
    controller = window.edit_history
    assert controller._dialog.isVisible()
    assert not controller._dialog.isModal()
    assert controller._list.count() == 2
    controller.set_confirm_delete(True)
    assert window.settings.value(CONFIRM_DELETE_SETTING, False, type=bool)
    controller._list.setCurrentRow(0)
    qtbot.mouseClick(controller._restore_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller.busy)
    assert window.project.to_dict() == before
    assert controller._list.currentRow() == 0
    assert window.action_redo.isEnabled()
    controller._dialog.reject()


def test_history_never_auto_opens_or_adds_persistent_workspace_controls(window, qtbot):
    first = window.active_editor
    window.show()
    qtbot.waitUntil(lambda: first._layout_restored)
    first.duplicate_segment()
    second = window.add_project(PackProject(title="Another project"), dirty=False)
    for editor in (first, second, first, second):
        window.tabs.setCurrentWidget(editor)
        assert editor.edit_history._dialog is None
        assert editor.action_history in window.project_menu.actions()
        history_actions = {editor.action_undo, editor.action_redo, editor.action_history}
        assert all(
            button.defaultAction() not in history_actions
            for button in editor.findChildren(QToolButton)
        )
        assert all(editor.project_toolbar.widgetForAction(action) is None for action in history_actions)
    assert window.findChildren(QDockWidget) == []
    assert not any(dialog.windowTitle() == "Edit History" for dialog in window.findChildren(QDialog))


@pytest.mark.parametrize("operation", ["add", "duplicate", "split", "range", "combine", "suggestions"])
def test_segment_operations_are_single_reversible_edits(window, qtbot, monkeypatch, operation):
    before = window.project.to_dict()
    segment = window.project.segments[0]
    if operation == "add":
        window.add_segment()
    elif operation == "duplicate":
        window.duplicate_segment()
    elif operation == "split":
        monkeypatch.setattr(window, "current_position", lambda: 2)
        window.split_segment()
    elif operation == "range":
        window.mark_out_spin.setValue(4)
        window.apply_selected_range()
    elif operation == "combine":
        window.project.add_segment(Segment(3, 5, "Second", ["Bob"]))
        window._set_project(window.project, None, mark_dirty=False)
        before = window.project.to_dict()
        window.segment_table.selectAll()
        window.combine_segments()
    else:
        window._add_analysis_suggestions([AnalysisSuggestion(5, 6, "Suggested", "Whisper")])
    after = window.project.to_dict()
    assert before != after, operation
    assert len(window.edit_history.history.labels) == 1
    restore(window, qtbot)
    assert window.project.to_dict() == before
    restore(window, qtbot, redo=True)
    assert window.project.to_dict() == after
    if operation != "combine":
        assert window.project.segment_by_id(segment.id) is not None


def test_timeline_drag_is_one_edit_and_cancel_does_not_add_history(window, qtbot):
    segment = window.project.segments[0]
    window._timeline_range_edit_started(segment.id, 1, 3)
    for end in (4, 5, 6):
        window._timeline_range_changed(segment.id, 1, end)
    assert not window.edit_history.history.can_undo
    window._timeline_range_edit_finished(segment.id, 1, 3, 1, 6)
    assert window.edit_history.history.labels == ("Change segment timing",)
    restore(window, qtbot)
    assert window.project.segments[0].end == 3
    window._timeline_range_edit_started(segment.id, 1, 3)
    window._timeline_range_changed(segment.id, 1, 5)
    window._timeline_range_edit_finished(segment.id, 1, 3, 1, 3)
    assert window.edit_history.history.index == 0
    assert window.edit_history.history.can_redo
    assert not window.dirty


@pytest.mark.parametrize("field", ["caption_edit", "speakers_edit", "title_edit", "authors_edit", "readme_edit"])
def test_typing_coalesces_and_redo_branch_is_replaced(window, qtbot, field):
    before = window.project.to_dict()
    widget = getattr(window, field)
    widget.selectAll()
    qtbot.keyClicks(widget, "Changed")
    assert len(window.edit_history.history.labels) == 1
    after = window.project.to_dict()
    restore(window, qtbot)
    assert window.project.to_dict() == before
    restore(window, qtbot, redo=True)
    assert window.project.to_dict() == after
    restore(window, qtbot)
    window.duplicate_segment()
    assert not window.edit_history.history.can_redo


def test_coalesced_text_undo_and_noop_edits_preserve_clean_state(window, qtbot):
    original = window.caption_edit.toPlainText()
    window.caption_edit.setPlainText("Changed")
    assert window.dirty
    window.caption_edit.setPlainText(original)
    assert not window.dirty
    assert not window.edit_history.history.can_undo
    window.clear_icon()
    assert not window.dirty
    assert not window.edit_history.history.can_undo


@pytest.mark.parametrize("operation", [
    "icon", "clear-icon", "backing", "clear-backing", "prompt-audio", "video-audio",
    "image", "clear-image", "export-options", "draft", "source", "speaker-exclusion",
    "matching-preference",
])
def test_project_fields_round_trip(window, qtbot, monkeypatch, operation, tmp_path):
    segment = window.project.segments[0]
    if operation == "clear-icon":
        window.project.icon_path = "previous.png"
    elif operation == "clear-backing":
        window.project.backing_track_path = "previous.mp3"
    elif operation == "video-audio":
        segment.audio_mode, segment.audio_path = "file", "previous.mp3"
    elif operation == "clear-image":
        segment.image_path = "previous.png"
    elif operation == "speaker-exclusion":
        segment.characters = []
    window._set_project(window.project, None, mark_dirty=False)
    window.select_segment(segment.id)
    before = window.project.to_dict()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(tmp_path / "chosen"), ""))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: QMessageBox.StandardButton.Yes)
    operations = {
        "icon": window.choose_icon,
        "clear-icon": window.clear_icon,
        "backing": window.choose_backing_track,
        "clear-backing": window.clear_backing_track,
        "prompt-audio": window.choose_segment_audio,
        "video-audio": window.use_video_audio,
        "image": window.choose_segment_image,
        "clear-image": window.clear_segment_image,
        "source": window.clear_source_video,
        "speaker-exclusion": lambda: window._speaker_exclusion_changed(True),
        "matching-preference": lambda: window.speaker_matching.enabled_check.setChecked(True),
        "export-options": lambda: window._apply_export_options(
            replace(ExportOptions.from_project(window.project), head_padding=0.4)
        ),
        "draft": lambda: window._save_analysis_review(AnalysisReview(
            local_rows=[AnalysisDraftRow("1", "3", "Draft", "Whisper")],
        )),
    }
    operations[operation]()
    after = window.project.to_dict()
    assert after != before
    assert len(window.edit_history.history.labels) == 1
    restore(window, qtbot)
    assert window.project.to_dict() == before
    restore(window, qtbot, redo=True)
    assert window.project.to_dict() == after


@pytest.mark.parametrize("key,modifiers", [
    (Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier),
    (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier),
])
@pytest.mark.parametrize("widget_name", ["timeline", "segment_table", "video_widget"])
def test_history_shortcuts_outside_text_fields(window, qtbot, widget_name, key, modifiers):
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.waitUntil(lambda: window._layout_restored)
    window.duplicate_segment()
    widget = getattr(window, widget_name)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)
    qtbot.keyClick(widget, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: len(window.project.segments) == 1 and not window.edit_history.busy)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)
    qtbot.keyClick(widget, key, modifiers)
    qtbot.waitUntil(lambda: len(window.project.segments) == 2 and not window.edit_history.busy)


@pytest.mark.parametrize("widget_name", ["title_edit", "authors_edit", "readme_edit", "speakers_edit", "caption_edit"])
def test_text_shortcuts_use_native_undo_without_starting_replay(window, qtbot, widget_name):
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.waitUntil(lambda: window._layout_restored)
    widget = getattr(window, widget_name)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)
    widget.selectAll()
    qtbot.keyClicks(widget, "Native")
    before = window.edit_history.history.index
    qtbot.keyClick(widget, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not window.edit_history.busy
    text = widget.toPlainText() if hasattr(widget, "toPlainText") else widget.text()
    assert text != "Native"
    assert window.edit_history.history.index <= before + 1


def test_undo_works_immediately_after_duplicate_focuses_an_unedited_text_field(window, qtbot):
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.waitUntil(lambda: window._layout_restored)
    window.duplicate_segment()
    qtbot.waitUntil(window.speakers_edit.hasFocus)
    assert not window.speakers_edit.isUndoAvailable()
    qtbot.keyClick(window.speakers_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: len(window.project.segments) == 1 and not window.edit_history.busy)


def test_shortcuts_in_dialog_do_not_change_project(window, qtbot):
    window.duplicate_segment()
    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    button = QPushButton("Focus", dialog)
    dialog.show()
    dialog.activateWindow()
    button.setFocus()
    qtbot.waitUntil(button.hasFocus)
    qtbot.keyClick(button, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not window.edit_history.busy
    assert len(window.project.segments) == 2
    dialog.close()


def test_history_is_per_tab_and_new_document_clears_it(window, qtbot):
    first = window.active_editor
    first.duplicate_segment()
    second = window.add_project(PackProject(title="Second"), dirty=False)
    assert not second.action_undo.isEnabled()
    assert first.action_undo not in window.project_menu.actions()
    second.title_edit.setText("Changed")
    second._commit_editors()
    restore(window, qtbot)
    assert second.project.title == "Second"
    assert len(first.project.segments) == 2
    window.tabs.setCurrentWidget(first)
    restore(window, qtbot)
    assert len(first.project.segments) == 1
    first._set_project(PackProject(title="Replacement"), None, mark_dirty=False)
    assert not first.edit_history.history.labels


def test_save_points_survive_undo_redo_without_rewriting_saved_file(window, qtbot, tmp_path):
    destination = tmp_path / "saved.cvpack.json"
    window.duplicate_segment()
    assert window.save_editor(window.active_editor, destination=destination)
    qtbot.waitUntil(lambda: not window.dirty)
    saved_bytes = destination.read_bytes()
    restore(window, qtbot)
    assert window.dirty
    assert destination.read_bytes() == saved_bytes
    restore(window, qtbot, redo=True)
    assert not window.dirty
    assert len(ProjectStore.load(destination).segments) == 2


def test_save_completion_after_undo_marks_the_saved_history_state(window, qtbot, tmp_path, monkeypatch):
    started, release = Event(), Event()
    save = ProjectStore.save

    def delayed(project, destination):
        started.set()
        assert release.wait(5)
        save(project, destination)

    monkeypatch.setattr(ProjectStore, "save", delayed)
    window.duplicate_segment()
    window.save_editor(window.active_editor, destination=tmp_path / "saved.cvpack.json")
    try:
        qtbot.waitUntil(started.is_set)
        restore(window, qtbot)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert window.dirty
    restore(window, qtbot, redo=True)
    assert not window.dirty


def test_gui_and_mcp_save_reservation_refuse_mid_drag_snapshots(window, qtbot, tmp_path, monkeypatch):
    destination = tmp_path / "saved.cvpack.json"
    ProjectStore.save(window.project, destination)
    original = destination.read_bytes()
    notices = []
    monkeypatch.setattr(window, "notice", lambda *args: notices.append(args))
    segment = window.project.segments[0]
    window._timeline_range_edit_started(segment.id, 1, 3)
    window._timeline_range_changed(segment.id, 1, 4)
    assert not window.save_editor(window.active_editor, destination=destination)
    with pytest.raises(ValueError, match="Finish the current timing edit"):
        window.reserve_project_save(window.session.id, destination)
    assert notices and "Finish the current timing edit" in notices[0][1]
    assert destination.read_bytes() == original
    window._timeline_range_edit_finished(segment.id, 1, 3, 1, 5)
    restore(window, qtbot)
    assert window.project.segments[0].end == 3
    assert not window.dirty


def test_completed_cut_redoes_by_restoring_references_without_running_media_again(
    window, qtbot, tmp_path, monkeypatch,
):
    from choicer_voicer_pack_creator.ui import main_window

    before = window.project.to_dict()
    result = window.session.snapshot()
    result.video_path = str(tmp_path / "cut.mkv")
    result.video_duration = 18
    result.backing_track_path = str(tmp_path / "cut-backing.wav")
    result.segments[0].end = 2
    execute = Mock(return_value=result)
    monkeypatch.setattr(main_window, "execute_scene_edit", execute)
    window._start_scene_edit(2, 4, "cut", tmp_path)
    qtbot.waitUntil(lambda: window._scene_job is None)
    assert window.edit_history.history.labels == ("Cut video range",)
    after = window.project.to_dict()
    restore(window, qtbot)
    assert window.project.to_dict() == before
    restore(window, qtbot, redo=True)
    assert window.project.to_dict() == after
    execute.assert_called_once()


def test_cancelled_replay_keeps_history_index_and_document(window, qtbot, monkeypatch):
    window.duplicate_segment()
    before = window.project.to_dict()
    started, release = Event(), Event()
    prepare = ReplayView.prepare

    def delayed(project):
        started.set()
        assert release.wait(5)
        return prepare(project)

    monkeypatch.setattr(ReplayView, "prepare", delayed)
    window.action_undo.trigger()
    try:
        qtbot.waitUntil(started.is_set)
        job = next(job for job in window.job_manager.active_jobs() if job.kind == "edit-history")
        window.job_manager.cancel(job.id)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.edit_history.busy)
    assert window.project.to_dict() == before
    assert window.edit_history.history.index == 1
    assert window.action_undo.isEnabled()


def test_replay_is_off_thread_and_stale_results_keep_new_edits(window, qtbot, monkeypatch):
    window.duplicate_segment()
    started, release = Event(), Event()
    original = ReplayView.prepare
    threads = []

    def prepare(project):
        threads.append(QThread.currentThread() == QApplication.instance().thread())
        started.set()
        assert release.wait(5)
        return original(project)

    monkeypatch.setattr(ReplayView, "prepare", prepare)
    window.action_undo.trigger()
    try:
        qtbot.waitUntil(started.is_set)
        assert window.edit_history.busy
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
        window.project.title = "Newer result"
        window._set_dirty(True, fields_only=True)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.edit_history.busy)
    assert threads == [False]
    assert window.project.title == "Newer result"
    assert len(window.project.segments) == 2
    assert "newer edits were kept" in window.statusBar().currentMessage()


def test_large_replay_yields_between_rows_and_releases_job_result(window, qtbot, monkeypatch):
    window.project.segments = [
        Segment(index, index + 0.5, f"Line {index}", ["Alice"]) for index in range(2000)
    ]
    window.project.video_duration = 2000
    window._set_dirty(True, history_label="Large batch")
    restore(window, qtbot)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(0)
    threads = []
    validate = PackProject.validate

    def track_validate(project):
        threads.append(QThread.currentThread() == QApplication.instance().thread())
        return validate(project)

    monkeypatch.setattr(PackProject, "validate", track_validate)
    restore(window, qtbot, redo=True)
    timer.stop()
    assert len(ticks) > 2
    assert threads and not any(threads)
    assert window.segment_table.rowCount() == 2000
    assert window.segment_table.item(1999, 4).text() == "Line 1999"
    records = [job for job in window.job_manager.tasks() if job.kind == "edit-history"]
    assert all(job.result is None for job in records)


def test_failed_replay_keeps_history_and_reenables_editor(window, qtbot, monkeypatch):
    window.duplicate_segment()
    before = window.project.to_dict()
    notices = []
    monkeypatch.setattr(window, "notice", lambda *args: notices.append(args))

    def fail(project):
        raise RuntimeError("Cannot prepare history")

    monkeypatch.setattr(ReplayView, "prepare", fail)
    restore(window, qtbot)
    assert window.project.to_dict() == before
    assert window.action_undo.isEnabled()
    assert window.editor_splitter.isEnabled()
    assert notices == [("Could not restore edit history", "RuntimeError: Cannot prepare history")]
