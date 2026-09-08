from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QImage, QWheelEvent
from PySide6.QtWidgets import QApplication

from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget, segment_lanes


def _point(widget: TimelineWidget, timestamp: float, y: int) -> QPoint:
    return QPoint(round(widget._time_to_x(timestamp)), y)


def _render(widget: TimelineWidget) -> QImage:
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    widget.render(image)
    return image


@pytest.fixture
def pannable_timeline(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 220)
    timeline.set_duration(100)
    segment = Segment(50, 52, "Line", ["Speaker"])
    timeline.set_segments([segment])
    timeline.set_selected(segment.id)
    timeline.set_marks(segment.start, segment.end, segment.id)
    timeline.set_playhead(50)
    timeline.set_zoom(5, anchor_time=50)
    timeline.show()
    return timeline


def _context_event(timeline, point, reason=QContextMenuEvent.Reason.Mouse):
    QApplication.sendEvent(
        timeline, QContextMenuEvent(reason, point, timeline.mapToGlobal(point)),
    )


@pytest.mark.parametrize("surface", ["ruler", "handle", "waveform", "segment", "empty-lane"])
def test_right_drag_pans_without_seeking_selecting_or_editing(pannable_timeline, qtbot, surface):
    timeline = pannable_timeline
    segment = timeline.segments[0]
    y = {
        "ruler": 4, "handle": 30, "waveform": 65,
        "segment": round(timeline._segment_rect(segment).center().y()), "empty-lane": 210,
    }[surface]
    changes = []
    for signal in (
        timeline.seek_requested, timeline.segment_selected, timeline.selection_changed,
        timeline.boundary_changed, timeline.range_edit_started, timeline.range_changed,
        timeline.range_edit_finished, timeline.zoom_changed, timeline.segment_context_menu_requested,
    ):
        signal.connect(lambda *values: changes.append(values))
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=QPoint(500, y))
    assert timeline.is_panning
    for x, expected_offset in ((400, 42), (650, 37)):
        qtbot.mouseMove(timeline, QPoint(x, y))
        assert timeline.offset == pytest.approx(expected_offset)
        assert timeline._time_to_x(50) == pytest.approx(x)
        assert timeline.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=QPoint(700, y))
    _context_event(timeline, QPoint(700, y))

    assert timeline.offset == pytest.approx(36)
    assert timeline.zoom == 5
    assert timeline.playhead == 50
    assert (timeline.mark_in, timeline.mark_out) == (50, 52)
    assert timeline.mark_segment_id == segment.id
    assert timeline.selected_ids == {segment.id}
    assert (segment.start, segment.end) == (50, 52)
    assert changes == []
    assert not timeline.is_panning
    assert timeline.cursor().shape() == Qt.CursorShape.ArrowCursor
    qtbot.mouseMove(timeline, QPoint(800, y))
    assert timeline.offset == pytest.approx(36)


@pytest.mark.parametrize("zoom", [1, 2, 80])
@pytest.mark.parametrize("x", [-100_000, 100_000])
def test_right_drag_clamps_view_and_can_return_from_bounds(pannable_timeline, qtbot, zoom, x):
    timeline = pannable_timeline
    timeline.set_zoom(zoom, anchor_time=50)
    original_offset = timeline.offset
    point = QPoint(500, 65)
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseMove(timeline, QPoint(x, 65))
    expected = timeline.duration - timeline.visible_duration if x < 0 else 0
    assert timeline.offset == pytest.approx(expected)
    qtbot.mouseMove(timeline, point)
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point)
    assert timeline.offset == pytest.approx(original_offset)


def test_right_drag_release_without_move_event_still_pans(pannable_timeline, qtbot):
    timeline = pannable_timeline
    menus = []
    timeline.segment_context_menu_requested.connect(lambda *values: menus.append(values))
    y = round(timeline._segment_rect(timeline.segments[0]).center().y())
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=QPoint(500, y))
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=QPoint(600, y))
    _context_event(timeline, QPoint(600, y))
    assert timeline.offset == pytest.approx(38)
    assert not menus


