from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class CollapsibleSection(QFrame):
    """A titled splitter pane whose content can collapse without hiding its header."""

    collapsed_changed = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collapsibleSection")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._last_expanded_height = 220

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toggle_button = QToolButton(self)
        self.toggle_button.setObjectName("sectionToggle")
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle_button.setToolTip(f"Collapse {title.title()}")
        self.toggle_button.toggled.connect(self._expanded_toggled)
        layout.addWidget(self.toggle_button)

        self.body = QFrame(self)
        self.body.setObjectName("sectionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 8, 8, 8)
        self.body_layout.setSpacing(6)
        layout.addWidget(self.body, 1)

    @property
    def is_collapsed(self) -> bool:
        return not self.toggle_button.isChecked()

    @property
    def last_expanded_height(self) -> int:
        return self._last_expanded_height

    def set_content(self, widget: QWidget, *, scrollable: bool = False) -> None:
        if self.body_layout.count():
            raise RuntimeError("CollapsibleSection content has already been set")
        if not scrollable:
            self.body_layout.addWidget(widget, 1)
            return
        scroll = QScrollArea(self.body)
        scroll.setObjectName("sectionScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(max(120, widget.fontMetrics().height() * 5))
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        self.body_layout.addWidget(scroll, 1)

    def set_collapsed(self, collapsed: bool) -> None:
        self.toggle_button.setChecked(not collapsed)

    def set_last_expanded_height(self, height: int) -> None:
        self._last_expanded_height = max(120, height)

    def _expanded_toggled(self, expanded: bool) -> None:
        if not expanded:
            self._last_expanded_height = max(self.height(), self.sizeHint().height(), 120)
        self.body.setVisible(expanded)
        self.toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.toggle_button.setToolTip(
            f"{'Collapse' if expanded else 'Expand'} {self.toggle_button.text().title()}"
        )
        if expanded:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16_777_215)
        else:
            collapsed_height = self.toggle_button.sizeHint().height() + 2
            self.setMinimumHeight(collapsed_height)
            self.setMaximumHeight(collapsed_height)
        self.updateGeometry()
        self.collapsed_changed.emit(not expanded)