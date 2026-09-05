from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.youtube import (
    YouTubeCancelled,
    YouTubeDownload,
    download_youtube,
    normalize_youtube_url,
)


class YouTubeWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()

    def __init__(self, media: MediaTools, url: str, folder: Path, language: str) -> None:
        super().__init__()
        self.media, self.url, self.folder, self.language = media, url, folder, language

    def run(self) -> None:
        def report(message: str, fraction: float | None) -> None:
            self.progress.emit(
                message, -1 if fraction is None else max(0, min(1000, round(fraction * 1000)))
            )

        try:
            result = download_youtube(
                self.media, self.url, self.folder, self.language,
                progress=report, cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(result)
        except YouTubeCancelled:
            self.canceled.emit()
        except Exception as error:
            self.failed.emit(str(error))


class YouTubeDialog(QDialog):
    def __init__(self, media: MediaTools, folder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.media = media
        self.worker: YouTubeWorker | None = None
        self.download_result: YouTubeDownload | None = None
        self._close_after_cancel = False
        self.setWindowTitle("New from YouTube")
        self.resize(650, 300)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Download one public YouTube video that you own or have permission to use. "
            "Available creator or automatic captions load first; local Whisper then runs "
            "automatically for comparison (first-time model downloads ask permission). "
            "No sign-in, cookies, or access-restriction bypass is used."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=...")
        form.addRow("Video URL", self.url_edit)
        self.folder_edit = QLineEdit(folder)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.folder_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("Keep downloaded media in", row)
        self.language_combo = QComboBox()
        self.language_combo.setEditable(True)
        self.language_combo.addItem("Original language (auto)", "auto")
        for code in ("en", "ja", "zh", "ko", "es", "fr", "de", "pt", "ru"):
            self.language_combo.addItem(code, code)
        self.language_combo.setToolTip(
            "Choose or type a language code such as en or pt-BR. "
            "Creator captions are preferred; automatic translations are not used."
        )
        form.addRow("Caption language", self.language_combo)
        layout.addLayout(form)
        note = QLabel(
            "Each import creates a separate media folder; existing files are never replaced. "
            "Keep this folder with your saved project. Playlists and live streams are not imported."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.progress_label = QLabel("Ready")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.download_button = QPushButton("Download Video")
        self.download_button.setObjectName("primary")
        self.download_button.clicked.connect(self.start_download)
        buttons.addButton(self.download_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Keep downloaded media in", self.folder_edit.text()
        )
        if folder:
            self.folder_edit.setText(folder)

    def start_download(self) -> None:
        if self.worker is not None:
            return
        try:
            url = normalize_youtube_url(self.url_edit.text())
            if not self.folder_edit.text().strip():
                raise ValueError("Choose a folder for the downloaded video.")
            folder = Path(self.folder_edit.text().strip()).resolve()
            if not folder.is_dir():
                raise ValueError("The media destination must be an existing folder.")
        except ValueError as error:
            QMessageBox.warning(self, "Cannot download video", str(error))
            return
        language = self.language_combo.currentData()
        if self.language_combo.currentText() != self.language_combo.itemText(
            self.language_combo.currentIndex()
        ):
            language = self.language_combo.currentText().strip()
        worker = YouTubeWorker(self.media, url, folder, str(language or "auto"))
        self.worker = worker
        self.download_result = None
        for widget in (
            self.url_edit, self.folder_edit, self.browse_button,
            self.language_combo, self.download_button,
        ):
            widget.setEnabled(False)
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.canceled.connect(lambda: self.progress_label.setText("Download canceled"))
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        self.progress_label.setText(message)
        self.progress_bar.setRange(0, 0 if value < 0 else 1000)
        if value >= 0:
            self.progress_bar.setValue(value)

    @Slot(object)
    def _completed(self, result: object) -> None:
        if not isinstance(result, YouTubeDownload):
            self._failed("The downloader returned an invalid result.")
            return
        self.download_result = result

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.progress_label.setText("Download failed; existing media and project are unchanged.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        QMessageBox.critical(self, "YouTube download failed", message)

    @Slot()
    def _finished(self) -> None:
        self.worker = None
        if self._close_after_cancel:
            super().reject()
        elif self.download_result is not None:
            super().accept()
        else:
            for widget in (
                self.url_edit, self.folder_edit, self.browse_button,
                self.language_combo, self.download_button,
            ):
                widget.setEnabled(True)

    def reject(self) -> None:
        if self.worker is not None:
            self._close_after_cancel = True
            self.worker.requestInterruption()
            self.progress_label.setText(
                "Canceling download; waiting for the current network request or media merge..."
            )
        else:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
