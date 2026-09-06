from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtMultimedia import QMediaPlayer

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
    yield editor
    editor.dirty = False
    editor.close()


def start_playback(window, monkeypatch, position_ms: int = 0) -> None:
    monkeypatch.setattr(
        window.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState
    )
    monkeypatch.setattr(window.player, "position", lambda: position_ms)
    window.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)


def assert_selected(window, segment: Segment) -> None:
    assert window.selected_segment_id == segment.id
    assert window.timeline.selected_id == segment.id
    assert window.timeline.mark_segment_id == segment.id
    assert (window.timeline.mark_in, window.timeline.mark_out) == (segment.start, segment.end)
    assert window.mark_in_spin.value() == segment.start
    assert window.mark_out_spin.value() == segment.end
    assert window.speakers_edit.text() == ", ".join(segment.characters)
    assert window.caption_edit.toPlainText() == segment.caption
    row = window.segment_table.selectedItems()[0].row()
    assert window.segment_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == segment.id


def test_playback_follows_boundaries_gaps_overlaps_and_backward_seeks(window, monkeypatch):
    first = Segment(1, 2, "First", ["A"])
    second = Segment(2, 3, "Second", ["B"])
    overlap = Segment(2.5, 4, "Overlapping line", ["C"])
    later = Segment(5, 6, "", ["D"])
    window._set_project(
        PackProject(video_duration=10, segments=[later, overlap, second, first]),
        None, mark_dirty=False,
    )
    before = window.project.to_dict()
    seek = Mock()
    prompt_stop = Mock()
    pause = Mock()
    monkeypatch.setattr(window.player, "setPosition", seek)
    monkeypatch.setattr(window.prompt_player, "stop", prompt_stop)
    monkeypatch.setattr(window.player, "pause", pause)
    start_playback(window, monkeypatch)
    window.player.positionChanged.emit(999)
    assert not window.selected_segment_id

    for milliseconds, expected in (
        (1000, first),
        (1999, first),
        (2000, second),
        (2500, overlap),
        (3000, overlap),
        (4000, overlap),
        (4999, overlap),
        (5250, later),
        (1500, first),
        (0, first),
    ):
        window.player.positionChanged.emit(milliseconds)
        assert_selected(window, expected)
        assert window.timeline.playhead == milliseconds / 1000

    seek.assert_not_called()
    prompt_stop.assert_not_called()
    pause.assert_not_called()
    assert window.project.to_dict() == before
    assert not window.dirty


def test_playback_start_selects_current_segment_without_waiting_for_a_tick(window, monkeypatch):
    segment = Segment(1, 3, "Current line", ["A"])
    window._set_project(
        PackProject(video_duration=5, segments=[segment]), None, mark_dirty=False
    )
    start_playback(window, monkeypatch, 1500)
    assert_selected(window, segment)
    assert not window.dirty


def test_playback_scrolls_only_when_the_selected_segment_changes(window, qtbot, monkeypatch):
    segments = [Segment(index, index + 1, f"Line {index}", ["A"]) for index in range(80)]
    window._set_project(
        PackProject(video_duration=80, segments=segments), None, mark_dirty=False
    )
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.inspector_splitter.setSizes([150, 300, 200])
    start_playback(window, monkeypatch)

    table = window.segment_table
    last = table.item(79, 0)
    assert not table.viewport().rect().intersects(table.visualItemRect(last))
    window.player.positionChanged.emit(79_250)
    assert_selected(window, segments[-1])
    assert table.viewport().rect().contains(table.visualItemRect(last))

    table.verticalScrollBar().setValue(0)
    sync_editor = Mock(wraps=window._sync_selected_editor)
    monkeypatch.setattr(window, "_sync_selected_editor", sync_editor)
    window.player.positionChanged.emit(79_500)
    assert table.verticalScrollBar().value() == 0
    sync_editor.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [QMediaPlayer.PlaybackState.PausedState, QMediaPlayer.PlaybackState.StoppedState],
)
def test_paused_and_stopped_seeking_keeps_the_editing_selection(window, monkeypatch, state):
    first = Segment(1, 2, "First", ["A"])
    second = Segment(3, 4, "Second", ["B"])
    window._set_project(
        PackProject(video_duration=5, segments=[first, second]), None, mark_dirty=False
    )
    window.select_segment(first.id)
    monkeypatch.setattr(window.player, "playbackState", lambda: state)
    window.player.positionChanged.emit(3500)
    assert_selected(window, first)
    assert not window.dirty


@pytest.mark.parametrize("activity", ["stopped-seek", "range-edit", "preview"])
def test_playback_does_not_replace_selection_during_other_operations(window, monkeypatch, activity):
    first = Segment(1, 4, "First", ["A"])
    second = Segment(2, 3, "Second", ["B"])
    window._set_project(
        PackProject(video_duration=5, segments=[first, second]), None, mark_dirty=False
    )
    window.select_segment(first.id)
    if activity == "stopped-seek":
        window._stopped_seek_active = True
        window._stopped_seek_target_ms = 2500
    elif activity == "range-edit":
        window._timeline_range_edit_started(first.id, first.start, first.end)
    else:
        window._preview_end = first.end

    start_playback(window, monkeypatch, 2500)
    window.player.positionChanged.emit(2500)
    assert_selected(window, first)
    if activity == "preview":
        assert window._preview_end == first.end
        pause = Mock(side_effect=lambda: monkeypatch.setattr(
            window.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PausedState
        ))
        monkeypatch.setattr(window.player, "pause", pause)
        window.player.positionChanged.emit(4000)
        pause.assert_called_once_with()
        assert window._preview_end is None
        assert_selected(window, first)
    assert not window.dirty


def test_simultaneous_speakers_keep_manual_selection_until_next_segment(window, monkeypatch):
    first = Segment(1, 3, "Together", ["A"])
    simultaneous = Segment(1, 3, "Together", ["B"])
    next_line = Segment(3, 4, "Next", ["C"])
    window._set_project(
        PackProject(video_duration=5, segments=[first, simultaneous, next_line]),
        None, mark_dirty=False,
    )
    window.select_segment(simultaneous.id)
    start_playback(window, monkeypatch, 1500)
    window.player.positionChanged.emit(2000)
    assert_selected(window, simultaneous)
    window.player.positionChanged.emit(3000)
    assert_selected(window, next_line)
    assert not window.dirty


def test_following_preserves_live_edits_to_the_previous_segment(window, qtbot, monkeypatch):
    first = Segment(1, 2, "First", ["A"])
    second = Segment(2, 3, "Second", ["B"])
    window._set_project(
        PackProject(video_duration=5, segments=[first, second]), None, mark_dirty=False
    )
    start_playback(window, monkeypatch, 1000)
    window.speakers_edit.selectAll()
    qtbot.keyClicks(window.speakers_edit, "Alice, Bob")
    window.caption_edit.setPlainText("Edited first line")
    window.player.positionChanged.emit(2000)

    assert_selected(window, second)
    assert first.characters == ["Alice", "Bob"]
    assert first.caption == "Edited first line"
    assert window.segment_table.item(0, 3).text() == "Alice, Bob"
    assert window.segment_table.item(0, 4).text() == "Edited first line"
    assert window.dirty
