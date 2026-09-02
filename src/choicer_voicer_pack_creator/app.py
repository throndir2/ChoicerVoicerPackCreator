from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from choicer_voicer_pack_creator.media import MediaError, MediaTools
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv
    QCoreApplication.setOrganizationName("ChoicerVoicerCommunity")
    QCoreApplication.setApplicationName("Choicer Voicer Pack Creator")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    app = QApplication(arguments)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    icon_path = bundle_root / "assets" / "icon.svg"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        media = MediaTools()
    except MediaError as error:
        QMessageBox.critical(
            None,
            "FFmpeg is required",
            f"{error}\n\nInstall FFmpeg, then restart Choicer Voicer Pack Creator.",
        )
        return 2

    smoke_test = "--smoke-test" in arguments
    paths = [item for item in arguments[1:] if not item.startswith("--")]
    initial_path = Path(paths[0]).resolve() if paths else None
    window = MainWindow(media, initial_path)
    window.show()
    if smoke_test:
        window.dirty = False
        QTimer.singleShot(350, app.quit)
    return app.exec()
