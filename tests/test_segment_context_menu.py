from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.history import CONFIRM_DELETE_SETTING
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    qtbot.addWidget(editor)
    editor._set_project(
        PackProject(
            video_duration=10, auto_speaker_matching=False,
            segments=[
                Segment(1, 2, "First line", ["Alice"]),
                Segment(3, 4, "Middle line", ["Bob"]),
                Segment(5, 6, "Last line", ["Carol"]),
            ],
        ), None, mark_dirty=False,
    )
    editor.show()
    qtbot.waitUntil(lambda: editor._layout_restored)
    yield editor
    editor._segment_context_menu.hide()
    qtbot.waitUntil(lambda: not editor.edit_history.busy, timeout=10000)
    editor.dirty = False
    editor.close()
    qtbot.waitUntil(lambda: not editor.job_manager.active_jobs(), timeout=10000)


def click_block(window, qtbot, row, modifier=Qt.KeyboardModifier.NoModifier):
    timeline = window.timeline
    point = timeline._segment_rect(window.project.segments[row]).center().toPoint()
    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, modifier, pos=point)


def context_menu(window, qtbot, surface, row, *, keyboard=False):
    segment = window.project.segments[row]
    if surface == "timeline":
        widget = window.timeline
        point = widget._segment_rect(segment).center().toPoint()
    else:
        table = window.segment_table
        item = table.item(row, 0)
        table.scrollToItem(item)
        widget = table.viewport()
        point = table.visualItemRect(item).center()
    if not keyboard:
        qtbot.mouseClick(widget, Qt.MouseButton.RightButton, pos=point)
    reason = QContextMenuEvent.Reason.Keyboard if keyboard else QContextMenuEvent.Reason.Mouse
    QApplication.sendEvent(widget, QContextMenuEvent(reason, point, widget.mapToGlobal(point)))
    assert window._segment_context_menu.isVisible()
    return window._segment_context_menu


def trigger_menu_action(menu, action, qtbot):
    assert action in menu.actions()
    assert action.isEnabled()
    qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())


