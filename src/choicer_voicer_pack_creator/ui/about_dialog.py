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
    QScrollArea,
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
        self.resize(600, 430)

        layout = QVBoxLayout(self)
        description = QLabel(
            "<h3>Choicer Voicer Pack Creator</h3>"
            f"<p>Version {__version__}</p>"
            "<p>A desktop editor for creating, editing, importing, and validating dub packs "
            "for <i>The Choicer Voicer</i>.</p>"
            "<p>Unofficial tool. Not affiliated with the creators of "
            "<i>The Choicer Voicer</i>.</p>"
            "<p>Licensed under the MIT License. Third-party licenses and source information "
            "are listed in <code>THIRD_PARTY_NOTICES.md</code>.</p>"
            "<p><b>Keep singing; remove dialogue</b> uses the "
            '<a href="https://zenodo.org/records/13327983">Facing the Music BandIt combined model</a> '
            "by <b>Karn N. Watcharasupat, Chih-Wei Wu, and Iroro Orife</b>. "
            'The <a href="https://github.com/kwatcharasupat/bandit-v2">'
            "BandIt source</a> is <b>Apache-2.0</b>; the optional model weights are "
            '<a href="https://creativecommons.org/licenses/by-nc/4.0/">'
            "<b>CC BY-NC 4.0 (non-commercial use)</b></a>, not MIT or unrestricted. "
            "The creators do not endorse this application.</p>"
        )
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignmentFlag.AlignTop)
        description.setOpenExternalLinks(True)
        description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(description)
        layout.addWidget(scroll)

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
