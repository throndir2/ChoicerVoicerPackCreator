from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget


def _point(widget: TimelineWidget, timestamp: float, y: int) -> QPoint:
    return QPoint(round(widget._time_to_x(timestamp)), y)


def test_waveform_handles_resize_selected_segment(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(segment.start, segment.end, segment.id)
    timeline.show()

    finished: list[tuple[object, ...]] = []
    timeline.range_edit_finished.connect(lambda *values: finished.append(values))
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 2, 65))
    qtbot.mouseMove(timeline, _point(timeline, 1.25, 65))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 1.25, 65))

    assert segment.start == pytest.approx(1.25)
    assert segment.end == pytest.approx(4.0)
    assert timeline.mark_in == pytest.approx(1.25)
    assert finished == [(segment.id, 2.0, 4.0, 1.25, 4.0)]


def test_segment_body_drag_moves_range_without_changing_duration(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(segment.start, segment.end, segment.id)
    timeline.show()

    center_y = round(timeline._segment_rect(segment).center().y())
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 3, center_y))
    qtbot.mouseMove(timeline, _point(timeline, 4.5, center_y))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 4.5, center_y))

    assert segment.start == pytest.approx(3.5)
    assert segment.end == pytest.approx(5.5)
    assert segment.duration == pytest.approx(2.0)


def test_dragging_empty_waveform_creates_new_range(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(1, 2, "Existing", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(1, 2, segment.id)
    timeline.show()

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 6, 65))
    qtbot.mouseMove(timeline, _point(timeline, 8, 65))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 8, 65))

    assert timeline.mark_in == pytest.approx(6.0)
    assert timeline.mark_out == pytest.approx(8.0)
    assert timeline.mark_segment_id == ""
    assert (segment.start, segment.end) == (1, 2)


def test_escape_cancels_new_waveform_range_and_restores_owned_marks(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(1, 2, "Existing", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(1, 2, segment.id)
    timeline.show()

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 6, 65))
    qtbot.mouseMove(timeline, _point(timeline, 8, 65))
    qtbot.keyPress(timeline, Qt.Key.Key_Escape)
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 8, 65))

    assert timeline.mark_in == pytest.approx(1.0)
    assert timeline.mark_out == pytest.approx(2.0)
    assert timeline.mark_segment_id == segment.id
    assert (segment.start, segment.end) == (1, 2)


def test_seek_click_does_not_detach_selected_segment_marks(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(1, 2, "Existing", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(1, 2, segment.id)
    timeline.show()

    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 6, 65))

    assert timeline.mark_segment_id == segment.id
    assert (timeline.mark_in, timeline.mark_out) == (1, 2)


def test_segment_click_selects_then_seeks_to_precise_clicked_point(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Existing", ["Speaker"])
    timeline.set_segments([segment])
    timeline.show()
    selected: list[str] = []
    seeks: list[float] = []
    timeline.segment_selected.connect(selected.append)
    timeline.seek_requested.connect(seeks.append)
    center_y = round(timeline._segment_rect(segment).center().y())

    qtbot.mouseClick(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=_point(timeline, 3.25, center_y),
    )

    assert selected == [segment.id]
    assert seeks == [pytest.approx(3.25, abs=0.01)]


def test_narrow_segment_keeps_clickable_and_draggable_center(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(120)
    segment = Segment(20, 21, "Short", ["Speaker"])
    timeline.set_segments([segment])
    timeline.show()
    rect = timeline._segment_rect(segment)
    assert 3 < rect.width() < 14
    center_y = round(rect.center().y())
    selected: list[str] = []
    seeks: list[float] = []
    timeline.segment_selected.connect(selected.append)
    timeline.seek_requested.connect(seeks.append)

    qtbot.mouseClick(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=_point(timeline, 20.5, center_y),
    )
    assert selected == [segment.id]
    assert seeks == [pytest.approx(20.5, abs=0.07)]

    qtbot.mousePress(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=_point(timeline, 20.5, center_y),
    )
    qtbot.mouseMove(timeline, _point(timeline, 22.5, center_y))
    qtbot.mouseRelease(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=_point(timeline, 22.5, center_y),
    )
    assert segment.start == pytest.approx(22.0, abs=0.07)
    assert segment.end == pytest.approx(23.0, abs=0.07)


def test_minimum_width_segment_center_is_clickable_and_blank_lane_seeks(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(1000)
    segment = Segment(200, 200.1, "Tiny", ["Speaker"])
    timeline.set_segments([segment])
    timeline.show()
    rect = timeline._segment_rect(segment)
    assert rect.width() == 3.0
    center_y = round(rect.center().y())
    selected: list[str] = []
    seeks: list[float] = []
    timeline.segment_selected.connect(selected.append)
    timeline.seek_requested.connect(seeks.append)

    qtbot.mouseClick(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=QPoint(round(rect.center().x()), center_y),
    )
    assert selected == [segment.id]

    selected.clear()
    seeks.clear()
    blank_x = round(rect.right() + 50)
    qtbot.mouseClick(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=QPoint(blank_x, center_y),
    )
    assert selected == []
    assert seeks == [pytest.approx(timeline._x_to_time(blank_x), abs=0.01)]
    assert (segment.start, segment.end) == (200, 200.1)