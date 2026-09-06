from __future__ import annotations

from functools import cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QToolButton, QWidget

_DRAWINGS = {
    "new": '<rect x="3" y="5" width="12" height="14" rx="2"/>'
    '<path d="m15 10 5-3v10l-5-3M6 12h6m-3-3v6"/>',
    "link": '<path d="m10 8 2-2a4 4 0 0 1 6 6l-2 2M14 16l-2 2a4 4 0 0 1-6-6l2-2m1 6 6-8"/>',
    "open": '<path d="M3 8V5h6l2 3h10l-3 12H3V8h18"/>',
    "import": '<path d="M3 9V5h6l2 3h10v12H3v-3m-1-4h11m-3-3 3 3-3 3"/>',
    "archive": '<rect x="4" y="3" width="16" height="18" rx="2"/>'
    '<path d="M10 3v11h4V3m-4 4h4m-4 4h4m-4 7h4"/>',
    "save": '<path d="M4 3h13l3 3v15H4V3m4 0v6h8V3M8 21v-8h8v8"/>',
    "export": '<path d="M13 3h8v8m0-8L10 14M9 5H3v16h16v-6"/>',
    "restore": '<path d="M4 10a8 8 0 1 1 1 8M4 4v6h6m2-3v6l4 2"/>',
    "analyze": '<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6M5 10h2l2-4 2 8 2-4h2"/>',
    "backing": '<path d="M9 17V5l11-2v12M9 9l11-2"/>'
    '<ellipse cx="6" cy="18" rx="3" ry="2"/><ellipse cx="17" cy="16" rx="3" ry="2"/>',
    "add": '<circle cx="12" cy="12" r="9"/><path d="M7 12h10m-5-5v10"/>',
    "split": '<circle cx="5" cy="6" r="3"/><circle cx="5" cy="18" r="3"/>'
    '<path d="m7 8 13 13M7 16 20 3"/>',
    "combine": '<path d="M3 5h4l7 7h7m-4-4 4 4-4 4M3 19h4l7-7"/>',
    "duplicate": '<rect x="8" y="8" width="13" height="13" rx="2"/>'
    '<path d="M16 8V3H3v13h5"/>',
    "delete": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "play": '<path d="m7 4 13 8-13 8Z"/>',
    "pause": '<path d="M8 4v16m8-16v16"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="1"/>',
    "apply": '<path d="m4 12 5 5L20 6"/>',
    "mark-in": '<path d="M8 4H4v16h4m2-8h11m-7-4-4 4 4 4"/>',
    "mark-out": '<path d="M16 4h4v16h-4M3 12h11m-4-4 4 4-4 4"/>',
    "tasks": '<path d="m3 6 2 2 3-4m3 2h10M3 13h4m4 0h10M3 20h4m4 0h10"/>',
    "logs": '<path d="M5 3h10l4 4v14H5V3m10 0v5h4M8 12h8m-8 4h8"/>',
    "help": '<circle cx="12" cy="12" r="9"/>'
    '<path d="M9 8a3 3 0 0 1 6 1c0 2-3 2-3 5m0 3h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
}


@cache
def command_icon(name: str) -> QIcon:
    """Render our outline icons at common display scales, including a muted disabled state."""
    drawing = _DRAWINGS[name]
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, "#dbe7f7"), (QIcon.Mode.Disabled, "#617086")):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'fill="none" stroke="{color}" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{drawing}</svg>'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("ascii")))
        for size in (16, 20, 24, 32, 40, 48, 64):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


def describe_action(
    action: QAction, icon: str, description: str, *, label: str | None = None,
) -> None:
    action.setIcon(command_icon(icon))
    # Native menu styles can use the icon gutter for the check indicator.
    action.setIconVisibleInMenu(not action.isCheckable())
    if label is not None:
        action.setIconText(label)
    shortcuts = " / ".join(
        shortcut.toString(QKeySequence.SequenceFormat.NativeText)
        for shortcut in action.shortcuts()
    )
    action.setToolTip(f"{description} ({shortcuts})" if shortcuts else description)
    action.setStatusTip(description)


def action_button(
    action: QAction, parent: QWidget, *, compact: bool = False,
) -> QToolButton:
    button = QToolButton(parent)
    button.setDefaultAction(action)
    button.setToolButtonStyle(
        Qt.ToolButtonStyle.ToolButtonIconOnly if compact
        else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    )
    button.setIconSize(QSize(20, 20))
    button.setAutoRaise(True)
    button.setAccessibleName(action.text().replace("&&", "&"))
    return button