@pytest.mark.parametrize("modifier", [
    Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier,
])
def test_waveform_modifier_selection_toggles_individual_blocks_without_seeking(
    window, qtbot, monkeypatch, modifier,
):
    before = window.project.to_dict()
    first, middle, last = window.project.segments
    click_block(window, qtbot, 0)
    seeks = []
    monkeypatch.setattr(window, "seek", seeks.append)
    click_block(window, qtbot, 2, modifier)
    assert set(window._selected_table_ids()) == {first.id, last.id}
    assert window.timeline.selected_ids == {first.id, last.id}
    assert window.timeline.selected_id == ""
    assert window.timeline.mark_segment_id == ""
    assert not window.caption_edit.isEnabled()
    assert window.action_combine.isEnabled()
    window._refresh_table()
    assert window.timeline.selected_ids == {first.id, last.id}
    click_block(window, qtbot, 0, modifier)
    assert window.selected_segment() is last
    assert window.timeline.selected_ids == {last.id}
    assert window.caption_edit.isEnabled()
    click_block(window, qtbot, 2, modifier)
    assert window.timeline.selected_ids == set()
    assert not window._selected_table_ids()
    assert not window.action_duplicate.isEnabled()
    assert not window.action_delete.isEnabled()
    assert not seeks
    click_block(window, qtbot, 1)
    assert window.selected_segment() is middle
    assert window.timeline.selected_ids == {middle.id}
    assert seeks
    assert window.project.to_dict() == before
    assert not window.dirty


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_context_menu_preserves_group_or_targets_single_without_seeking(
    window, qtbot, monkeypatch, surface,
):
    first, middle, last = window.project.segments
    click_block(window, qtbot, 0)
    click_block(window, qtbot, 2, Qt.KeyboardModifier.ShiftModifier)
    seeks = []
    monkeypatch.setattr(window, "seek", seeks.append)
    menu = context_menu(window, qtbot, surface, 2)
    assert set(window._selected_table_ids()) == {first.id, last.id}
    assert window.action_combine.isEnabled()
    assert window.action_duplicate.text() == "Duplicate 2 Segments"
    assert window.action_delete.text() == "Delete 2 Segments"
    assert not window.action_preview.isEnabled()
    assert not window.action_split.isEnabled()
    menu.hide()
    menu = context_menu(window, qtbot, surface, 1)
    assert window.selected_segment() is middle
    assert window.timeline.selected_ids == {middle.id}
    assert not window.action_combine.isEnabled()
    assert window.action_duplicate.isEnabled()
    assert window.action_delete.isEnabled()
    assert window.action_preview.isEnabled()
    assert window.action_split.isEnabled()
    assert seeks == []
    assert not window.dirty


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_single_context_menu_targets_segment_with_no_prior_selection(window, qtbot, surface):
    assert window._selected_table_ids() == []
    context_menu(window, qtbot, surface, 1)
    assert window.selected_segment() is window.project.segments[1]
    assert window.timeline.selected_ids == {window.project.segments[1].id}


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_keyboard_context_menu_and_escape_keep_selection(window, qtbot, surface):
    click_block(window, qtbot, 0)
    click_block(window, qtbot, 2, Qt.KeyboardModifier.ShiftModifier)
    selected = window.timeline.selected_ids.copy()
    menu = context_menu(window, qtbot, surface, 0, keyboard=True)
    assert window.timeline.selected_ids == selected
    qtbot.keyClick(menu, Qt.Key.Key_Escape)
    assert not menu.isVisible()
    assert window.timeline.selected_ids == selected
    assert not window.dirty


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_right_click_empty_space_does_not_open_segment_menu(window, qtbot, surface):
    click_block(window, qtbot, 0)
    before = window._selected_table_ids()
    widget = window.timeline if surface == "timeline" else window.segment_table.viewport()
    point = QPoint(widget.width() - 2, widget.height() - 2)
    qtbot.mouseClick(widget, Qt.MouseButton.RightButton, pos=point)
    QApplication.sendEvent(widget, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, widget.mapToGlobal(point),
    ))
    assert not window._segment_context_menu.isVisible()
    assert window._selected_table_ids() == before


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_menu_merges_only_selected_segments_and_can_be_saved(window, qtbot, tmp_path, surface):
    first, middle, last = window.project.segments
    click_block(window, qtbot, 0)
    click_block(window, qtbot, 2, Qt.KeyboardModifier.ShiftModifier)
    menu = context_menu(window, qtbot, surface, 2)
    trigger_menu_action(menu, window.action_combine, qtbot)
    merged = window.selected_segment()
    assert (merged.start, merged.end) == (first.start, last.end)
    assert merged.caption == "First line Last line"
    assert merged.characters == ["Alice", "Carol"]
    assert window.project.segments == [merged, middle]
    assert window.timeline.selected_ids == {merged.id}
    window.project_path = tmp_path / "merged.cvpack.json"
    assert window.save_project()
    qtbot.waitUntil(lambda: not window.dirty)
    assert ProjectStore.load(window.project_path).to_dict() == window.project.to_dict()


@pytest.mark.parametrize("operation", ["duplicate", "delete"])
def test_group_actions_preserve_media_and_are_one_reversible_edit(
    window, qtbot, monkeypatch, tmp_path, operation,
):
    audio, image = tmp_path / "prompt.mp3", tmp_path / "still.png"
    audio.write_bytes(b"preserved audio")
    image.write_bytes(b"preserved image")
    first, middle, last = window.project.segments
    first.audio_mode, first.audio_path = "file", str(audio)
    first.source_range_known = False
    last.image_path = str(image)
    window._set_project(window.project, None, mark_dirty=False)
    before = window.project.to_dict()
    click_block(window, qtbot, 0)
    click_block(window, qtbot, 2, Qt.KeyboardModifier.ShiftModifier)
    confirmations = []

    def confirm(box):
        confirmations.append(box.text())
        assert "2 selected segments" in box.text()
        assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    menu = context_menu(window, qtbot, "timeline", 0)
    action = window.action_duplicate if operation == "duplicate" else window.action_delete
    trigger_menu_action(menu, action, qtbot)
    if operation == "duplicate":
        assert not confirmations
        assert len(window.project.segments) == 5
        originals = {first.id, middle.id, last.id}
        duplicates = [item for item in window.project.segments if item.id not in originals]
        for original, duplicate in zip((first, last), duplicates, strict=True):
            assert original.id != duplicate.id
            assert original.to_dict() | {"id": duplicate.id} == duplicate.to_dict()
            assert original.characters is not duplicate.characters
        assert window.timeline.selected_ids == {item.id for item in duplicates}
    else:
        assert len(confirmations) == 1
        assert window.project.segments == [middle]
        assert window.timeline.selected_ids == set()
    after = window.project.to_dict()
    assert len(window.edit_history.history.labels) == 1
    window.action_undo.trigger()
    qtbot.waitUntil(lambda: not window.edit_history.busy, timeout=10000)
    assert window.project.to_dict() == before
    window.action_redo.trigger()
    qtbot.waitUntil(lambda: not window.edit_history.busy, timeout=10000)
    assert window.project.to_dict() == after
    assert audio.read_bytes() == b"preserved audio"
    assert image.read_bytes() == b"preserved image"