@pytest.mark.parametrize("early_context", [False, True], ids=["release-menu", "press-menu"])
@pytest.mark.parametrize("jitter", [False, True])
def test_right_click_opens_one_segment_menu_only_on_release(
    pannable_timeline, qtbot, early_context, jitter,
):
    timeline = pannable_timeline
    segment = timeline.segments[0]
    point = timeline._segment_rect(segment).center().toPoint()
    menus = []
    timeline.segment_context_menu_requested.connect(lambda *values: menus.append(values))
    for _ in range(2):
        qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
        if early_context:
            _context_event(timeline, point)
        release = point + QPoint(QApplication.startDragDistance() // 2, 0) if jitter else point
        qtbot.mouseMove(timeline, release)
        assert timeline.offset == 40
        assert not menus
        qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=release)
        assert menus == [(segment.id, timeline.mapToGlobal(release))]
        _context_event(timeline, release)
        assert len(menus) == 1
        menus.clear()


@pytest.mark.parametrize("delta", [QPoint(100, 0), QPoint(0, 100)])
def test_drag_back_to_start_still_suppresses_menu_but_keyboard_menu_works(
    pannable_timeline, qtbot, delta,
):
    timeline = pannable_timeline
    segment = timeline.segments[0]
    point = timeline._segment_rect(segment).center().toPoint()
    menus = []
    timeline.segment_context_menu_requested.connect(lambda *values: menus.append(values))
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    _context_event(timeline, point)
    qtbot.mouseMove(timeline, point + delta)
    qtbot.mouseMove(timeline, point)
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point)
    _context_event(timeline, point)
    assert timeline.offset == 40
    assert not menus
    _context_event(timeline, point, QContextMenuEvent.Reason.Keyboard)
    assert menus == [(segment.id, timeline.mapToGlobal(point))]


def test_escape_restores_pan_and_does_not_open_menu_on_release(pannable_timeline, qtbot):
    timeline = pannable_timeline
    point = timeline._segment_rect(timeline.segments[0]).center().toPoint()
    menus, edits = [], []
    timeline.segment_context_menu_requested.connect(lambda *values: menus.append(values))
    timeline.range_edit_finished.connect(lambda *values: edits.append(values))
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseMove(timeline, point + QPoint(100, 0))
    assert timeline.offset == 38
    qtbot.keyClick(timeline, Qt.Key.Key_Escape)
    assert timeline.offset == 40
    assert not timeline.is_panning
    assert timeline.cursor().shape() == Qt.CursorShape.ArrowCursor
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point)
    _context_event(timeline, point)
    assert not menus
    assert not edits


def test_other_mouse_buttons_do_not_replace_or_finish_pan(pannable_timeline, qtbot):
    timeline = pannable_timeline
    point = QPoint(500, 65)
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseMove(timeline, point + QPoint(100, 0))
    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=point + QPoint(100, 0))
    assert timeline.is_panning
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point + QPoint(200, 0))
    assert timeline.offset == 36
    assert timeline.playhead == 50
    assert (timeline.mark_in, timeline.mark_out) == (50, 52)


def test_wheel_zoom_waits_until_pan_finishes(pannable_timeline, qtbot):
    timeline = pannable_timeline
    point = QPoint(500, 65)

    def wheel():
        QApplication.sendEvent(timeline, QWheelEvent(
            QPointF(point), QPointF(timeline.mapToGlobal(point)), QPoint(), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        ))

    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseMove(timeline, point + QPoint(100, 0))
    wheel()
    assert timeline.zoom == 5
    assert timeline.offset == 38
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point + QPoint(100, 0))
    wheel()
    assert timeline.zoom == 6.25


def test_right_button_does_not_interrupt_left_drag(pannable_timeline, qtbot):
    timeline = pannable_timeline
    menus = []
    timeline.segment_context_menu_requested.connect(lambda *values: menus.append(values))
    point = QPoint(500, 4)
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point)
    qtbot.mousePress(timeline, Qt.MouseButton.RightButton, pos=point)
    qtbot.mouseRelease(timeline, Qt.MouseButton.RightButton, pos=point)
    _context_event(timeline, point)
    assert timeline._drag_kind == "playhead"
    qtbot.mouseMove(timeline, point + QPoint(100, 0))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point + QPoint(100, 0))
    assert timeline.playhead == 52
    assert timeline.offset == 40
    assert not menus


