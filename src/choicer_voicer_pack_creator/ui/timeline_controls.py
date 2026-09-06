from __future__ import annotations

from PySide6.QtCore import QEvent
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget


class TimelineControlBar(QWidget):
    """Keep segment actions beside the range, wrapping only in a narrow video pane."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.range_layout = QHBoxLayout()
        self.actions_layout = QHBoxLayout()
        self.zoom_layout = QHBoxLayout()
        for layout in (self.range_layout, self.actions_layout, self.zoom_layout):
            layout.setSpacing(6)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(6)
        self._grid.addLayout(self.range_layout, 0, 0)
        self._grid.addLayout(self.actions_layout, 0, 1)
        self._grid.setColumnStretch(2, 1)
        self._grid.addLayout(self.zoom_layout, 0, 3)
        self._single_row = True

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._arrange()

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._arrange()
        return handled

    def _arrange(self) -> None:
        preferred_width = sum(
            layout.sizeHint().width()
            for layout in (self.range_layout, self.actions_layout, self.zoom_layout)
        ) + 3 * self._grid.horizontalSpacing()
        single_row = self.width() >= preferred_width
        if single_row == self._single_row:
            return
        self._single_row = single_row
        self._grid.removeItem(self.range_layout)
        self._grid.removeItem(self.actions_layout)
        self._grid.addLayout(self.range_layout, 0, 0, 1, 1 if single_row else 3)
        self._grid.addLayout(
            self.actions_layout, 0 if single_row else 1, 1 if single_row else 0,
            1, 1 if single_row else 4,
        )
        self._grid.activate()
