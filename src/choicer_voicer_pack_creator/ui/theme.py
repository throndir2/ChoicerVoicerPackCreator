APP_STYLESHEET = """
QWidget {
    color: #e7edf7;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
}
QMainWindow, QDialog { background: #080d14; }
QMenuBar, QMenu, QToolBar, QStatusBar { background: #0d141f; color: #dbe7f7; }
QMenuBar::item:selected, QMenu::item:selected { background: #19344c; }
QToolBar { border: 0; border-bottom: 1px solid #233246; spacing: 6px; padding: 6px; }
QStatusBar { border-top: 1px solid #233246; }
QGroupBox {
    background: #0d141f;
    border: 1px solid #233246;
    border-radius: 8px;
    margin-top: 11px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
    color: #9eb0c6;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QFrame#collapsibleSection {
    background: #0d141f;
    border: 1px solid #233246;
    border-radius: 8px;
}
QToolButton#sectionToggle {
    background: #121d2b;
    color: #aebed1;
    border: 0;
    border-bottom: 1px solid #233246;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 7px 9px;
    font-weight: 700;
    text-align: left;
}
QToolButton#sectionToggle:hover { background: #192a3d; color: #d9e7f6; }
QFrame#sectionBody, QScrollArea#sectionScroll, QScrollArea#sectionScroll > QWidget > QWidget {
    background: #0d141f;
    border: 0;
}
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTableWidget {
    background: #111b28;
    border: 1px solid #2a3c52;
    border-radius: 5px;
    padding: 5px;
    selection-background-color: #19617c;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #42d6e7;
}
QPushButton {
    background: #162334;
    border: 1px solid #324a64;
    border-radius: 5px;
    padding: 6px 10px;
    font-weight: 600;
}
QPushButton:hover { background: #1c3047; border-color: #49cddd; }
QPushButton:pressed { background: #102030; }
QPushButton:disabled { color: #617086; background: #111720; border-color: #202a38; }
QPushButton#primary:enabled { background: #087a8d; border-color: #33d1df; color: white; }
QPushButton#primary:enabled:hover { background: #0994aa; }
QPushButton#danger { color: #ff9999; border-color: #733b47; }
QPushButton#danger:hover { background: #512832; }
QTableWidget {
    background: #0b121c;
    gridline-color: #1e2b3b;
    alternate-background-color: #0f1925;
    border-radius: 7px;
}
QHeaderView::section {
    background: #152235;
    color: #9fb1c8;
    border: 0;
    border-right: 1px solid #26374b;
    border-bottom: 1px solid #26374b;
    padding: 6px;
    font-weight: 600;
}
QTableWidget::item:selected { background: #174d66; }
QScrollBar:vertical, QScrollBar:horizontal { background: #0b121b; width: 12px; height: 12px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #30455e; border-radius: 5px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #3d607c; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QSplitter::handle { background: transparent; }
QSplitter::handle:vertical {
    height: 9px;
    margin: 4px 24px;
    border-top: 1px solid #2b3d52;
}
QSplitter::handle:horizontal {
    width: 9px;
    margin: 24px 4px;
    border-left: 1px solid #2b3d52;
}
QSplitter::handle:vertical:hover { border-top-color: #55cfe0; }
QSplitter::handle:horizontal:hover { border-left-color: #55cfe0; }
QLabel#muted { color: #7f91a8; }
QLabel#path { color: #8ea2ba; font-size: 9pt; }
QProgressBar { background: #111b28; border: 1px solid #2a3c52; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #14aabe; border-radius: 3px; }
"""

SEGMENT_COLORS = (
    "#33d1df",
    "#ffb454",
    "#9b8cff",
    "#59d492",
    "#ff7c9d",
    "#68a7ff",
    "#e6d96a",
    "#c989ff",
)
