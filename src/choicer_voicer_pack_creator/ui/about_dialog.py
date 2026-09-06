from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.diagnostics import diagnostic_event

SUPPORT_URL = "https://www.buymeacoffee.com/throndir"


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Choicer Voicer Pack Creator")
        self.resize(600, 400)

        layout = QVBoxLayout(self)
        description = QLabel(
            f"<h3>Choicer Voicer Pack Creator {__version__}</h3>"
            "<p>An unofficial community desktop editor for creating, importing, and validating "
            "Choicer Voicer dub packs.</p>"
            "<p>The desktop interface uses PySide6/Qt. Windows bundles include an unmodified "
            "FFmpeg LGPL shared build for media conversion; its license, provenance, and source "
            "links are in <code>THIRD_PARTY_NOTICES.md</code>.</p>"
            "<p>Godot is <b>not</b> the GUI framework or an end-user dependency. Release tests use "
            "Godot's native <code>ConfigFile</code> parser because The Choicer Voicer is a Godot "
            "application and reads pack metadata with that parser.</p>"
            "<p>Optional video analysis uses deterministic audio-energy scanning and can download "
            "a pinned local whisper.cpp CPU runtime/model. No media is uploaded. Transcripts and "
            "timestamps are editable suggestions, never correctness claims.</p>"
            "<p>Project files store paths and edit decisions only. Source media remains yours.</p>"
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        controls = QHBoxLayout()
        self.support_button = QPushButton(self)
        self.support_button.setObjectName("coffeeSupport")
        self.support_button.setAccessibleName("Buy Me a Coffee")
        self.support_button.setToolTip(f"Support development - opens in your browser:\n{SUPPORT_URL}")
        self.support_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.support_button.setAutoDefault(False)
        self.support_button.setIcon(QIcon(str(
            Path(__file__).resolve().parent.parent / "resources" / "buy-me-a-coffee.png"
        )))
        self.support_button.setIconSize(QSize(144, 40))
        self.support_button.setFixedSize(150, 46)
        self.support_button.clicked.connect(self._open_support_page)
        controls.addWidget(self.support_button)
        controls.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setDefault(True)
        buttons.rejected.connect(self.reject)
        controls.addWidget(buttons)
        layout.addLayout(controls)

    def _open_support_page(self) -> None:
        opened = QDesktopServices.openUrl(QUrl(SUPPORT_URL))
        diagnostic_event("support_page_opened", url=SUPPORT_URL, opened=opened)
        if not opened:
            QMessageBox.warning(
                self, "Could not open browser", f"Open this URL manually:\n{SUPPORT_URL}"
            )
