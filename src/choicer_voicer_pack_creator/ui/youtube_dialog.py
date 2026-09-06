from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, Signal, Slot
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
from choicer_voicer_pack_creator.ui.analysis_dialog import (
    _current_dialog_request,
    register_job_detail,
    save_diagnostic_logs,
    show_message,
)
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.youtube import (
    ExistingYouTubeImport,
    YouTubeCancelled,
    YouTubeDownload,
    YouTubeImportConflict,
    download_youtube,
    normalize_youtube_url,
)


class YouTubeWorker(JobWorker):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()

    def __init__(
        self, media: MediaTools, url: str, folder: Path, language: str,
        *, create_folder: bool = False,
        existing: ExistingYouTubeImport | None = None, overwrite: bool = False,
    ) -> None:
        super().__init__()
        self.media, self.url, self.folder, self.language = media, url, folder, language
        self.create_folder = create_folder
        self.existing, self.overwrite = existing, overwrite

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
            if self.create_folder:
                self.folder.mkdir(parents=True, exist_ok=True)
            if not self.folder.is_dir():
                raise ValueError("The media destination must be an existing folder.")
            diagnostic_event("youtube_worker_started", folder=self.folder, language=self.language)
            result = download_youtube(
                self.media, self.url, self.folder, self.language,
                progress=report, cancelled=self.isInterruptionRequested,
                existing=self.existing, overwrite=self.overwrite,
            )
            self.completed.emit(result)
            diagnostic_event("youtube_worker_completed")
        except YouTubeCancelled:
            diagnostic_event("youtube_worker_canceled")
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception("youtube_worker_failed", error)
            self.failed.emit(str(error))


class YouTubeConflictDialog(QDialog):
    def __init__(self, conflict: YouTubeImportConflict, parent: QWidget) -> None:
        super().__init__(parent)
        self.imports = conflict.imports
        self.overwrite = False
        self.setWindowTitle("YouTube import already exists")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(650, 250)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "An import of this video already exists. Use its complete video and fetch the "
            "title/captions again, or download a replacement. Overwrite replaces source download "
            "files only after the new video is ready; other files are kept. "
            "Projects using the replaced source files may be affected."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.folder_combo = QComboBox()
        for item in self.imports:
            self.folder_combo.addItem(str(item.folder))
        self.folder_combo.setCurrentIndex(next(
            (index for index, item in enumerate(self.imports) if not item.reuse_problem), 0,
        ))
        layout.addWidget(self.folder_combo)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.reuse_button = buttons.addButton("Use Existing", QDialogButtonBox.ButtonRole.AcceptRole)
        self.overwrite_button = buttons.addButton("Overwrite", QDialogButtonBox.ButtonRole.ActionRole)
        self.reuse_button.clicked.connect(self.accept)
        self.overwrite_button.clicked.connect(self._overwrite)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.folder_combo.currentIndexChanged.connect(self._selection_changed)
        self._selection_changed()

    @property
    def selected_import(self) -> ExistingYouTubeImport:
        return self.imports[self.folder_combo.currentIndex()]

    def _selection_changed(self) -> None:
        problem = self.selected_import.reuse_problem
        self.status_label.setText(problem or "Complete video with audio found. No video download needed.")
        self.reuse_button.setEnabled(not problem)
        self.reuse_button.setDefault(not problem)
        self.overwrite_button.setAutoDefault(False)

    def _overwrite(self) -> None:
        self.overwrite = True
        self.accept()