def test_zoomed_waveform_renders_separate_transients_at_their_times(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 176)
    timeline.set_duration(60)
    peaks = [0.0] * 120_000
    peaks[60_000] = 1.0
    peaks[60_020] = 0.75
    timeline.set_waveform(peaks)
    timeline.set_zoom(80, anchor_time=30)
    timeline.set_marks(0, 0)

    visible = timeline._visible_waveform_peaks()
    assert len(visible) == 1000
    assert visible[500] == 1.0
    assert visible[501:513] == [0.0] * 12
    assert visible[513] == 0.75
    image = _render(timeline)
    assert image.pixelColor(500, 50).name() == "#32c6d5"
    assert image.pixelColor(507, 50).name() != "#32c6d5"
    assert image.pixelColor(513, 50).name() == "#32c6d5"


def test_zoomed_waveform_fills_each_peak_time_interval_without_gaps(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 176)
    timeline.set_duration(10)
    timeline.set_waveform([0.5] * 100)
    timeline.set_marks(0, 0)
    timeline.set_zoom(80, anchor_time=5)

    assert timeline._visible_waveform_peaks() == [0.5] * 1000
    image = _render(timeline)
    assert all(image.pixelColor(x, 60).name() == "#32c6d5" for x in range(1000))


def test_waveform_pixel_buckets_preserve_peaks_when_zoomed_out(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(100, 176)
    timeline.set_duration(100)
    peaks = [0.0] * 10_003
    peaks[100] = 0.75
    peaks[-1] = 1.0
    timeline.set_waveform(peaks)

    visible = timeline._visible_waveform_peaks()
    assert visible[0] == 0.75
    assert visible[1] == 0.75
    assert visible[-1] == 1.0
    assert not any(visible[2:-1])
    timeline.set_zoom(80, anchor_time=100)
    assert timeline._visible_waveform_peaks()[-1] == 1.0


def test_waveform_cache_tracks_view_changes_but_not_playhead_or_height(qtbot):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 176)
    timeline.set_duration(10)
    timeline.set_waveform([0.25] * 10_000)
    cached = timeline._visible_waveform_peaks()
    timeline.set_playhead(5)
    timeline.resize(1000, 300)
    assert timeline._visible_waveform_peaks() is cached

    timeline.set_zoom(2, anchor_time=5)
    zoomed = timeline._visible_waveform_peaks()
    assert zoomed is not cached
    timeline.ensure_visible(9)
    panned = timeline._visible_waveform_peaks()
    assert panned is not zoomed
    timeline.resize(500, 300)
    resized = timeline._visible_waveform_peaks()
    assert resized is not panned
    assert len(resized) == 500
    timeline.set_duration(20)
    assert timeline._visible_waveform_peaks() is not resized
    timeline.set_waveform([0.75] * 10_000)
    assert timeline._visible_waveform_peaks() == [0.75] * 500
    timeline.set_waveform([])
    assert timeline._visible_waveform_peaks() == []
    assert timeline._waveform_cache == []


@pytest.mark.parametrize(("duration", "precision"), [(60, 1), (6, 2), (0.6, 3)])
def test_zoomed_ruler_labels_distinguish_subsecond_ticks(qtbot, duration, precision):
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    timeline.resize(1000, 176)
    timeline.set_duration(duration)
    timeline.set_zoom(80, anchor_time=0)
    labels = []

    class Painter:
        def fillRect(self, *_args):
            pass

        def setFont(self, *_args):
            pass

        def setPen(self, *_args):
            pass

        def drawLine(self, *_args):
            pass

        def drawText(self, _point, text):
            labels.append(text)

    timeline._paint_ruler(Painter())
    assert len(labels) >= 5
    assert len(labels) == len(set(labels))
    assert all(len(label.split(".")[1]) == precision for label in labels)


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