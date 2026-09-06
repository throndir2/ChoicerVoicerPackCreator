from __future__ import annotations

from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QTableWidget


class ReadableTableWidget(QTableWidget):
    """Reserve real rows instead of allowing splitter panes to shrink to a header."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        height = (
            self.horizontalHeader().sizeHint().height()
            + 2 * self.verticalHeader().defaultSectionSize()
            + self.horizontalScrollBar().sizeHint().height()
            + 2 * self.frameWidth()
        )
        hint.setHeight(max(hint.height(), height))
        return hint

    def showEvent(self, event) -> None:  # noqa: N802
        self.setMinimumHeight(self.minimumSizeHint().height())
        super().showEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self.setMinimumHeight(self.minimumSizeHint().height())
