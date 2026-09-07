from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget, segment_lanes


def _point(widget: TimelineWidget, timestamp: float, y: int) -> QPoint:
    return QPoint(round(widget._time_to_x(timestamp)), y)


def test_segment_release_applies_final_pointer_after_last_move(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.show()
    y = round(timeline._segment_rect(segment).center().y())
    finished = []
    timeline.range_edit_finished.connect(lambda *values: finished.append(values))
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 3, y))
    qtbot.mouseMove(timeline, _point(timeline, 4, y))
    assert (segment.start, segment.end) == (3, 5)
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 5, y))
    assert (segment.start, segment.end) == (4, 6)
    assert finished == [(segment.id, 2, 4, 4, 6)]


def test_lane_layout_reuses_lowest_available_lane_and_accepts_prepared_layout(qtbot):
    segments = [
        Segment(0, 5), Segment(1, 2), Segment(2, 3),
        Segment(5 - 0.0009, 6), Segment(5, 7),
    ]
    lanes = segment_lanes(list(reversed(segments)))
    assert [lanes[segment.id] for segment in segments] == [0, 1, 1, 0, 1]
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.set_segments(segments, lanes=lanes)
    assert timeline._segment_lanes is lanes
    assert timeline.minimumHeight() == 188


def test_paint_skips_offscreen_geometry_but_keeps_minimum_width_edge(qtbot, monkeypatch):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(100)
    timeline.set_playhead(50)
    timeline.set_zoom(5, anchor_time=50)
    segments = [Segment(1, 2), Segment(39.98, 39.99), Segment(41, 43), Segment(80, 90)]
    timeline.set_segments(segments)
    measured = []
    original = timeline._segment_rect

    def rectangle(segment):
        measured.append(segment.id)
        return original(segment)

    monkeypatch.setattr(timeline, "_segment_rect", rectangle)
    timeline.grab()
    assert measured == [segments[1].id, segments[2].id]


@pytest.mark.parametrize("y", [4, 65, 210])
def test_playhead_drag_seeks_continuously_without_editing_ranges(qtbot, y: int) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(segment.start, segment.end, segment.id)
    timeline.set_playhead(3)
    timeline.show()
    seeks: list[float] = []
    edits: list[tuple[object, ...]] = []
    timeline.seek_requested.connect(seeks.append)
    for signal in (
        timeline.segment_selected,
        timeline.boundary_changed,
        timeline.range_edit_started,
        timeline.range_changed,
        timeline.range_edit_finished,
    ):
        signal.connect(lambda *values: edits.append(values))

    qtbot.mouseMove(timeline, _point(timeline, 3, y))
    assert timeline.cursor().shape() == Qt.CursorShape.OpenHandCursor
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 3, y))
    assert timeline.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    for timestamp in (4.5, 6.25, 1.5):
        qtbot.mouseMove(timeline, _point(timeline, timestamp, y))
        assert seeks[-1] == pytest.approx(timestamp)
        assert timeline.playhead == pytest.approx(timestamp)
        timeline.set_playhead(3.1)
        assert timeline.playhead == pytest.approx(timestamp)
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 5.5, y))

    assert seeks[-1] == pytest.approx(5.5)
    assert timeline.playhead == pytest.approx(5.5)
    assert timeline.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert edits == []
    assert (segment.start, segment.end) == (2, 4)
    assert (timeline.mark_in, timeline.mark_out) == (2, 4)
    assert timeline.mark_segment_id == segment.id
    assert timeline.selected_id == segment.id
    timeline.set_playhead(5.75)
    assert timeline.playhead == pytest.approx(5.75)


@pytest.mark.parametrize("timestamp", [2, 4])
@pytest.mark.parametrize("y", [4, 65])
def test_playhead_can_be_dragged_when_aligned_with_range_edge(
    qtbot, timestamp: float, y: int
) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    timeline.set_marks(2, 4)
    timeline.set_playhead(timestamp)
    timeline.show()

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, timestamp, y))
    qtbot.mouseMove(timeline, _point(timeline, 7, y))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 7, y))

    assert timeline.playhead == pytest.approx(7)
    assert (timeline.mark_in, timeline.mark_out) == (2, 4)


