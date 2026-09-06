from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.separation import (
    SeparationCancelled,
    SeparationDownloadRequired,
    SeparationManager,
)


class BackingWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    download_required = Signal()
    canceled = Signal()

    def __init__(
        self,
        manager: SeparationManager,
        media: MediaTools,
        video: Path,
        *,
        allow_download: bool,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.media = media
        self.video = video
        self.allow_download = allow_download

    def run(self) -> None:
        def report(message: str, fraction: float | None) -> None:
            value = -1 if fraction is None else max(0, min(1000, round(fraction * 1000)))
            self.progress.emit(message, value)

        try:
            result = self.manager.generate(
                self.media,
                self.video,
                allow_download=self.allow_download,
                progress=report,
                cancelled=self.isInterruptionRequested,
            )
            self.completed.emit(result)
        except SeparationDownloadRequired:
            self.download_required.emit()
        except SeparationCancelled:
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception("backing_worker_failed", error)
            self.failed.emit(str(error))


class BackingDialog(QDialog):
    def __init__(
        self,
        media: MediaTools,
        video: Path,
        data_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.video = video
        self.manager = SeparationManager(data_root)
        self.worker: BackingWorker | None = None
        self.backing_path: Path | None = None
        self._outcome = ""
        self._closing = False
        self.setWindowTitle("Generate backing track")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Separate music and effects from the original dialogue, locally on your CPU. "
            "No audio is uploaded. Your captions, speakers, timings and prompt files will not change.\n\n"
            "Separation can take several minutes and may leave some voice bleed or remove "
            "some effects. Listen to the backing before sharing the pack."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.progress_label = QLabel("Preparing backing generation...")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.retry_button = QPushButton("Retry")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(lambda: self.start())
        buttons.addWidget(self.retry_button)
        self.close_button = QPushButton("Cancel")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self.start)

    def start(self, *, allow_download: bool = False) -> None:
        if self.worker is not None or self._closing:
            return
        self._outcome = ""
        self.backing_path = None
        self.retry_button.setVisible(False)
        self.close_button.setText("Cancel")
        self.progress_bar.setFormat("%p%")
        self._progress("Checking local separation model...", -1)
        worker = BackingWorker(
            self.manager, self.media, self.video, allow_download=allow_download,
        )
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.download_required.connect(self._download_required)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._worker_finished)
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
        if not isinstance(result, Path) or not result.is_file():
            self._failed("Backing generation returned no usable audio file.")
            return
        self.backing_path = result
        self._outcome = "completed"

    @Slot()
    def _download_required(self) -> None:
        self._outcome = "download"

    @Slot(str)
    def _failed(self, message: str) -> None:
        self._outcome = "failed"
        self.progress_label.setText(
            f"{message}\n\nYour existing project and backing are unchanged. "
            "Retry here or use Tools > Generate Backing Track later."
        )
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Failed")

    @Slot()
    def _canceled(self) -> None:
        self._outcome = "canceled"

    @Slot()
    def _worker_finished(self) -> None:
        self.worker = None
        if self._closing or self._outcome == "canceled":
            self.backing_path = None
            super().reject()
        elif self._outcome == "completed":
            super().accept()
        elif self._outcome == "download":
            size = self.manager.model_download_bytes / 1024**2
            answer = QMessageBox.question(
                self,
                "Download local music-separation model?",
                f"Download approximately {size:.0f} MiB of checksum-verified model data? "
                "The model is stored in your local application data and reused offline. "
                "A missing or damaged model needs this download before generation can continue.\n\n"
                "Audio stays on this computer. Canceling keeps your imported video and dialogue "
                "work; you can generate backing later from Tools.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            diagnostic_event(
                "backing_download_consent", accepted=answer == QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.start(allow_download=True)
            else:
                super().reject()
        else:
            if self._outcome != "failed":
                self._failed("Backing generation stopped without returning a result.")
            self.retry_button.setVisible(True)
            self.close_button.setText("Close")

    def accept(self) -> None:
        if self.worker is None and self.backing_path is not None:
            super().accept()

    def reject(self) -> None:
        self._closing = True
        if self.worker is not None:
            self.worker.requestInterruption()
            self.close_button.setEnabled(False)
            self.progress_label.setText("Canceling backing generation; waiting for the worker...")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
