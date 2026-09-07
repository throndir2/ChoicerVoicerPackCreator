from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QContextMenuEvent, QMouseEvent

from choicer_voicer_pack_creator.ui.readable_table import ReadableTableWidget


class SegmentTableWidget(ReadableTableWidget):
    segment_context_menu_requested = Signal(str, QPoint)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            # Let the context menu choose its target without the normal selection seek.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
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