@pytest.mark.parametrize("timestamp", [2, 4])
def test_colored_handle_remains_editable_when_aligned_with_playhead(
    qtbot, timestamp: float
) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    timeline.set_marks(2, 4)
    timeline.set_playhead(timestamp)
    timeline.show()
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, timestamp, 30))
    qtbot.mouseMove(timeline, _point(timeline, timestamp + 0.5, 30))
    qtbot.mouseRelease(
        timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, timestamp + 0.5, 30)
    )

    assert seeks == []
    assert timeline.playhead == pytest.approx(timestamp)
    assert (timeline.mark_in, timeline.mark_out) == (
        (2.5, 4) if timestamp == 2 else (2, 4.5)
    )


def test_playhead_drag_uses_zoomed_coordinates_without_shifting_view(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(100)
    timeline.set_playhead(50)
    timeline.set_zoom(5, anchor_time=50)
    timeline.show()
    assert timeline.offset == pytest.approx(40)

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 50, 4))
    qtbot.mouseMove(timeline, _point(timeline, 59.5, 4))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 59.5, 4))

    assert timeline.playhead == pytest.approx(59.5)
    assert timeline.offset == pytest.approx(40)


@pytest.mark.parametrize(("x", "expected"), [(-100, 0), (1100, 10)])
def test_playhead_drag_clamps_to_media_bounds(qtbot, x: int, expected: float) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    timeline.set_playhead(5)
    timeline.show()
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 5, 4))
    qtbot.mouseMove(timeline, QPoint(x, 4))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=QPoint(x, 4))

    assert seeks[-1] == pytest.approx(expected)
    assert timeline.playhead == pytest.approx(expected)
    assert timeline._drag_kind == ""


def test_escape_cancels_playhead_drag_without_editing_marks(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    timeline.set_marks(1, 2)
    timeline.set_playhead(5)
    timeline.show()
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 5, 65))
    qtbot.mouseMove(timeline, _point(timeline, 8, 65))
    qtbot.keyPress(timeline, Qt.Key.Key_Escape)
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 8, 65))
    qtbot.mouseMove(timeline, _point(timeline, 9, 65))

    assert seeks[-1] == pytest.approx(5)
    assert timeline.playhead == pytest.approx(5)
    assert (timeline.mark_in, timeline.mark_out) == (1, 2)
    assert timeline._drag_kind == ""


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


@pytest.mark.parametrize("lane_count", [1, 5])
def test_taller_timeline_expands_waveform_and_keeps_segment_lanes_visible(qtbot, lane_count):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.set_duration(10)
    segments = [Segment(2, 4, f"Line {index}", ["Speaker"]) for index in range(lane_count)]
    timeline.set_segments(segments)
    timeline.set_marks(2, 4, segments[0].id)
    timeline.set_selected(segments[0].id)
    timeline.set_waveform([1.0] * 100)
    timeline.resize(1000, timeline.minimumHeight())
    timeline.show()
    original_bottom = timeline._waveform_bottom()
    original_rects = [timeline._segment_rect(segment) for segment in segments]

    timeline.resize(1000, timeline.minimumHeight() + 200)
    assert timeline._waveform_bottom() == original_bottom + 200
    for segment, original_rect in zip(segments, original_rects, strict=True):
        rect = timeline._segment_rect(segment)
        assert rect.top() == original_rect.top() + 200
        assert rect.height() == original_rect.height()
        assert rect.bottom() < timeline.height()

    image = timeline.grab().toImage()
    waveform_y = round(timeline._waveform_bottom() - 30)
    assert image.pixelColor(800, waveform_y).name() == "#32c6d5"
    assert image.pixelColor(200, waveform_y).name() == "#48dbe7"
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 2, waveform_y))
    qtbot.mouseMove(timeline, _point(timeline, 1.25, waveform_y))
    qtbot.mouseRelease(
        timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 1.25, waveform_y),
    )
    assert segments[0].start == pytest.approx(1.25)
    assert timeline.mark_in == pytest.approx(1.25)
    assert all(segment.start == 2 for segment in segments[1:])


