from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QAbstractItemView, QApplication

from choicer_voicer_pack_creator.ui.readable_table import ReadableTableWidget

_AUTO_SCROLL_MARGIN = 28
_AUTO_SCROLL_INTERVAL_MS = 30
_DROP_LINE_COLOR = QColor("#42d6e7")


class SegmentTableWidget(ReadableTableWidget):
    segment_context_menu_requested = Signal(str, QPoint)
    # Segment IDs in list order and the row boundary (0 = top) where they were dropped.
    segments_move_requested = Signal(list, int)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._press_position: QPoint | None = None
        self._swallow_release = False
        self._move_ids: list[str] = []
        self._drop_row = -1
        self._pointer = QPoint()
        self._scroll_carry = 0.0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(_AUTO_SCROLL_INTERVAL_MS)
        self._scroll_timer.timeout.connect(self._auto_scroll)

    @property
    def moving_rows(self) -> bool:
        return bool(self._move_ids)

    @property
    def drop_row(self) -> int:
        return self._drop_row

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._swallow_release = False
        if event.button() == Qt.MouseButton.RightButton:
            # Let the context menu choose its target without the normal selection seek.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
            )
            and not self._move_ids
        ):
            position = event.position().toPoint()
            item = self.item(self.rowAt(position.y()), 0)
            if item is not None and item.isSelected():
                # Keep the selection so it can be dragged; a click without a drag
                # selects only this row when the button is released.
                self._press_position = position
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = event.position().toPoint()
        if self._swallow_release and event.buttons() & Qt.MouseButton.LeftButton:
            event.accept()
            return
        if self._move_ids:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self.cancel_row_move()
            else:
                self._pointer = position
                self._update_drop_row()
            event.accept()
            return
        if self._press_position is not None:
            if (
                event.buttons() & Qt.MouseButton.LeftButton
                and (position - self._press_position).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._start_row_move(position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._swallow_release:
            self._swallow_release = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._move_ids:
            identifiers, row = self._move_ids, self._drop_row
            self._end_row_move()
            event.accept()
            if row >= 0:
                self.segments_move_requested.emit(identifiers, row)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_position is not None:
            position = QPointF(self._press_position)
            self._press_position = None
            super().mousePressEvent(QMouseEvent(
                QEvent.Type.MouseButtonPress, position, self.viewport().mapToGlobal(position),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ))
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._move_ids and event.key() == Qt.Key.Key_Escape:
            self.cancel_row_move()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.cancel_row_move()
        super().focusOutEvent(event)

    def cancel_row_move(self) -> None:
        if self._press_position is None and not self._move_ids:
            return
        # The button is probably still down; its release must not act as a click.
        self._swallow_release = True
        self._end_row_move()

    def _end_row_move(self) -> None:
        self._press_position = None
        if not self._move_ids:
            return
        self._scroll_timer.stop()
        self._move_ids = []
        self._drop_row = -1
        self._scroll_carry = 0.0
        self.viewport().unsetCursor()
        self.viewport().update()

    def _start_row_move(self, position: QPoint) -> None:
        rows = sorted(index.row() for index in self.selectionModel().selectedRows())
        self._press_position = None
        self._move_ids = [str(self.item(row, 0).data(Qt.ItemDataRole.UserRole)) for row in rows]
        if not self._move_ids:
            return
        self._pointer = position
        self._scroll_carry = 0.0
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self._scroll_timer.start()
        self._update_drop_row()

    def _drop_row_at(self, y: int) -> int:
        count = self.rowCount()
        height = self.viewport().height()
        row = self.rowAt(min(max(y, 0), max(0, height - 1)))
        if row < 0:
            return count
        if y < 0:
            return row
        if y >= height:
            return row + 1
        return row + int(y >= self.rowViewportPosition(row) + self.rowHeight(row) / 2)

    def _update_drop_row(self) -> None:
        row = self._drop_row_at(self._pointer.y())
        if row != self._drop_row:
            self._drop_row = row
            self.viewport().update()

    def _auto_scroll(self) -> None:
        if not self._move_ids:
            self._scroll_timer.stop()
            return
        y, height = self._pointer.y(), self.viewport().height()
        margin = min(_AUTO_SCROLL_MARGIN, height // 3)
        if y < margin:
            distance, direction = margin - y, -1
        elif y > height - margin:
            distance, direction = y - (height - margin), 1
        else:
            self._scroll_carry = 0.0
            return
        # Scroll faster the further the pointer is past the edge zone.
        speed = 0.15 + min(distance / max(1, margin), 6.0) * 0.25
        if self.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel:
            speed *= self.verticalHeader().defaultSectionSize()
        self._scroll_carry += speed
        step = int(self._scroll_carry)
        if not step:
            return
        self._scroll_carry -= step
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + direction * step)
        self._update_drop_row()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        count = self.rowCount()
        if not self._move_ids or self._drop_row < 0 or count == 0:
            return
        if self._drop_row < count:
            y = self.rowViewportPosition(self._drop_row)
        else:
            y = self.rowViewportPosition(count - 1) + self.rowHeight(count - 1)
        height = self.viewport().height()
        if not -2 <= y <= height + 2:
            return
        y = min(max(y, 1), height - 2)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(_DROP_LINE_COLOR, 2))
        painter.drawLine(0, y, self.viewport().width(), y)
        painter.end()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        if self._move_ids:
            event.accept()
            return
        keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        row = self.currentRow() if keyboard else self.rowAt(event.pos().y())
        item = self.item(row, 0)
        if keyboard and (item is None or not item.isSelected()):
            selected = self.selectionModel().selectedRows()
            item = self.item(selected[0].row(), 0) if selected else None
        if item is not None:
            position = (
                self.viewport().mapToGlobal(self.visualItemRect(item).center())
                if keyboard else event.globalPos()
            )
            self.segment_context_menu_requested.emit(
                str(item.data(Qt.ItemDataRole.UserRole)), position,
            )
        event.accept()