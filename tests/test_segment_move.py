from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui.main_window import MainWindow


def project(*ranges: tuple[float, float], duration: float = 60) -> PackProject:
    return PackProject(
        video_duration=duration, auto_speaker_matching=False,
        segments=[
            Segment(start, end, f"Line {index}", ["Alice"])
            for index, (start, end) in enumerate(ranges, start=1)
        ],
    )


def captions(value: PackProject) -> list[str]:
    return [segment.caption for segment in value.segments]


def times(value: PackProject) -> list[tuple[float, float]]:
    return [(segment.start, segment.end) for segment in value.segments]


def test_move_up_places_group_just_before_next_and_keeps_spacing():
    value = project((1, 2), (10, 11), (20, 21), (22, 23.5))
    ids = [segment.id for segment in value.segments[2:]]
    assert value.move_segments(ids, 1)
    assert captions(value) == ["Line 1", "Line 3", "Line 4", "Line 2"]
    assert times(value) == [(1, 2), (6.4, 7.4), (8.4, 9.9), (10, 11)]


def test_move_down_places_group_just_after_previous_and_others_stay():
    value = project((1, 2), (3, 4), (10, 11), (20, 21))
    ids = [value.segments[0].id]
    assert value.move_segments(ids, 3)
    assert captions(value) == ["Line 2", "Line 3", "Line 1", "Line 4"]
    assert times(value) == [(3, 4), (10, 11), (11.1, 12.1), (20, 21)]


def test_move_to_end_and_top_of_list():
    value = project((5, 6), (8, 9), (12, 13))
    assert value.move_segments([value.segments[0].id], 3)
    assert captions(value) == ["Line 2", "Line 3", "Line 1"]
    assert times(value)[-1] == (13.1, 14.1)
    assert value.move_segments([value.segments[2].id], 0)
    assert captions(value) == ["Line 1", "Line 2", "Line 3"]
    assert times(value)[0] == (6.9, 7.9)


def test_scattered_group_is_gathered_and_compressed_to_fit():
    value = project((1, 2), (3, 4), (6, 7), (30, 31), (40, 41))
    ids = [value.segments[0].id, value.segments[4].id]
    assert value.move_segments(ids, 3)
    assert captions(value) == ["Line 2", "Line 3", "Line 1", "Line 5", "Line 4"]
    moved = value.segments[2:4]
    assert value.segments[1].end < moved[0].start < moved[0].end <= moved[1].start
    assert moved[1].end < value.segments[4].start
    assert [segment.end - segment.start for segment in moved] == pytest.approx([1, 1])


def test_no_change_and_no_room():
    value = project((1, 2), (2.1, 3), (3.1, 4))
    before = value.to_dict()
    assert not value.move_segments([value.segments[1].id], 1)
    assert not value.move_segments([value.segments[1].id], 2)
    with pytest.raises(ValueError, match="not enough free time"):
        value.move_segments([value.segments[2].id], 1)
    assert value.to_dict() == before


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    qtbot.addWidget(editor)
    editor._set_project(
        project(*[(index * 10.0, index * 10.0 + 2) for index in range(40)], duration=500),
        None, mark_dirty=False,
    )
    editor.show()
    qtbot.waitUntil(lambda: editor._layout_restored)
    yield editor
    qtbot.waitUntil(lambda: not editor.edit_history.busy, timeout=10000)
    editor.dirty = False
    editor.close()
    qtbot.waitUntil(lambda: not editor.job_manager.active_jobs(), timeout=10000)


def send_mouse(widget, kind, point, buttons):
    button = Qt.MouseButton.LeftButton
    QApplication.sendEvent(widget, QMouseEvent(
        kind, QPointF(point), QPointF(widget.mapToGlobal(point)),
        Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button,
        buttons, Qt.KeyboardModifier.NoModifier,
    ))


def test_drag_selected_rows_down_with_auto_scroll_and_undo(window, qtbot, monkeypatch):
    table = window.segment_table
    table.setFixedHeight(table.horizontalHeader().height() + 6 * table.rowHeight(0))
    qtbot.wait(50)
    before = window.project.to_dict()
    ids = [window.project.segments[1].id, window.project.segments[2].id]
    window._select_segments(ids)
    viewport = table.viewport()
    start = table.visualItemRect(table.item(1, 0)).center()
    held = Qt.MouseButton.LeftButton
    send_mouse(viewport, QEvent.Type.MouseButtonPress, start, held)
    send_mouse(viewport, QEvent.Type.MouseMove, start + QPoint(0, 20), held)
    assert table.moving_rows
    line_y = table.rowViewportPosition(table.drop_row)
    assert viewport.grab().toImage().pixelColor(5, line_y).name() == "#42d6e7"
    bottom = QPoint(start.x(), viewport.height() + 30)
    send_mouse(viewport, QEvent.Type.MouseMove, bottom, held)
    qtbot.waitUntil(lambda: table.verticalScrollBar().value() > 5, timeout=5000)
    # Hold the pointer still while the list keeps scrolling to the end.
    qtbot.waitUntil(
        lambda: table.verticalScrollBar().value() == table.verticalScrollBar().maximum(),
        timeout=10000,
    )
    assert table.drop_row == table.rowCount()
    send_mouse(viewport, QEvent.Type.MouseButtonRelease, bottom, Qt.MouseButton.NoButton)
    assert not table.moving_rows
    assert [segment.id for segment in window.project.segments[-2:]] == ids
    assert times(window.project)[-2:] == [(392.1, 394.1), (402.1, 404.1)]
    assert set(window._selected_table_ids()) == set(ids)
    assert window.dirty
    window.action_undo.trigger()
    qtbot.waitUntil(lambda: not window.edit_history.busy, timeout=10000)
    assert window.project.to_dict() == before


def test_click_without_drag_selects_single_row_and_escape_cancels(window, qtbot, monkeypatch):
    table = window.segment_table
    ids = [window.project.segments[0].id, window.project.segments[1].id]
    window._select_segments(ids)
    viewport = table.viewport()
    point = table.visualItemRect(table.item(1, 0)).center()
    held = Qt.MouseButton.LeftButton
    send_mouse(viewport, QEvent.Type.MouseButtonPress, point, held)
    send_mouse(viewport, QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.NoButton)
    assert window._selected_table_ids() == [ids[1]]

    window._select_segments(ids)
    before = window.project.to_dict()
    send_mouse(viewport, QEvent.Type.MouseButtonPress, point, held)
    send_mouse(viewport, QEvent.Type.MouseMove, point + QPoint(0, 60), held)
    assert table.moving_rows
    qtbot.keyClick(table, Qt.Key.Key_Escape)
    assert not table.moving_rows
    send_mouse(viewport, QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.NoButton)
    assert window.project.to_dict() == before
    assert set(window._selected_table_ids()) == set(ids)


def test_drop_without_room_reports_and_keeps_project(window, qtbot, monkeypatch):
    window.project.segments[1].end = 19.95
    window._refresh_table()
    before = window.project.to_dict()
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda _parent, _title, text: messages.append(text),
    )
    assert not window.move_segments([window.project.segments[5].id], 2)
    assert messages and "not enough free time" in messages[0]
    assert window.project.to_dict() == before