class YouTubeDialog(QDialog):
    download_started = Signal()

    def __init__(
        self, media: MediaTools, folder: str, parent: QWidget | None = None,
        *, data_root: Path | None = None,
        job_manager=None, project_id: str | None = None, source_snapshot=None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.job_manager = job_manager
        self.project_id = project_id
        self.source_snapshot = source_snapshot
        if job_manager is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.data_root = data_root or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation,
        )) / "analysis"
        self.worker: YouTubeWorker | None = None
        self.download_result: YouTubeDownload | None = None
        self._pending_conflict: YouTubeImportConflict | None = None
        self._conflict_dialog: YouTubeConflictDialog | None = None
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
            "If this video was imported before, choose whether to use its existing video "
            "or explicitly overwrite it. New videos get a separate media folder. "
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
        buttons.rejected.connect(self.cancel_download)
        if job_manager is not None:
            hide_button = QPushButton("Hide")
            hide_button.clicked.connect(self.hide)
            buttons.addButton(hide_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Keep downloaded media in", self.folder_edit.text()
        )
        if folder:
            self.folder_edit.setText(folder)

    def start_download(self) -> None:
        if self.worker is not None or self._conflict_dialog is not None:
            return
        try:
            url = normalize_youtube_url(self.url_edit.text())
            folder_text = self.folder_edit.text().strip() or self.default_folder
            if not folder_text:
                raise ValueError("Choose a folder for the downloaded video.")
            folder = Path(folder_text).resolve()
            create_folder = bool(
                self.default_folder and folder == Path(self.default_folder).resolve()
            )
            if self.job_manager is None:
                if create_folder:
                    folder.mkdir(parents=True, exist_ok=True)
                if not folder.is_dir():
                    raise ValueError("The media destination must be an existing folder.")
        except (OSError, ValueError) as error:
            diagnostic_exception("youtube_input_rejected", error)
            self.progress_label.setText(str(error))
            show_message(self, "warning", "Cannot download video", str(error))
            return
        self.folder_edit.setText(str(folder))
        language = self.language_combo.currentData()
        if self.language_combo.currentText() != self.language_combo.itemText(
            self.language_combo.currentIndex()
        ):
            language = self.language_combo.currentText().strip()
        self._start_worker(url, folder, str(language or "auto"), create_folder=create_folder)

    def _start_worker(
        self, url: str, folder: Path, language: str, *, create_folder: bool = False,
        existing: ExistingYouTubeImport | None = None, overwrite: bool = False,
    ) -> None:
        worker = YouTubeWorker(
            self.media, url, folder, language,
            create_folder=create_folder, existing=existing, overwrite=overwrite,
        )
        self.worker = worker
        if self.job_manager is not None:
            worker.configure_job(
                self.job_manager, self.project_id, "youtube", "Import YouTube video",
                resource_class="network", write_paths=(folder,),
                source_snapshot=self.source_snapshot,
            )
        self.download_result = None
        self._pending_conflict = None
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
        self.download_started.emit()
        worker.start()
        register_job_detail(
            self, worker, retry=self.start_download,
            available=lambda: (
                self.worker is None and self.download_result is None
                and self._conflict_dialog is None
            ),
        )

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
        if self._close_after_cancel:
            return
        if isinstance(result, YouTubeImportConflict):
            self._pending_conflict = result
            return
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
        self.progress_label.setText(
            f"Import failed: {message}\n"
            "Use Save Diagnostic Bundle to collect logs for support."
        )
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Failed")
        if self.job_manager is None:
            QMessageBox.critical(
                self, "YouTube download failed",
                f"{message}\n\nUse Save Diagnostic Bundle to collect logs for support.",
            )

    @Slot()
    def _finished(self) -> None:
        worker = self.worker
        self.worker = None
        if self._close_after_cancel:
            super().reject()
        elif self.download_result is not None:
            super().accept()
        elif self._pending_conflict is not None and worker is not None:
            self._show_conflict(self._pending_conflict, worker)
        else:
            self._enable_inputs()

    def _enable_inputs(self) -> None:
        for widget in (
            self.url_edit, self.folder_edit, self.browse_button,
            self.language_combo, self.download_button,
        ):
            widget.setEnabled(True)

    def _show_conflict(self, conflict: YouTubeImportConflict, worker: YouTubeWorker) -> None:
        self._pending_conflict = None
        dialog = YouTubeConflictDialog(conflict, self)
        self._conflict_dialog = dialog
        url, folder, language = worker.url, worker.folder, worker.language
        current = _current_dialog_request(self)
        self.progress_label.setText("Waiting for your existing-import choice.")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Waiting for choice")

        def chosen(result: int) -> None:
            self._conflict_dialog = None
            existing, overwrite = dialog.selected_import, dialog.overwrite
            dialog.deleteLater()
            diagnostic_event(
                "youtube_existing_import_choice", folder=existing.folder,
                accepted=result == QDialog.DialogCode.Accepted, overwrite=overwrite,
            )
            if (
                result == QDialog.DialogCode.Accepted and not self._close_after_cancel
                and current()
            ):
                self._start_worker(url, folder, language, existing=existing, overwrite=overwrite)
            else:
                self._canceled()
                self._enable_inputs()
                if not current():
                    self.progress_label.setText(
                        "The project or source changed. Start a new YouTube import."
                    )

        dialog.finished.connect(chosen)
        dialog.show()

    def reject(self) -> None:
        if self.job_manager is not None:
            self.hide()
            return
        self.cancel_download()

    def cancel_download(self) -> None:
        if self._conflict_dialog is not None:
            self._conflict_dialog.reject()
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
        if self.job_manager is not None:
            self.hide()
            event.ignore()
        elif self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
