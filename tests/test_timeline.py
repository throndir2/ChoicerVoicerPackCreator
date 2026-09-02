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