def test_cancel_group_deletion_keeps_selection_and_preference(window, qtbot, monkeypatch):
    click_block(window, qtbot, 0)
    click_block(window, qtbot, 2, Qt.KeyboardModifier.ShiftModifier)
    before, selected = window.project.to_dict(), window.timeline.selected_ids.copy()

    def cancel(box):
        box.checkBox().setChecked(True)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", cancel)
    menu = context_menu(window, qtbot, "table", 0)
    trigger_menu_action(menu, window.action_delete, qtbot)
    assert window.project.to_dict() == before
    assert window.timeline.selected_ids == selected
    assert window.settings.value(CONFIRM_DELETE_SETTING, True, type=bool)
    assert not window.edit_history.history.can_undo


def test_context_menu_keeps_target_during_playback_and_loading_closes_it(
    window, qtbot, monkeypatch,
):
    first, middle, _last = window.project.segments
    context_menu(window, qtbot, "timeline", 0)
    monkeypatch.setattr(
        window.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState,
    )
    window.player.positionChanged.emit(3500)
    assert window.selected_segment() is first
    window._segment_context_menu.hide()
    window.player.positionChanged.emit(3500)
    assert window.selected_segment() is middle
    context_menu(window, qtbot, "timeline", 0)
    window._set_loading(True)
    assert not window._segment_context_menu.isVisible()
    assert not window.action_combine.isEnabled()
    assert not window.action_duplicate.isEnabled()
    assert not window.action_delete.isEnabled()
    window._set_loading(False)
    assert window.action_duplicate.isEnabled()
    assert window.action_delete.isEnabled()


def test_right_drag_keeps_playback_follow_from_shifting_view_or_selection(
    window, qtbot, monkeypatch,
):
    click_block(window, qtbot, 0)
    timeline = window.timeline
    before = window.project.to_dict()
    selected = window.selected_segment()
    timeline.set_zoom(2, anchor_time=3)
    original_offset = timeline.offset
    point = timeline._segment_rect(window.project.segments[1]).center().toPoint()
    seeks = []
    monkeypatch.setattr(window.player, "setPosition", seeks.append)
    monkeypatch.setattr(
        window.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState,
    )
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseMove(timeline, point - QPoint(100, 0))
    offset = timeline.offset
    assert offset > original_offset
    window.player.positionChanged.emit(5500)
    assert timeline.playhead == 5.5
    assert timeline.offset == offset
    assert window.selected_segment() is selected
    assert window._range_edit_record is None
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point - QPoint(100, 0))
    QApplication.sendEvent(timeline, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, timeline.mapToGlobal(point),
    ))
    assert not window._segment_context_menu.isVisible()
    assert not seeks
    assert window.project.to_dict() == before
    assert not window.dirty
    assert not window.edit_history.history.can_undo
    window.player.positionChanged.emit(5500)
    assert window.selected_segment() is window.project.segments[2]


@pytest.mark.parametrize("surface", ["timeline", "table"])
def test_right_click_does_not_move_playhead_before_split(window, qtbot, monkeypatch, surface):
    click_block(window, qtbot, 0)
    position = [3.5]
    monkeypatch.setattr(window, "current_position", lambda: position[0])
    monkeypatch.setattr(window, "seek", lambda value: position.__setitem__(0, value))
    menu = context_menu(window, qtbot, surface, 1)
    assert position == [3.5]
    trigger_menu_action(menu, window.action_split, qtbot)
    assert [(item.start, item.end) for item in window.project.segments] == [
        (1, 2), (3, 3.5), (3.5, 4), (5, 6),
    ]
