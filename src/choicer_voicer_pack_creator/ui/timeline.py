from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.ui.theme import SEGMENT_COLORS


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    segment_selected = Signal(str)
    boundary_changed = Signal(str, float, float)
    range_edit_started = Signal(str, float, float)
    range_changed = Signal(str, float, float)
    range_edit_finished = Signal(str, float, float, float, float)
    zoom_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(176)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.duration = 1.0
        self.playhead = 0.0
        self.peaks: list[float] = []
        self.segments: list[Segment] = []
        self.selected_id = ""
        self.zoom = 1.0
        self.offset = 0.0
        self.mark_in = 0.0
        self.mark_out = 3.0
        self.mark_segment_id = ""
        self._drag_id = ""
        self._drag_kind = ""
        self._drag_active = False
        self._drag_press_x = 0.0
        self._drag_anchor_time = 0.0
        self._drag_base_start = 0.0
        self._drag_base_end = 0.0
        self._drag_original_start = 0.0
        self._drag_original_end = 0.0
        self._drag_previous_mark_start = 0.0
        self._drag_previous_mark_end = 0.0
        self._drag_previous_mark_segment_id = ""
        self._segment_lanes: dict[str, int] = {}

    @property
    def visible_duration(self) -> float:
        return self.duration / max(1.0, self.zoom)

    def set_duration(self, duration: float) -> None:
        self.duration = max(0.1, duration)
        self.mark_out = min(self.duration, max(self.mark_in + 0.05, self.mark_out))
        self._clamp_offset()
        self.update()

    def set_waveform(self, peaks: list[float]) -> None:
        self.peaks = list(peaks)
        self.update()

    def set_segments(self, segments: list[Segment]) -> None:
        self.segments = segments
        self._layout_segment_lanes()
        self.update()

    def _layout_segment_lanes(self) -> None:
        lane_ends: list[float] = []
        self._segment_lanes = {}
        for segment in sorted(self.segments, key=lambda item: (item.start, item.end)):
            lane = next(
                (
                    index
                    for index, lane_end in enumerate(lane_ends)
                    if segment.start >= lane_end - 0.001
                ),
                len(lane_ends),
            )
            if lane == len(lane_ends):
                lane_ends.append(segment.end)
            else:
                lane_ends[lane] = segment.end
            self._segment_lanes[segment.id] = lane
        visible_lanes = max(1, min(5, len(lane_ends)))
        self.setMinimumHeight(max(176, 126 + visible_lanes * 31))

    def set_selected(self, segment_id: str) -> None:
        changed = self.selected_id != segment_id
        self.selected_id = segment_id
        segment = next((item for item in self.segments if item.id == segment_id), None)
        if segment and changed:
            self.ensure_visible(segment.start)
        self.update()

    def set_playhead(self, seconds: float) -> None:
        if self._drag_kind == "playhead":
            return
        self.playhead = max(0.0, min(self.duration, seconds))
        if not self._drag_kind:
            self.ensure_visible(self.playhead, margin=0.05)
        self.update()

    def set_marks(self, mark_in: float, mark_out: float, segment_id: str = "") -> None:
        self.mark_in = max(0.0, min(self.duration, mark_in))
        self.mark_out = max(self.mark_in, min(self.duration, mark_out))
        self.mark_segment_id = segment_id if self._segment(segment_id) else ""
        self.update()

    def set_zoom(self, zoom: float, anchor_time: float | None = None) -> None:
        old_visible = self.visible_duration
        old_anchor = anchor_time if anchor_time is not None else self.offset + old_visible / 2.0
        fraction = (old_anchor - self.offset) / old_visible if old_visible else 0.5
        self.zoom = max(1.0, min(80.0, zoom))
        self.offset = old_anchor - fraction * self.visible_duration
        self._clamp_offset()
        self.zoom_changed.emit(self.zoom)
        self.update()

    def ensure_visible(self, timestamp: float, margin: float = 0.12) -> None:
        visible = self.visible_duration
        edge = visible * margin
        if timestamp < self.offset + edge:
            self.offset = timestamp - edge
        elif timestamp > self.offset + visible - edge:
            self.offset = timestamp - visible + edge
        self._clamp_offset()

    def _clamp_offset(self) -> None:
        self.offset = max(0.0, min(max(0.0, self.duration - self.visible_duration), self.offset))

    def _time_to_x(self, timestamp: float) -> float:
        return (timestamp - self.offset) / self.visible_duration * max(1, self.width())

    def _x_to_time(self, x: float) -> float:
        value = self.offset + x / max(1, self.width()) * self.visible_duration
        return max(0.0, min(self.duration, value))

    def _waveform_bottom(self) -> float:
        return 108.0 + max(0, self.height() - self.minimumHeight())

    def _segment_rect(self, segment: Segment) -> QRectF:
        top = self._waveform_bottom() + 6
        lane = min(4, self._segment_lanes.get(segment.id, 0))
        return QRectF(
            self._time_to_x(segment.start),
            top + lane * 30,
            max(3.0, self._time_to_x(segment.end) - self._time_to_x(segment.start)),
            27.0,
        )

    def paintEvent(self, event: object) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#09111a"))
        self._paint_ruler(painter)
        self._paint_waveform(painter)
        self._paint_marks(painter)
        self._paint_segments(painter)
        self._paint_playhead(painter)

    def _paint_ruler(self, painter: QPainter) -> None:
        painter.fillRect(0, 0, self.width(), 25, QColor("#0d1824"))
        visible = self.visible_duration
        raw_step = visible / max(3, self.width() // 100)
        magnitude = 10 ** math.floor(math.log10(max(raw_step, 0.001)))
        step = next(item * magnitude for item in (1, 2, 5, 10) if item * magnitude >= raw_step)
        first = math.floor(self.offset / step) * step
        painter.setFont(QFont("Segoe UI", 8))
        timestamp = first
        while timestamp <= self.offset + visible + step:
            x = self._time_to_x(timestamp)
            if 0 <= x <= self.width():
                painter.setPen(QPen(QColor("#35485e"), 1))
                painter.drawLine(QPointF(x, 15), QPointF(x, self.height()))
                painter.setPen(QColor("#8da0b7"))
                minutes = int(timestamp // 60)
                seconds = timestamp - minutes * 60
                painter.drawText(QPointF(x + 3, 12), f"{minutes}:{seconds:04.1f}")
            timestamp += step

    def _paint_waveform(self, painter: QPainter) -> None:
        top, bottom = 29.0, self._waveform_bottom()
        center = (top + bottom) / 2.0
        painter.setPen(QColor("#1f3144"))
        painter.drawLine(QPointF(0, center), QPointF(self.width(), center))
        if not self.peaks:
            painter.setPen(QColor("#63778f"))
            painter.drawText(QRectF(0, top, self.width(), bottom - top), Qt.AlignmentFlag.AlignCenter, "Waveform loading…")
            return
        painter.setPen(QPen(QColor("#32c6d5"), 1))
        count = len(self.peaks)
        first = max(0, int(self.offset / self.duration * count))
        last = min(count, int((self.offset + self.visible_duration) / self.duration * count) + 1)
        if last <= first:
            return
        pixels = max(1, self.width())
        stride = max(1, (last - first) // pixels)
        for index in range(first, last, stride):
            timestamp = index / count * self.duration
            x = self._time_to_x(timestamp)
            peak = max(self.peaks[index : min(last, index + stride)])
            height = peak * (bottom - top) * 0.47
            painter.drawLine(QPointF(x, center - height), QPointF(x, center + height))

    def _paint_marks(self, painter: QPainter) -> None:
        x1, x2 = self._time_to_x(self.mark_in), self._time_to_x(self.mark_out)
        bottom = self._waveform_bottom() + 1
        painter.fillRect(QRectF(x1, 25, x2 - x1, bottom - 25), QColor(40, 190, 210, 25))
        painter.setPen(QPen(QColor("#48dbe7"), 2))
        painter.drawLine(QPointF(x1, 25), QPointF(x1, bottom))
        painter.setPen(QPen(QColor("#ffb454"), 2))
        painter.drawLine(QPointF(x2, 25), QPointF(x2, bottom))
        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        painter.fillRect(QRectF(x1 - 3, 25, 7, 12), QColor("#48dbe7"))
        painter.fillRect(QRectF(x2 - 3, 25, 7, 12), QColor("#ffb454"))
        painter.setPen(QColor("#bceff4"))
        painter.drawText(QPointF(x1 + 5, 36), "IN")
        painter.setPen(QColor("#ffd6a0"))
        painter.drawText(QPointF(x2 + 5, 36), "OUT")

    def _paint_segments(self, painter: QPainter) -> None:
        for index, segment in enumerate(self.segments):
            rect = self._segment_rect(segment)
            if rect.right() < 0 or rect.left() > self.width():
                continue
            color = QColor(SEGMENT_COLORS[index % len(SEGMENT_COLORS)])
            alpha = 115 if segment.id == self.selected_id else 58
            painter.fillRect(rect, QColor(color.red(), color.green(), color.blue(), alpha))
            painter.setPen(QPen(color if segment.id == self.selected_id else color.darker(120), 2 if segment.id == self.selected_id else 1))
            painter.drawRect(rect)
            painter.setPen(color.lighter(130))
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            label = f"{index + 1}  {segment.primary_character}  ·  {segment.caption}"
            painter.drawText(rect.adjusted(5, 2, -5, -2), Qt.AlignmentFlag.AlignVCenter, label)

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self._time_to_x(self.playhead)
        if 0 <= x <= self.width():
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            painter.setBrush(QColor("#ffffff"))
            painter.drawPolygon([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 8)])

    def _playhead_hit(self, position: QPointF) -> bool:
        x = self._time_to_x(self.playhead)
        if not (0 <= x <= self.width() and abs(position.x() - x) <= 7):
            return False
        # Keep colored handles and segment blocks editable where they cross the playhead.
        if 25 <= position.y() <= 37 and any(
            abs(position.x() - self._time_to_x(mark)) <= 8
            for mark in (self.mark_in, self.mark_out)
        ):
            return False
        return not any(self._segment_rect(segment).contains(position) for segment in self.segments)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        x = event.position().x()
        if self._playhead_hit(event.position()):
            self._prepare_drag("playhead", "", self.playhead, self.playhead, x)
            self._activate_drag()
            self._update_drag(x)
            event.accept()
            return
        for segment in reversed(self.segments):
            rect = self._segment_rect(segment)
            if rect.contains(event.position()):
                edge_width = min(7.0, rect.width() / 3.0) if rect.width() >= 6.0 else 0.0
                if x <= rect.left() + edge_width:
                    self._prepare_drag("segment-start", segment.id, segment.start, segment.end, x)
                    self.selected_id = segment.id
                    self.segment_selected.emit(segment.id)
                    self._activate_drag()
                    return
                if x >= rect.right() - edge_width:
                    self._prepare_drag("segment-end", segment.id, segment.start, segment.end, x)
                    self.selected_id = segment.id
                    self.segment_selected.emit(segment.id)
                    self._activate_drag()
                    return
                self._prepare_drag("segment-body", segment.id, segment.start, segment.end, x)
                self.selected_id = segment.id
                self.segment_selected.emit(segment.id)
                return

        if 25 <= event.position().y() <= self._waveform_bottom() + 1:
            x1 = self._time_to_x(self.mark_in)
            x2 = self._time_to_x(self.mark_out)
            segment_id = self.mark_segment_id if self._segment(self.mark_segment_id) else ""
            if abs(x - x1) <= 8:
                self._prepare_drag("mark-start", segment_id, self.mark_in, self.mark_out, x)
                self._activate_drag()
                return
            if abs(x - x2) <= 8:
                self._prepare_drag("mark-end", segment_id, self.mark_in, self.mark_out, x)
                self._activate_drag()
                return
            if min(x1, x2) < x < max(x1, x2):
                self._prepare_drag("mark-body", segment_id, self.mark_in, self.mark_out, x)
                return
            self._prepare_drag("mark-new", "", self.mark_in, self.mark_out, x)
            return
        self.seek_requested.emit(self._x_to_time(x))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_kind:
            if not self._drag_active:
                distance = abs(event.position().x() - self._drag_press_x)
                if distance < QApplication.startDragDistance():
                    return
                self._activate_drag()
            self._update_drag(event.position().x())
            return

        x = event.position().x()
        if self._playhead_hit(event.position()):
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            QToolTip.hideText()
            return
        if 25 <= event.position().y() <= self._waveform_bottom() + 1:
            x1 = self._time_to_x(self.mark_in)
            x2 = self._time_to_x(self.mark_out)
            if abs(x - x1) <= 8 or abs(x - x2) <= 8:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif min(x1, x2) < x < max(x1, x2):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            return
        for segment in reversed(self.segments):
            rect = self._segment_rect(segment)
            if rect.contains(event.position()):
                edge_width = min(7.0, rect.width() / 3.0) if rect.width() >= 6.0 else 0.0
                if (
                    event.position().x() <= rect.left() + edge_width
                    or event.position().x() >= rect.right() - edge_width
                ):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"{segment.primary_character}\n{segment.start:.3f}–{segment.end:.3f}s\n"
                    f"Click to cue the start; drag the center to move; drag an edge to trim.\n"
                    f"{segment.caption}",
                    self,
                )
                return
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._drag_kind:
            return
        if self._drag_kind == "playhead":
            self._update_drag(event.position().x())
        elif self._drag_active:
            final_start, final_end = self._current_drag_range()
            self.range_edit_finished.emit(
                self._drag_id,
                self._drag_original_start,
                self._drag_original_end,
                final_start,
                final_end,
            )
        elif self._drag_kind == "segment-body":
            self.seek_requested.emit(self._drag_original_start)
        else:
            self.seek_requested.emit(self._drag_anchor_time)
        self._clear_drag()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._drag_kind:
            if self._drag_kind == "playhead":
                self._seek_playhead(self._drag_original_start)
            elif self._drag_active:
                if self._drag_kind == "mark-new":
                    self._apply_drag_range(
                        self._drag_previous_mark_start, self._drag_previous_mark_end
                    )
                    self.mark_segment_id = self._drag_previous_mark_segment_id
                else:
                    self._apply_drag_range(
                        self._drag_original_start, self._drag_original_end
                    )
                self.range_edit_finished.emit(
                    self._drag_id,
                    self._drag_original_start,
                    self._drag_original_end,
                    *self._current_drag_range(),
                )
            self._clear_drag()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def _segment(self, segment_id: str) -> Segment | None:
        return next((item for item in self.segments if item.id == segment_id), None)

    def _prepare_drag(
        self,
        kind: str,
        segment_id: str,
        start: float,
        end: float,
        press_x: float,
    ) -> None:
        self._drag_previous_mark_start = self.mark_in
        self._drag_previous_mark_end = self.mark_out
        self._drag_previous_mark_segment_id = self.mark_segment_id
        self._drag_kind = kind
        self._drag_id = segment_id
        self._drag_active = False
        self._drag_press_x = press_x
        self._drag_anchor_time = self._x_to_time(press_x)
        self._drag_base_start = start
        self._drag_base_end = end
        segment = self._segment(segment_id)
        self._drag_original_start = segment.start if segment else start
        self._drag_original_end = segment.end if segment else end

    def _activate_drag(self) -> None:
        if self._drag_active:
            return
        self._drag_active = True
        if self._drag_kind == "playhead":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            QToolTip.hideText()
            return
        if self._drag_kind == "mark-new":
            self.mark_segment_id = ""
        self.range_edit_started.emit(
            self._drag_id, self._drag_original_start, self._drag_original_end
        )
        self.setCursor(
            Qt.CursorShape.SizeAllCursor
            if self._drag_kind in {"segment-body", "mark-body"}
            else Qt.CursorShape.SizeHorCursor
        )

    def _update_drag(self, x: float) -> None:
        timestamp = self._x_to_time(x)
        if self._drag_kind == "playhead":
            self._seek_playhead(timestamp)
            return
        minimum = 0.05
        start, end = self._drag_base_start, self._drag_base_end
        if self._drag_kind in {"segment-start", "mark-start"}:
            start = min(timestamp, end - minimum)
        elif self._drag_kind in {"segment-end", "mark-end"}:
            end = max(timestamp, start + minimum)
        elif self._drag_kind in {"segment-body", "mark-body"}:
            duration = end - start
            shifted_start = start + timestamp - self._drag_anchor_time
            start = max(0.0, min(self.duration - duration, shifted_start))
            end = start + duration
        elif self._drag_kind == "mark-new":
            anchor = self._drag_anchor_time
            if timestamp >= anchor:
                start, end = anchor, max(timestamp, anchor + minimum)
            else:
                start, end = min(timestamp, anchor - minimum), anchor
        start = max(0.0, min(self.duration, round(start, 3)))
        end = max(0.0, min(self.duration, round(end, 3)))
        if end - start < minimum:
            if start + minimum <= self.duration:
                end = round(start + minimum, 3)
            else:
                start = round(max(0.0, end - minimum), 3)
        self._apply_drag_range(start, end)

    def _seek_playhead(self, seconds: float) -> None:
        self.playhead = max(0.0, min(self.duration, seconds))
        self.update()
        self.seek_requested.emit(self.playhead)

    def _apply_drag_range(self, start: float, end: float) -> None:
        segment = self._segment(self._drag_id)
        if segment:
            segment.start, segment.end = start, end
            if segment.id == self.selected_id:
                self.mark_in, self.mark_out = start, end
                self.mark_segment_id = segment.id
            self._layout_segment_lanes()
            self.boundary_changed.emit(segment.id, start, end)
        else:
            self.mark_in, self.mark_out = start, end
            self.mark_segment_id = ""
        self.range_changed.emit(self._drag_id, start, end)
        self.update()

    def _current_drag_range(self) -> tuple[float, float]:
        segment = self._segment(self._drag_id)
        if segment:
            return segment.start, segment.end
        return self.mark_in, self.mark_out

    def _clear_drag(self) -> None:
        self._drag_id = ""
        self._drag_kind = ""
        self._drag_active = False

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        anchor = self._x_to_time(event.position().x())
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.set_zoom(self.zoom * factor, anchor)
        event.accept()
