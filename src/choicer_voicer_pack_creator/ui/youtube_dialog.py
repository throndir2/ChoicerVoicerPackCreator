from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal, Slot
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

from choicer_voicer_pack_creator.diagnostics import (
    DiagnosticProgress,
    diagnostic_event,
    diagnostic_exception,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.ui.analysis_dialog import save_diagnostic_logs
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
        diagnostics = DiagnosticProgress("youtube_worker_progress")

        def report(message: str, fraction: float | None) -> None:
            diagnostics.report(message, fraction)
            self.progress.emit(
                message, -1 if fraction is None else max(
                    0, min(1000 if fraction >= 1 else 999, round(fraction * 1000))
                )
            )

        try:
            diagnostic_event("youtube_worker_started", folder=self.folder, language=self.language)
            result = download_youtube(
                self.media, self.url, self.folder, self.language,
                progress=report, cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(result)
            diagnostic_event("youtube_worker_completed")
        except YouTubeCancelled:
            diagnostic_event("youtube_worker_canceled")
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception("youtube_worker_failed", error)
            self.failed.emit(str(error))


class YouTubeDialog(QDialog):
    def __init__(
        self, media: MediaTools, folder: str, parent: QWidget | None = None,
        *, data_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.data_root = data_root or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation,
        )) / "analysis"
        self.worker: YouTubeWorker | None = None
        self.download_result: YouTubeDownload | None = None
        self._close_after_cancel = False
        self.setWindowTitle("New from YouTube")
        self.resize(650, 300)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Download one public YouTube video that you own or have permission to use. "
            "Available creator or automatic captions load first; local Whisper then runs "
            "automatically as a separate transcript (first-time model downloads ask permission). "
            "Review both and choose either YouTube's text and timings or Whisper's. "
            "No sign-in, cookies, or access-restriction bypass is used."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=...")
        form.addRow("Video URL", self.url_edit)
        self.default_folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        self.folder_edit = QLineEdit(folder.strip() or self.default_folder)
        self.folder_edit.setPlaceholderText(self.default_folder or "Choose a download folder")
        self.folder_edit.setToolTip("Leave blank to use your Windows Downloads folder.")
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
        self.progress_bar.setToolTip(
            "Percentages combine the selected video and audio transfers, not the whole import. "
            "Revised size estimates or retries can pause the percentage without moving it "
            "backward. Merging and checking have no measurable percentage; 100% means ready."
        )
        layout.addWidget(self.progress_bar)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_logs_button = QPushButton("Save Diagnostic Bundle...")
        self.save_logs_button.setAutoDefault(False)
        self.save_logs_button.clicked.connect(lambda: save_diagnostic_logs(self, self.data_root))
        buttons.addButton(self.save_logs_button, QDialogButtonBox.ButtonRole.ActionRole)
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
            folder_text = self.folder_edit.text().strip() or self.default_folder
            if not folder_text:
                raise ValueError("Choose a folder for the downloaded video.")
            folder = Path(folder_text).resolve()
            if self.default_folder and folder == Path(self.default_folder).resolve():
                folder.mkdir(parents=True, exist_ok=True)
            if not folder.is_dir():
                raise ValueError("The media destination must be an existing folder.")
        except (OSError, ValueError) as error:
            diagnostic_exception("youtube_input_rejected", error)
            QMessageBox.warning(self, "Cannot download video", str(error))
            return
        self.folder_edit.setText(str(folder))
        language = self.language_combo.currentData()
        if self.language_combo.currentText() != self.language_combo.itemText(
            self.language_combo.currentIndex()
        ):
            language = self.language_combo.currentText().strip()
        worker = YouTubeWorker(self.media, url, folder, str(language or "auto"))
        self.worker = worker
        self.download_result = None
        self._close_after_cancel = False
        self._progress("Fetching YouTube video details...", -1)
        for widget in (
            self.url_edit, self.folder_edit, self.browse_button,
            self.language_combo, self.download_button,
        ):
            widget.setEnabled(False)
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        diagnostic_event("youtube_worker_start_requested")
        worker.start()

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        if self._close_after_cancel:
            return
        self.progress_label.setText(
            f"{message} Progress is not measurable in this stage." if value < 0 else message
        )
        self.progress_bar.setRange(0, 0 if value < 0 else 1000)
        self.progress_bar.setFormat("Ready" if value == 1000 else "Transfers: %p%")
        if value >= 0:
            self.progress_bar.setValue(value)

    @Slot(object)
    def _completed(self, result: object) -> None:
        if not isinstance(result, YouTubeDownload):
            self._failed("The downloader returned an invalid result.")
            return
        self.download_result = result
        self._progress("YouTube video ready", 1000)

    @Slot()
    def _canceled(self) -> None:
        self.progress_label.setText("Download canceled")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Canceled")

    @Slot(str)
    def _failed(self, message: str) -> None:
        diagnostic_event("youtube_failure_displayed", message=message)
        self.progress_label.setText("Download failed; existing media and project are unchanged.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Failed")
        QMessageBox.critical(
            self, "YouTube download failed",
            f"{message}\n\nUse Save Diagnostic Bundle to collect logs for support.",
        )

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
            diagnostic_event("youtube_cancel_requested")
            self._close_after_cancel = True
            self.worker.requestInterruption()
            self.progress_label.setText(
                "Canceling download; stopping its network and media processes..."
            )
        else:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