def test_segment_body_drag_moves_range_without_changing_duration(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2, 4, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(segment.start, segment.end, segment.id)
    timeline.set_playhead(3)
    timeline.show()
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)

    center_y = round(timeline._segment_rect(segment).center().y())
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 3, center_y))
    qtbot.mouseMove(timeline, _point(timeline, 4.5, center_y))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 4.5, center_y))

    assert segment.start == pytest.approx(3.5)
    assert segment.end == pytest.approx(5.5)
    assert segment.duration == pytest.approx(2.0)
    assert seeks == []


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
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)

    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=_point(timeline, 6, 65))

    assert seeks == [pytest.approx(6)]
    assert timeline.mark_segment_id == segment.id
    assert (timeline.mark_in, timeline.mark_out) == (1, 2)


@pytest.mark.parametrize("already_selected", [False, True])
def test_segment_click_selects_then_seeks_to_start(qtbot, already_selected: bool) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segment = Segment(2.125, 4, "Existing", ["Speaker"])
    timeline.set_segments([segment])
    if already_selected:
        timeline.set_selected(segment.id)
    timeline.set_playhead(3)
    timeline.show()
    selected: list[str] = []
    seeks: list[float] = []
    timeline.segment_selected.connect(selected.append)
    timeline.seek_requested.connect(seeks.append)
    edits: list[tuple[object, ...]] = []
    timeline.range_edit_started.connect(lambda *values: edits.append(values))
    timeline.range_edit_finished.connect(lambda *values: edits.append(values))
    center_y = round(timeline._segment_rect(segment).center().y())

    qtbot.mouseClick(
        timeline,
        Qt.MouseButton.LeftButton,
        pos=_point(timeline, 3.25, center_y),
    )

    assert selected == [segment.id]
    assert seeks == [segment.start]
    assert edits == []
    assert (segment.start, segment.end) == (2.125, 4)


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
    assert seeks == [segment.start]

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
    assert seeks == [segment.start]

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


@pytest.mark.parametrize("modifier", [
    Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier,
])
@pytest.mark.parametrize("edge", ["start", "center", "end"])
def test_modifier_click_and_motion_select_without_dragging_or_seeking(qtbot, modifier, edge):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    first, second = Segment(1, 2), Segment(3, 4)
    timeline.set_segments([first, second])
    timeline.set_selected(first.id)
    timeline.show()
    rect = timeline._segment_rect(second)
    x = {"start": rect.left() + 1, "center": rect.center().x(), "end": rect.right() - 1}[edge]
    point = QPoint(round(x), round(rect.center().y()))
    selections, seeks, edits = [], [], []
    timeline.selection_changed.connect(selections.append)
    timeline.seek_requested.connect(seeks.append)
    timeline.range_edit_started.connect(lambda *args: edits.append(args))
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, modifier, pos=point)
    qtbot.mouseMove(timeline, point + QPoint(50, 0))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, modifier, pos=point + QPoint(50, 0))
    assert selections == [[first.id, second.id]]
    assert timeline.selected_ids == {first.id, second.id}
    assert timeline.selected_id == ""
    assert not timeline._drag_kind
    assert not seeks
    assert not edits
    assert (second.start, second.end) == (3, 4)


def test_all_selected_blocks_are_highlighted_and_removed_ids_are_cleared(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(10)
    segments = [Segment(1, 2), Segment(3, 4), Segment(5, 6)]
    timeline.set_segments(segments)
    timeline.show()
    points = [
        timeline._segment_rect(segment).bottomRight().toPoint() - QPoint(10, 5)
        for segment in segments
    ]
    unselected = timeline.grab().toImage()
    timeline.set_selection([segments[0].id, segments[2].id])
    selected = timeline.grab().toImage()
    assert selected.pixelColor(points[0]) != unselected.pixelColor(points[0])
    assert selected.pixelColor(points[1]) == unselected.pixelColor(points[1])
    assert selected.pixelColor(points[2]) != unselected.pixelColor(points[2])
    timeline.set_segments([segments[1]])
    assert timeline.selected_ids == set()
    assert timeline.selected_id == ""