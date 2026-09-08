from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
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
from choicer_voicer_pack_creator.separation_types import (
    KEEP_SINGING,
    REMOVE_ALL_VOCALS,
    BackingMode,
    validate_backing_mode,
)
from choicer_voicer_pack_creator.ui.analysis_dialog import (
    _current_dialog_request,
    _workspace_for,
    register_job_detail,
    report_processing,
    show_message,
)
from choicer_voicer_pack_creator.ui.job_worker import JobWorker


class BackingWorker(JobWorker):
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
            if not isinstance(result, Path) or not result.is_file():
                raise ValueError("Backing generation returned no usable audio file.")
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
        *,
        job_manager=None,
        project_id: str | None = None,
        source_snapshot=None,
        mode: BackingMode = REMOVE_ALL_VOCALS,
        auto_start: bool = False,
        before_start: Callable[[BackingMode], bool] | None = None,
        request_current: Callable[[], bool] | None = None,
        on_stale: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.video = video
        self.data_root = data_root
        self.job_manager = job_manager
        self.project_id = project_id
        self.source_snapshot = source_snapshot
        if job_manager is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.mode = validate_backing_mode(REMOVE_ALL_VOCALS if auto_start else mode)
        self.manager: SeparationManager | None = None
        self._auto_start = auto_start
        self._before_start = before_start
        self._request_current = request_current or (lambda: True)
        self._on_stale = on_stale
        self._source_current: Callable[[], bool] = lambda: True
        self._started = False
        self.worker: BackingWorker | None = None
        self.backing_path: Path | None = None
        self._outcome = ""
        self._closing = False
        self._pending_consent = False
        self._consent_callback = None
        self.setWindowTitle("Generate backing track")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        self.remove_vocals_choice = QRadioButton("Remove all vocals (dialogue and singing)")
        self.keep_singing_choice = QRadioButton("Keep singing; remove dialogue")
        self.remove_vocals_choice.setChecked(self.mode == REMOVE_ALL_VOCALS)
        self.keep_singing_choice.setChecked(self.mode == KEEP_SINGING)
        layout.addWidget(self.remove_vocals_choice)
        layout.addWidget(self.keep_singing_choice)
        self.license_warning = QLabel(
            "BandIt model: CC BY-NC 4.0 — non-commercial use only. "
            "See Help → About for attribution and license details."
        )
        self.license_warning.setWordWrap(True)
        self.keep_singing_choice.toggled.connect(self.license_warning.setVisible)
        self.license_warning.setVisible(self.mode == KEEP_SINGING)
        layout.addWidget(self.license_warning)
        if auto_start:
            self.remove_vocals_choice.setEnabled(False)
            self.keep_singing_choice.setEnabled(False)
        note = QLabel(
            "Separation may leave voices or remove some effects. Audio stays on this computer."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.progress_label = QLabel("Choose what to keep, then select Generate.")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        layout.addWidget(self.progress_bar)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.generate_button = QPushButton("Generate")
        self.generate_button.clicked.connect(lambda: self.start())
        self.generate_button.setVisible(not auto_start)
        buttons.addWidget(self.generate_button)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(lambda: self.start())
        buttons.addWidget(self.retry_button)
        self.close_button = QPushButton("Cancel")
        self.close_button.clicked.connect(self.cancel_generation)
        buttons.addWidget(self.close_button)
        if job_manager is not None:
            hide_button = QPushButton("Hide")
            hide_button.clicked.connect(self.hide)
            buttons.addWidget(hide_button)
        layout.addLayout(buttons)
        if auto_start:
            QTimer.singleShot(0, self.start)

    def request_is_current(self) -> bool:
        return not self._closing and self._source_current() and self._request_current()

    def _stale_request(self) -> None:
        if self._on_stale is not None:
            self._on_stale()
        self._failed(
            "This request no longer matches the open project. "
            "Use Close, then open Generate Backing Track again."
        )
        self.retry_button.setVisible(False)
        self.generate_button.setEnabled(False)
        self.close_button.setText("Close")

    def start(self, *, allow_download: bool = False) -> None:
        if self.worker is not None or self._closing or self._pending_consent:
            return
        if self._started:
            if not self.request_is_current():
                self._stale_request()
                return
        else:
            mode = (
                KEEP_SINGING if self.keep_singing_choice.isChecked() and not self._auto_start
                else REMOVE_ALL_VOCALS
            )
            if self._before_start is not None and not self._before_start(mode):
                self._stale_request()
                return
            self.mode = mode
            self._source_current = _current_dialog_request(self)
            self._started = True
            self.remove_vocals_choice.setEnabled(False)
            self.keep_singing_choice.setEnabled(False)
            self.generate_button.setVisible(False)
        if self.manager is None:
            try:
                self.manager = SeparationManager(self.data_root, mode=self.mode)
            except (OSError, RuntimeError, ValueError) as error:
                diagnostic_exception("backing_setup_failed", error)
                self._failed(str(error))
                report_processing(self, "backing", "failed", f"Backing could not start: {error}")
                self.retry_button.setVisible(True)
                self.close_button.setText("Close")
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
        if self.job_manager is not None:
            worker.configure_job(
                self.job_manager, self.project_id, "backing", "Generate backing track",
                resource_class="cpu", read_paths=(self.video,),
                resource_keys=("separation-inference", f"separation:{self.data_root.resolve()}"),
                source_snapshot=self.source_snapshot,
                priority=-10,
            )
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.download_required.connect(self._download_required)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        editor = self.parentWidget()
        register_job_detail(
            self, worker, retry=self.start,
            available=lambda: (
                self.worker is None and not self._pending_consent and not self._closing
                and self.request_is_current()
                and getattr(editor, "_backing_dialog", self) is self
            ),
        )

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
            f"{message}\n\nExisting backing is unchanged."
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
        elif not self.request_is_current():
            self._stale_request()
        elif self._outcome == "download":
            assert self.manager is not None
            size = self.manager.model_download_bytes / 1024**2
            self._pending_consent = True

            def consent(accepted: bool) -> None:
                self._pending_consent = False
                self._consent_callback = None
                diagnostic_event("backing_download_consent", accepted=accepted)
                if not self.request_is_current():
                    self._stale_request()
                    return
                if accepted and not self._closing:
                    self.start(allow_download=True)
                else:
                    self.progress_label.setText("Backing generation not started; download declined.")
                    report_processing(self, "backing", "cancelled", "Backing download declined; use Retry to resume.")
                    self.close_button.setText("Close")
                    self.retry_button.setVisible(True)
                    if self.job_manager is None:
                        super(BackingDialog, self).reject()

            coordinator = getattr(_workspace_for(self), "setup_consent", None)
            model_label = (
                "BandIt singing-preserving model"
                if self.mode == KEEP_SINGING else "Music-separation model"
            )
            restriction = (
                " — CC BY-NC 4.0, non-commercial use only" if self.mode == KEEP_SINGING else ""
            )
            if coordinator is not None:
                model = self.manager.manifest["model"]
                self._consent_callback = consent
                report_processing(self, "backing", "consent", "Waiting for backing-model download permission.")
                coordinator.request(
                    self.project_id,
                    {f"separation:{model['sha256']}": f"{model_label} (~{size:.0f} MiB){restriction}"},
                    consent, self.request_is_current,
                )
            else:
                show_message(
                    self, "question",
                    "Download local music-separation model?",
                    f"{model_label}{restriction}.\n\n"
                    f"Download approximately {size:.0f} MiB of checksum-verified model data? "
                    "The model is stored in your local application data and reused offline. "
                    "A missing or damaged model needs this download before generation can continue.\n\n"
                    "Audio stays on this computer. Canceling keeps your imported video and dialogue "
                    "work; you can generate backing later from Tools.",
                    consent, QMessageBox.StandardButton.Yes,
                )
        else:
            if self._outcome != "failed":
                self._failed("Backing generation stopped without returning a result.")
            self.retry_button.setVisible(True)
            self.close_button.setText("Close")

    def accept(self) -> None:
        if self.worker is None and self.backing_path is not None:
            super().accept()

    def reject(self) -> None:
        if self.job_manager is not None and self._started:
            self.hide()
            return
        self.cancel_generation()

    def cancel_generation(self) -> None:
        current = self._started and self.request_is_current()
        if self._started and not current and self._on_stale is not None:
            self._on_stale()
        self._closing = True
        if self._consent_callback is not None:
            coordinator = getattr(_workspace_for(self), "setup_consent", None)
            if coordinator is not None:
                coordinator.cancel_request(self._consent_callback)
        if current:
            report_processing(
                self, "backing", "cancelled", "Backing generation paused; existing audio kept.",
            )
        if self.worker is not None:
            self.worker.requestInterruption()
            self.close_button.setEnabled(False)
            self.progress_label.setText("Canceling backing generation; waiting for the worker...")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.job_manager is not None and self._started:
            self.hide()
            event.ignore()
        elif self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
