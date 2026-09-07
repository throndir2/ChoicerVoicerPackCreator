from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(editor)
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    monkeypatch.setattr(editor.speaker_matching, "name_committed", Mock())
    yield editor
    editor.dirty = False
    editor.close()


def assert_selected(window, segment):
    assert window.selected_segment_id == segment.id
    assert window.timeline.selected_id == segment.id
    assert window.timeline.mark_segment_id == segment.id
    assert (window.timeline.mark_in, window.timeline.mark_out) == (segment.start, segment.end)
    assert (window.mark_in_spin.value(), window.mark_out_spin.value()) == (
        segment.start, segment.end,
    )
    assert window.caption_edit.toPlainText() == segment.caption
    assert window.speakers_edit.text() == ", ".join(segment.characters)
    assert window._selected_table_ids() == [segment.id]


def test_button_visits_unassigned_lines_in_order_and_wraps(window, qtbot, monkeypatch):
    first = Segment(1, 2, "First")
    assigned = Segment(2, 3, "Named", ["Alice"])
    automatic = Segment(3, 4, "Matched", ["Bob"], speaker_assignment="automatic")
    simultaneous = Segment(3, 4, "No name yet")
    twin = Segment(3, 4, "Another simultaneous line")
    last = Segment(5, 6, "Last")
    window._set_project(
        PackProject(
            video_duration=10, segments=[last, first, assigned, automatic, simultaneous, twin],
        ),
        None, mark_dirty=False,
    )
    before = window.project.to_dict()
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    seek = Mock()
    monkeypatch.setattr(window.player, "setPosition", seek)
    button = window.next_unassigned_button
    assert button.isVisible() and button.isEnabled()
    assert button.defaultAction() is window.action_next_unassigned
    assert button.accessibleName() == "Next Line Without a Speaker"
    assert window.action_next_unassigned in window.segments_menu.actions()

    for segment in (first, simultaneous, twin, last, first):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert_selected(window, segment)
        assert window.speakers_edit.hasFocus()
        seek.assert_called_with(int(segment.start * 1000))

    assert window.project.to_dict() == before
    assert not window.dirty


def test_multiple_selected_rows_start_navigation_at_first_unassigned_line(window):
    first = Segment(1, 2, "First", ["Alice"])
    target = Segment(2, 3, "No speaker")
    last = Segment(3, 4, "Last", ["Bob"])
    window._set_project(
        PackProject(video_duration=5, segments=[first, target, last]),
        None, mark_dirty=False,
    )
    window.select_segment(first.id)
    window.segment_table.selectionModel().select(
        window.segment_table.model().index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert not window.selected_segment_id
    assert len(window._selected_table_ids()) == 2
    window.action_next_unassigned.trigger()
    assert_selected(window, target)
    assert not window.dirty


@pytest.mark.parametrize("characters", [[], [""], [" ", "\t"]])
@pytest.mark.parametrize("assignment", ["manual", "automatic", "excluded"])
def test_empty_names_include_lines_excluded_from_auto_fill(
    window, characters, assignment,
):
    segment = Segment(1, 2, "Needs a speaker", characters, speaker_assignment=assignment)
    window._set_project(
        PackProject(video_duration=5, segments=[segment]), None, mark_dirty=False,
    )
    window.action_next_unassigned.trigger()
    assert_selected(window, segment)
    assert segment.speaker_assignment == assignment
    assert not window.dirty


@pytest.mark.parametrize("kind", ["empty", "assigned", "only-current"])
def test_no_other_match_leaves_selection_and_playback_unchanged(window, monkeypatch, kind):
    segment = Segment(1, 2, "Current", [] if kind == "only-current" else ["Alice", "Bob"])
    window._set_project(
        PackProject(video_duration=5, segments=[] if kind == "empty" else [segment]),
        None, mark_dirty=False,
    )
    if kind != "empty":
        window.select_segment(segment.id)
    before = window.project.to_dict()
    seek = Mock()
    prompt_stop = Mock()
    monkeypatch.setattr(window.player, "setPosition", seek)
    monkeypatch.setattr(window.prompt_player, "stop", prompt_stop)
    window.action_next_unassigned.trigger()
    assert window.statusBar().currentMessage() == "No other lines without a speaker."
    if kind != "empty":
        assert_selected(window, segment)
    else:
        assert not window.selected_segment_id
    seek.assert_not_called()
    prompt_stop.assert_not_called()
    assert window.project.to_dict() == before
    assert not window.dirty


def test_navigation_preserves_live_edits_and_uses_updated_assignments(window, qtbot):
    first = Segment(1, 2, "First")
    second = Segment(2, 3, "Second")
    third = Segment(3, 4, "Third")
    window._set_project(
        PackProject(video_duration=5, segments=[first, second, third]),
        None, mark_dirty=False,
    )
    window.select_segment(first.id)
    qtbot.keyClicks(window.speakers_edit, "Alice, Bob")
    window.caption_edit.setPlainText("Edited line")
    window.action_next_unassigned.trigger()
    assert_selected(window, second)
    assert first.characters == ["Alice", "Bob"]
    assert first.caption == "Edited line"
    assert window.segment_table.item(0, 3).text() == "Alice, Bob"
    assert window.segment_table.item(0, 4).text() == "Edited line"
    window.action_next_unassigned.trigger()
    assert_selected(window, third)
    window.action_next_unassigned.trigger()
    assert_selected(window, second)
    assert window.dirty


def test_navigation_scrolls_to_target_and_stops_prompt_preview(window, qtbot, monkeypatch):
    segments = [Segment(index, index + 1, f"Line {index}", ["Alice"]) for index in range(80)]
    target = segments[-1]
    target.characters = []
    target.audio_mode = "file"
    target.audio_path = "preserved.mp3"
    target.source_range_known = False
    window._set_project(
        PackProject(video_duration=80, segments=segments), None, mark_dirty=False,
    )
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.inspector_splitter.setSizes([150, 300, 200])
    window.select_segment(segments[0].id)
    window._preview_end = 1
    prompt_stop = Mock()
    monkeypatch.setattr(window.prompt_player, "stop", prompt_stop)
    table = window.segment_table
    last = table.item(79, 0)
    assert not table.viewport().rect().intersects(table.visualItemRect(last))

    window.action_next_unassigned.trigger()
    assert_selected(window, target)
    assert table.viewport().rect().contains(table.visualItemRect(last))
    prompt_stop.assert_called_once_with()
    assert window._preview_end is None
    assert target.audio_mode == "file"
    assert target.audio_path == "preserved.mp3"
    assert not target.source_range_known
    assert not window.dirty


def test_navigation_targets_active_tab_and_is_disabled_while_loading(window):
    first = window.active_editor
    first._set_project(
        PackProject(video_duration=5, segments=[Segment(1, 2, "First project")]),
        None, mark_dirty=False,
    )
    target = Segment(2, 3, "Second project")
    second = window.add_project(PackProject(video_duration=5, segments=[target]), dirty=False)
    action = second.action_next_unassigned
    assert action in window.segments_menu.actions()
    assert first.action_next_unassigned not in window.segments_menu.actions()
    second._set_loading(True)
    assert not action.isEnabled()
    assert not second.next_unassigned_button.isEnabled()
    second._set_loading(False)
    assert action.isEnabled()
    action.trigger()
    assert_selected(second, target)
    assert not first.selected_segment_id
    assert not first.dirty and not second.dirty
