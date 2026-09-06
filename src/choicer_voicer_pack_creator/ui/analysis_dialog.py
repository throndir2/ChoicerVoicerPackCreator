from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisResult,
    AnalysisSuggestion,
    WhisperManager,
    analyze_video,
    detect_hardware,
)
from choicer_voicer_pack_creator.captions import SOURCE_HEAD_PADDING, SOURCE_TAIL_PADDING
from choicer_voicer_pack_creator.diagnostics import (
    AnalysisDiagnostics,
    analysis_log_path,
    diagnostic_event,
    diagnostic_exception,
    save_diagnostic_bundle,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import AnalysisDraftRow, AnalysisReview, SourceCaption
from choicer_voicer_pack_creator.ui.job_worker import JobWorker


def _workspace_for(dialog: QWidget):
    return getattr(dialog.parentWidget(), "workspace", None)


def _current_dialog_request(dialog: QWidget) -> Callable[[], bool]:
    editor = dialog.parentWidget()
    session = getattr(editor, "session", None)
    workspace = _workspace_for(dialog)
    if session is None or workspace is None:
        return lambda: True
    token = session.source_token()
    snapshot = getattr(dialog, "source_snapshot", None) or {}
    source_revision = snapshot.get("source_revision", session.source_revision)
    return lambda: (
        not workspace._closing and session.id not in workspace._closed_ids
        and session.id in workspace.editors and session.source_token() == token
        and session.source_revision == source_revision and not session.loading
    )


def register_job_detail(
    dialog: QDialog, worker: JobWorker, *, retry: Callable[[], None] | None = None,
    available: Callable[[], bool] | None = None,
) -> None:
    workspace = getattr(dialog.parentWidget(), "workspace", None)
    tasks = getattr(workspace, "tasks_window", None)
    if tasks is not None and worker.job_handle is not None:
        tasks.register_detail(worker.job_handle.id, dialog)
        if retry is not None and available is not None and hasattr(tasks, "register_retry"):
            current = _current_dialog_request(dialog)
            job_id = worker.job_handle.id
            tasks.register_retry(job_id, retry, available=lambda: current() and available())
            dialog.destroyed.connect(lambda: tasks.unregister_retry(job_id))


def _job_manager_for(parent: QWidget):
    return getattr(parent, "job_manager", None) or getattr(
        getattr(parent, "workspace", None), "job_manager", None,
    )


def show_message(
    parent: QWidget, kind: str, title: str, text: str,
    callback: Callable[[bool], None] | None = None,
    default: QMessageBox.StandardButton = QMessageBox.StandardButton.Cancel,
) -> None:
    """Use callback-driven, nonmodal messages for workspace-owned surfaces."""
    buttons = (
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        if callback else QMessageBox.StandardButton.Ok
    )
    if _job_manager_for(parent) is None:
        method = getattr(QMessageBox, kind)
        if callback:
            callback(method(parent, title, text, buttons, default) == QMessageBox.StandardButton.Yes)
        else:
            method(parent, title, text)
        return
    icons = {
        "question": QMessageBox.Icon.Question,
        "warning": QMessageBox.Icon.Warning,
        "critical": QMessageBox.Icon.Critical,
        "information": QMessageBox.Icon.Information,
    }
    box = QMessageBox(icons[kind], title, text, buttons, parent)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    box.setDefaultButton(default if callback else QMessageBox.StandardButton.Ok)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if callback:
        box.finished.connect(lambda answer: callback(answer == QMessageBox.StandardButton.Yes))
    box.show()


def open_diagnostic_logs(parent: QWidget, data_root: Path) -> None:
    folder = analysis_log_path(data_root).parent
    diagnostic_event("diagnostic_folder_requested", folder=folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        diagnostic_exception("diagnostic_folder_failed", error)
        show_message(parent, "warning", "Could not open diagnostic logs", str(error))
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
        diagnostic_event("diagnostic_folder_failed", reason="desktop_open_failed")
        show_message(
            parent, "warning", "Could not open diagnostic logs", f"Open this folder manually:\n{folder}"
        )


def save_diagnostic_logs(parent: QWidget, data_root: Path) -> None:
    filename = f"Choicer-Voicer-Diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save Diagnostic Bundle (review logs before sharing)",
        str(Path.home() / filename), "Diagnostic ZIP (*.zip)",
    )
    if not path:
        return
    destination = Path(path)

    def operation(_context=None) -> Path:
        try:
            return save_diagnostic_bundle(data_root, destination)
        except (OSError, ValueError) as error:
            diagnostic_exception("diagnostic_bundle_failed", error)
            raise

    def failed(message: str) -> None:
        show_message(parent, "warning", "Could not save diagnostic bundle", message)

    def completed(_result=None) -> None:
        show_message(
            parent, "information", "Diagnostic bundle saved",
            f"Saved to:\n{destination}\n\nSend this ZIP with a description of what went wrong. "
            "It contains recent runs, local file paths and technical errors, but no media, "
            "project files or normal transcript output. Review it before sharing. "
            "Nothing has been uploaded.",
        )

    def save() -> None:
        manager = _job_manager_for(parent)
        if manager is None:
            try:
                operation()
            except (OSError, ValueError) as error:
                failed(str(error))
                return
            completed()
            return
        project_id = getattr(parent, "project_id", None) or getattr(
            getattr(parent, "session", None), "id", None,
        )
        try:
            job = manager.submit(
                project_id, "diagnostics", "Save diagnostic bundle", operation,
                resource_class="io", read_paths=(analysis_log_path(data_root).parent,),
                write_paths=(destination,),
            )
        except (RuntimeError, ValueError) as error:
            failed(str(error))
            return
        job.completed.connect(completed)
        job.failed.connect(failed)

    if destination.suffix.lower() != ".zip":
        destination = destination.with_name(destination.name + ".zip")
        if destination.exists():
            show_message(
                parent, "question", "Replace diagnostic bundle?", f"Replace {destination}?",
                lambda accepted: save() if accepted else None,
            )
            return
    save()


class DraftDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.editor: QWidget | None = None

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget | None:
        self.editor = super().createEditor(parent, option, index)
        return self.editor

    def destroyEditor(self, editor: QWidget, index: QModelIndex) -> None:
        if self.editor is editor:
            self.editor = None
        super().destroyEditor(editor, index)

    def commit_pending(self) -> None:
        if self.editor is not None:
            editor = self.editor
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)


class AnalysisWorker(JobWorker):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()

    def __init__(
        self,
        media: MediaTools,
        video: Path,
        duration: float,
        data_root: Path,
        sensitivity: str,
        use_whisper: bool,
        model_key: str,
        language: str,
        *,
        source_captions: list[SourceCaption] | None = None,
        pause_threshold: float = 0.4,
    ) -> None:
        super().__init__()
        self.media = media
        self.video = video
        self.duration = duration
        self.data_root = data_root
        self.sensitivity = sensitivity
        self.use_whisper = use_whisper
        self.model_key = model_key
        self.language = language
        self.source_captions = source_captions
        self.pause_threshold = pause_threshold
        self.worker_id = uuid.uuid4().hex[:12]

    def run(self) -> None:
        try:
            with AnalysisDiagnostics(self.data_root) as diagnostics:
                diagnostic_event("analysis_worker_running", worker_id=self.worker_id)
                def report(message: str, fraction: float | None) -> None:
                    diagnostics.progress(message, fraction)
                    value = -1 if fraction is None else max(0, min(1000, round(fraction * 1000)))
                    self.progress.emit(message, value)

                result = analyze_video(
                    self.media,
                    self.video,
                    self.duration,
                    self.data_root,
                    sensitivity=self.sensitivity,
                    use_whisper=self.use_whisper,
                    model_key=self.model_key,
                    language=self.language,
                    progress=report,
                    cancelled=self.isInterruptionRequested,
                    source_captions=self.source_captions,
                    pause_threshold=self.pause_threshold,
                )
                if self.isInterruptionRequested():
                    raise AnalysisCancelled("Video analysis was canceled")
            self.completed.emit(result)
        except AnalysisCancelled:
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception(
                "analysis_worker_failed", error, worker_id=self.worker_id,
            )
            self.failed.emit(str(error))


class AnalysisDialog(QDialog):
    suggestions_accepted = Signal(object)
    preview_requested = Signal(float, float)
    review_changed = Signal(object)

    def __init__(
        self,
        media: MediaTools,
        video: Path,
        duration: float,
        data_root: Path,
        existing_segments: int,
        parent: QWidget | None = None,
        *,
        initial_scan: bool = False,
        source_captions: list[SourceCaption] | None = None,
        caption_language: str = "",
        auto_start: bool = False,
        youtube_import: bool = False,
        review: AnalysisReview | None = None,
        job_manager=None,
        project_id: str | None = None,
        source_snapshot=None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.video = video.resolve()
        self.duration = duration
        self.data_root = data_root.resolve()
        self.log_path = analysis_log_path(self.data_root)
        self.existing_segments = existing_segments
        self.worker: AnalysisWorker | None = None
        self.refinement_worker: AnalysisWorker | None = None
        self.job_manager = job_manager
        self.project_id = project_id
        self.source_snapshot = source_snapshot
        if job_manager is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._draft_revisions = {False: 0, True: 0}
        self._run_revisions: dict[bool, int] = {}
        self._pending_results: dict[bool, AnalysisResult] = {}
        self._pending_scan = False
        self._pending_refine = False
        self._scan_canceled = False
        self._whisper_after_refinement = False
        self._close_after_cancel = False
        self._accept_after_cancel = False
        self._accepted_suggestions: list[AnalysisSuggestion] = []
        self.source_captions = list(source_captions or [])
        self.source_choice = youtube_import or bool(self.source_captions) or bool(
            review and review.refined_rows
        )
        self.local_source = review.local_source if review else "Whisper"
        self.local_model_name = review.local_model_name if review else ""
        self.local_detected_language = review.local_detected_language if review else ""
        self.analysis_result: AnalysisResult | None = None
        self.hardware = detect_hardware()
        diagnostic_event(
            "analysis_dialog_opened", auto_start=auto_start, initial_scan=initial_scan,
            youtube_import=youtube_import, video=self.video,
            duration_seconds=duration, caption_count=len(self.source_captions),
            restored_draft=review is not None,
        )

        self.setWindowTitle(
            "Initial Video Analysis" if initial_scan else "Analyze Video & Suggest Segments"
        )
        self.resize(1300 if self.source_choice else 1050, 720)
        self.setMinimumSize(900 if self.source_choice else 760, 660 if self.source_choice else 520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            (
                "Choose YouTube or Whisper. YouTube rows appear only after processing. "
                "Each draft keeps its own text and timings. "
                "Click the Use button below either draft to add only its checked rows. "
                "Closing saves all drafts without adding segments. "
                if self.source_choice else
                "Create editable starting points from local audio. Activity scanning is deterministic. "
            ) +
            f"Caption drafts include up to {SOURCE_HEAD_PADDING:.2f}s before and "
            f"{SOURCE_TAIL_PADDING:.2f}s after of source audio, limited by neighboring rows. "
            "Adjust In/Out when reviewing. "
            "Whisper can suggest text and timestamps, but it can be wrong—especially for names, "
            "stylized speech, music, and overlapping speakers. Token scores are not accuracy "
            "guarantees, and no speaker is assigned automatically."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        options = QFormLayout()
        self.sensitivity_combo = QComboBox()
        self.sensitivity_combo.addItem("Balanced", "balanced")
        self.sensitivity_combo.addItem("Sensitive — more possible lines/effects", "sensitive")
        self.sensitivity_combo.addItem("Conservative — fewer, louder regions", "conservative")
        if not self.source_choice:
            options.addRow("Activity scan", self.sensitivity_combo)

        self.whisper_check = QCheckBox("Run local Whisper transcription")
        self.whisper_check.setChecked(self.source_choice or self.local_source == "Whisper")
        self.whisper_check.setVisible(not self.source_choice)
        self.whisper_check.toggled.connect(self._whisper_toggled)
        if not self.source_choice:
            options.addRow("Transcription", self.whisper_check)

        self.model_combo = QComboBox()
        self.model_combo.addItem("Tiny multilingual · ~74 MiB · fastest", "tiny")
        self.model_combo.addItem("Base multilingual · ~141 MiB · better accuracy", "base")
        recommended_index = self.model_combo.findData(self.hardware.recommended_model)
        self.model_combo.setCurrentIndex(max(0, recommended_index))
        self.model_combo.currentIndexChanged.connect(
            lambda _index: self._whisper_toggled(self.whisper_check.isChecked())
        )
        self.model_combo.setToolTip(
            "Model for the next scan only. Changing it does not change or select the current draft."
        )
        options.addRow("Whisper model (next scan)", self.model_combo)

        self.language_combo = QComboBox()
        for label, code in (
            ("Auto-detect", "auto"),
            ("English", "en"),
            ("Japanese", "ja"),
            ("Chinese", "zh"),
            ("Korean", "ko"),
            ("Spanish", "es"),
            ("French", "fr"),
            ("German", "de"),
            ("Portuguese", "pt"),
            ("Russian", "ru"),
        ):
            self.language_combo.addItem(label, code)
        if caption_language:
            spoken_language = caption_language.split("-")[0]
            index = self.language_combo.findData(spoken_language)
            if index < 0:
                self.language_combo.addItem(spoken_language, spoken_language)
                index = self.language_combo.count() - 1
            self.language_combo.setCurrentIndex(index)
        self.language_combo.setToolTip(
            "Language for the next scan only. The current draft keeps its text and timings."
        )
        options.addRow("Spoken language (next scan)", self.language_combo)

        hardware_label = QLabel(self.hardware.description)
        hardware_label.setWordWrap(True)
        hardware_label.setObjectName("muted")
        options.addRow("Hardware", hardware_label)
        layout.addLayout(options)

        self.setup_label = QLabel()
        self.setup_label.setWordWrap(True)
        self.setup_label.setObjectName("muted")
        self.setup_label.setToolTip(f"Analysis component storage: {self.data_root}")
        layout.addWidget(self.setup_label)
        self._whisper_toggled(self.whisper_check.isChecked())

        log_row = QHBoxLayout()
        log_label = QLabel(f"Diagnostic log: {self.log_path}")
        log_label.setWordWrap(True)
        log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        log_label.setObjectName("muted")
        log_row.addWidget(log_label, 1)
        self.logs_button = QPushButton("Open Logs")
        self.logs_button.setAutoDefault(False)
        self.logs_button.clicked.connect(lambda: open_diagnostic_logs(self, self.data_root))
        log_row.addWidget(self.logs_button)
        self.save_logs_button = QPushButton("Save Diagnostic Bundle...")
        self.save_logs_button.setAutoDefault(False)
        self.save_logs_button.clicked.connect(lambda: save_diagnostic_logs(self, self.data_root))
        log_row.addWidget(self.save_logs_button)
        layout.addLayout(log_row)

        progress_row = QHBoxLayout()
        self.progress_label = QLabel("Ready to scan")
        self.progress_label.setWordWrap(True)
        self.progress_label.setObjectName("muted")
        progress_row.addWidget(self.progress_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setFormat("Current step: %p%")
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumWidth(200)
        self.progress_bar.setMaximumWidth(320)
        progress_row.addWidget(self.progress_bar)
        layout.addLayout(progress_row)

        self.refined_table = self._create_table()
        self.refined_table.setMinimumHeight(140)
        self.local_table = self._create_table()
        if self.source_choice:
            self.refined_table.setColumnHidden(5, True)
            self.local_table.setColumnHidden(4, True)
        self.refined_radio = QRadioButton("Select YouTube transcript")
        self.local_radio = QRadioButton("Select Whisper transcript")
        self.source_group = QButtonGroup(self)
        for radio in (self.refined_radio, self.local_radio):
            radio.setToolTip(
                "Select this draft for playback and the Enter-key Use action. "
                "Each draft also has its own Use button below its rows. This does not run analysis."
            )
            self.source_group.addButton(radio)
        selected = review.selected_source if review else (
            "refined" if self.source_captions else "local"
        )
        {
            "refined": self.refined_radio,
            "local": self.local_radio,
        }[selected].setChecked(True)
        self.refined_status = QLabel(
            "YouTube captions are waiting for refinement; no unprocessed rows are shown. "
            "Uses local audio pauses with no model download. Music can hide pauses."
            if self.source_captions else
            "No original YouTube caption evidence is available. Reimport the video to retrieve it."
        )
        self.local_status = QLabel("Whisper has not run yet.")
        self.local_draft_label = QLabel()
        self.local_draft_label.setWordWrap(True)
        self.refined_panel = QGroupBox("YouTube", self)
        self.local_panel = QGroupBox(
            "Whisper Transcript" if self.local_source == "Whisper" else "Detected Audio Ranges",
            self,
        )
        self.pause_spin = QDoubleSpinBox()
        self.pause_spin.setRange(0.2, 1.0)
        self.pause_spin.setSingleStep(0.05)
        self.pause_spin.setDecimals(2)
        self.pause_spin.setSuffix(" s")
        self.pause_spin.setValue(review.pause_threshold if review else 0.4)
        self.pause_spin.setToolTip(
            "Minimum audio pause for a suggested break at a recorded text-fragment boundary. "
            "Changing this setting does not alter a draft until you refine again."
        )
        self.refine_button = QPushButton("Refine YouTube")
        self.refine_button.setAutoDefault(False)
        self.refine_button.clicked.connect(self.start_refinement)
        self.refine_button.setToolTip(
            "Create a separate draft from original imported captions, not your edited drafts. "
            "Uses local audio only, independently of Whisper."
        )
        if self.source_choice:
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setObjectName("transcriptSplitter")
            splitter.setHandleWidth(1)
            for panel, radio, status, table in (
                (self.refined_panel, self.refined_radio, self.refined_status, self.refined_table),
                (self.local_panel, self.local_radio, self.local_status, self.local_table),
            ):
                panel_layout = QVBoxLayout(panel)
                panel_layout.addWidget(radio)
                status.setWordWrap(True)
                panel_layout.addWidget(status)
                if panel is self.local_panel:
                    panel_layout.addWidget(self.local_draft_label)
                if panel is self.refined_panel:
                    refine_options = QHBoxLayout()
                    refine_options.addWidget(QLabel("Minimum pause"))
                    refine_options.addWidget(self.pause_spin)
                    refine_options.addWidget(self.refine_button)
                    panel_layout.addLayout(refine_options)
                panel_layout.addWidget(table)
            splitter.addWidget(self.refined_panel)
            splitter.addWidget(self.local_panel)
            splitter.setChildrenCollapsible(False)
            layout.addWidget(splitter, 1)
        else:
            self.refined_table.hide()
            self.refined_panel.hide()
            self.pause_spin.hide()
            self.refine_button.hide()
            local_layout = QVBoxLayout(self.local_panel)
            self.local_status.setWordWrap(True)
            local_layout.addWidget(self.local_status)
            local_layout.addWidget(self.local_draft_label)
            local_layout.addWidget(self.local_table)
            layout.addWidget(self.local_panel, 1)

        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        controls.button(QDialogButtonBox.StandardButton.Close).setText("Keep Drafts && Close")
        self.scan_button = QPushButton(
            "Run Whisper" if self.source_choice else "Scan Video"
        )
        self.scan_button.setAutoDefault(False)
        self.scan_button.clicked.connect(self.start_scan)
        controls.addButton(self.scan_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_button = QPushButton("Cancel Scan")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        controls.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.preview_button = QPushButton("Play Selected Line")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview_current_row)
        controls.addButton(self.preview_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.refined_add_button = QPushButton("Use YouTube Transcript", self.refined_panel)
        self.local_add_button = QPushButton("Use Whisper Transcript", self.local_panel)
        for button, radio, panel in (
            (self.refined_add_button, self.refined_radio, self.refined_panel),
            (self.local_add_button, self.local_radio, self.local_panel),
        ):
            button.setObjectName("primary")
            button.setAutoDefault(False)
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, radio=radio: radio.setChecked(True))
            button.clicked.connect(self.accept_suggestions)
            if self.source_choice:
                panel.layout().addWidget(button)
        if not self.source_choice:
            controls.addButton(self.local_add_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_refined_result_button = QPushButton("Replace Draft with New YouTube Result")
        self.apply_local_result_button = QPushButton("Replace Draft with New Local Result")
        for button, refine, panel in (
            (self.apply_refined_result_button, True, self.refined_panel),
            (self.apply_local_result_button, False, self.local_panel),
        ):
            button.setAutoDefault(False)
            button.hide()
            button.clicked.connect(
                lambda _checked=False, refine=refine: self.apply_new_result(refine=refine)
            )
            if panel.layout() is not None:
                panel.layout().addWidget(button)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        if review:
            self._populate_rows(self.local_table, review.local_rows)
            self._populate_rows(self.refined_table, review.refined_rows)
            self.local_status.setText(f"Saved {self.local_source} draft: {len(review.local_rows)} rows.")
            if review.refined_rows:
                self.refined_status.setText(
                    f"Saved YouTube draft: {len(review.refined_rows)} rows. "
                    "Review Source notes for timing limitations."
                )
        if not self.source_captions and not self.refined_table.rowCount():
            self.local_radio.setChecked(True)
        self.source_group.buttonToggled.connect(self._source_changed)
        self.pause_spin.valueChanged.connect(lambda _value: self.review_changed.emit(self.review_state()))
        for table in (self.refined_table, self.local_table):
            table.itemChanged.connect(self._draft_edited)
            table.itemSelectionChanged.connect(self._update_selection_controls)
            table.cellClicked.connect(
                lambda _row, _column, table=table: self._select_table(table)
            )
            table.cellActivated.connect(
                lambda _row, _column, table=table: self._select_table(table)
            )
        self._update_selection_controls()
        self._update_scan_button()
        if review:
            self.progress_label.setText("Saved drafts restored; choose a source or regenerate a draft.")
        elif self.source_choice:
            self.progress_label.setText("Process YouTube captions or run the separate Whisper transcript.")
        needs_refinement = bool(self.source_captions) and not (review and review.refined_rows)
        if needs_refinement and (auto_start or review is not None):
            self.progress_label.setText("Refining YouTube captions before showing their draft.")
            if auto_start and review is None:
                QTimer.singleShot(0, self._start_automatic_refinement)
            else:
                QTimer.singleShot(0, self._start_pending_refinement)
        elif auto_start:
            diagnostic_event("analysis_auto_start_scheduled")
            QTimer.singleShot(0, self.start_scan)

    @property
    def add_button(self) -> QPushButton:
        return (
            self.refined_add_button if self.selected_source == "refined" else self.local_add_button
        )

    @property
    def table(self) -> QTableWidget:
        return {
            "refined": self.refined_table,
            "local": self.local_table,
        }[self.selected_source]

    @property
    def selected_source(self) -> str:
        if self.source_choice and self.refined_radio.isChecked():
            return "refined"
        return "local"

    def _create_table(self) -> QTableWidget:
        table = QTableWidget(0, 6, self)
        table.setItemDelegate(DraftDelegate(table))
        table.setHorizontalHeaderLabels(
            ["Use", "In", "Out", "Transcript", "Source", "Token score"]
        )
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        for column in (0, 1, 2, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(4, 120)
        table.cellDoubleClicked.connect(
            lambda row, _column, table=table: self.preview_row(row, table)
        )
        return table

    def review_state(self) -> AnalysisReview:
        return AnalysisReview(
            self._draft_rows(self.local_table),
            self.selected_source, self.local_source,
            self._draft_rows(self.refined_table), self.pause_spin.value(),
            self.local_model_name, self.local_detected_language,
        )

    @staticmethod
    def _draft_rows(table: QTableWidget) -> list[AnalysisDraftRow]:
        return [
            AnalysisDraftRow(
                table.item(row, 1).text(), table.item(row, 2).text(),
                table.item(row, 3).text(), table.item(row, 4).text(),
                table.item(row, 5).data(Qt.ItemDataRole.UserRole),
                table.item(row, 0).checkState() == Qt.CheckState.Checked,
            )
            for row in range(table.rowCount())
        ]

    def _draft_edited(self, item: QTableWidgetItem) -> None:
        self._draft_revisions[item.tableWidget() is self.refined_table] += 1
        if item.column() == 3 and item.toolTip() != item.text():
            with QSignalBlocker(item.tableWidget()):
                item.setToolTip(item.text())
        self._update_selection_controls()
        self.review_changed.emit(self.review_state())

    def _select_table(self, table: QTableWidget) -> None:
        if not self._close_after_cancel and table.isEnabled() and table.rowCount():
            {
                self.refined_table: self.refined_radio,
                self.local_table: self.local_radio,
            }[table].setChecked(True)

    def _source_changed(self, _button: QWidget, checked: bool) -> None:
        if checked:
            self._update_selection_controls()
            self.review_changed.emit(self.review_state())

    def _update_selection_controls(self) -> None:
        sources = (
            (self.refined_radio, self.refined_table),
            (self.local_radio, self.local_table),
        )
        for radio, table in sources:
            radio.setEnabled(bool(table.rowCount()) and not self._close_after_cancel)
        if not self.table.rowCount():
            for radio, table in sources:
                if table.rowCount():
                    radio.setChecked(True)
                    break
        self.local_radio.setText(
            "Select Whisper transcript" if self.local_source == "Whisper" else "Select detected ranges"
        )
        if self.local_table.rowCount():
            details = ""
            if self.local_source == "Whisper":
                details = f" ({self.local_model_name or 'model not recorded'}"
                if self.local_detected_language:
                    details += f"; detected {self.local_detected_language}"
                details += ")"
            self.local_draft_label.setText(
                f"Current draft: {self.local_table.rowCount()} {self.local_source} rows{details}."
            )
        else:
            self.local_draft_label.setText("No current local draft is available.")
        usable = bool(self.table.rowCount()) and self.table.isEnabled()
        for button, table in (
            (self.refined_add_button, self.refined_table),
            (self.local_add_button, self.local_table),
        ):
            checked = any(
                table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(table.rowCount())
            )
            button.setEnabled(table.isEnabled() and checked and not self._close_after_cancel)
            button.setDefault(button is self.add_button)
            button.setToolTip(
                "Add only this draft's checked rows using its own text and timings, "
                "without rerunning analysis or importing the other draft."
            )
        self.preview_button.setEnabled(
            usable and self.table.currentRow() >= 0 and not self._close_after_cancel
        )
        source = {
            "refined": "YouTube", "local": self.local_source,
        }[self.selected_source]
        self.local_add_button.setText(
            "Use Whisper Transcript" if self.local_source == "Whisper" else "Use Detected Ranges"
        )
        if source in {"YouTube", "Whisper"}:
            self.preview_button.setText(f"Play Selected {source} Line")
        else:
            self.preview_button.setText("Play Selected Range")
        self.preview_button.setToolTip(
            "Play the selected line's In/Out range in the source video. Select a line first."
        )

    def _update_scan_button(self) -> None:
        whisper = self.source_choice or self.whisper_check.isChecked()
        if self.worker is not None:
            self.scan_button.setText(
                "Refining YouTube..." if self.worker.source_captions is not None else
                "Whisper Running..." if self.worker.use_whisper else "Scanning Audio..."
            )
            self.scan_button.setEnabled(False)
        else:
            rerun = bool(self.local_table.rowCount())
            self.scan_button.setText(
                ("Rerun Whisper..." if rerun else "Run Whisper")
                if whisper else ("Rescan Audio..." if rerun else "Scan Audio")
            )
            self.scan_button.setEnabled(not self._close_after_cancel)
        self.refine_button.setText(
            "Refine YouTube Again..." if self.refined_table.rowCount() else "Refine YouTube"
        )
        self.refine_button.setEnabled(
            bool(self.source_captions)
            and (self.refinement_worker if self.job_manager is not None else self.worker) is None
            and not self._close_after_cancel and not self._pending_refine
        )
        if self.refinement_worker is not None:
            self.refine_button.setText("Refining YouTube...")
        self.pause_spin.setEnabled(
            (self.refinement_worker if self.job_manager is not None else self.worker) is None
            and not self._close_after_cancel
        )
        if self._pending_scan:
            self.scan_button.setEnabled(False)
        self.scan_button.setToolTip(
            "Generate a new local draft. To import an existing result, use its highlighted Use button."
        )

    def _whisper_toggled(self, checked: bool) -> None:
        self.model_combo.setEnabled(checked)
        self.language_combo.setEnabled(checked)
        if hasattr(self, "scan_button"):
            self._update_scan_button()
        if not checked:
            self.setup_label.setText(
                "No model download. Suggestions will contain activity ranges with blank captions."
            )
            return
        model_key = str(self.model_combo.currentData())
        size = 141 if model_key == "base" else 74
        try:
            manager = WhisperManager(self.data_root)
            installed = manager.cli_path.is_file() and manager.model_path(model_key).is_file()
        except Exception as error:
            diagnostic_exception("whisper_installation_status_failed", error)
            installed = False
        if installed:
            self.setup_label.setText(
                "The selected CPU runtime and model are already installed. Their checksums are "
                "verified again before transcription. Audio and transcripts stay local."
            )
            return
        self.setup_label.setText(
            f"First use downloads a checksum-verified ~8 MiB CPU runtime and ~{size} MiB model. "
            "They remain in per-user application data for later scans. Audio and transcripts stay local."
        )

    def start_scan(self) -> None:
        diagnostic_event(
            "analysis_scan_requested", already_running=self.worker is not None,
            closing=self._close_after_cancel,
        )
        if self.worker is not None or self._close_after_cancel or self._pending_scan:
            return
        self._commit_draft_editors()
        self._pending_scan = True
        if self.local_table.rowCount():
            show_message(
                self, "question",
                "Replace local analysis draft?",
                "A successful scan will replace the local draft and its edits. "
                "The YouTube draft will not change. "
                "Edits made while the scan runs are kept until you explicitly apply its new result. "
                "A failed or canceled scan keeps all drafts. "
                "Continue?" if self.source_choice else
                "A successful scan will replace the current draft and its edits. "
                "Edits made while scanning are kept until you apply the new result. "
                "A failed or canceled scan keeps the draft. Continue?",
                self._confirm_scan,
            )
            self._update_scan_button()
            return
        self._confirm_scan(True)

    def _confirm_scan(self, accepted: bool) -> None:
        if not accepted or self._close_after_cancel:
            self._pending_scan = False
            self.progress_label.setText(f"Scan not started. {self._recovery_hint()}")
            self._update_scan_button()
            return
        use_whisper = self.source_choice or self.whisper_check.isChecked()
        model_key = str(self.model_combo.currentData())
        language = str(self.language_combo.currentData())

        def start(accepted: bool) -> None:
            self._pending_scan = False
            self._update_scan_button()
            if accepted and not self._close_after_cancel:
                self._start_worker(use_whisper=use_whisper, model_key=model_key, language=language)
            else:
                self.progress_label.setText(f"Whisper not started. {self._recovery_hint()}")

        if use_whisper:
            try:
                manager = WhisperManager(self.data_root)
            except Exception as error:
                diagnostic_exception("whisper_setup_unavailable", error)
                hint = self._recovery_hint()
                self.progress_label.setText(f"Whisper not started. {hint}")
                self._pending_scan = False
                self._update_scan_button()
                show_message(
                    self, "critical", "Whisper setup is unavailable", f"{error}\n\n{hint}"
                )
                return
            model_missing = not manager.model_path(model_key).is_file()
            runtime_missing = not manager.cli_path.is_file()
            diagnostic_event(
                "whisper_setup_checked", model=model_key, model_missing=model_missing,
                runtime_missing=runtime_missing,
            )
            if model_missing or runtime_missing:
                download_mib = manager.model_download_bytes(model_key) / 1024**2
                diagnostic_event("whisper_download_prompt_shown", download_mib=download_mib + 8)
                def consent(accepted: bool) -> None:
                    diagnostic_event("whisper_download_consent", accepted=accepted)
                    start(accepted)

                coordinator = getattr(_workspace_for(self), "setup_consent", None)
                if coordinator is not None:
                    components = {}
                    if runtime_missing:
                        components[f"whisper-runtime:{manager.runtime['archive_sha256']}"] = (
                            f"Whisper CPU runtime {manager.runtime['version']} (~8 MiB)"
                        )
                    if model_missing:
                        components[f"whisper-model:{manager.models[model_key]['sha256']}"] = (
                            f"Whisper {model_key} model (~{download_mib:.0f} MiB)"
                        )
                    coordinator.request(
                        self.project_id, components, consent, _current_dialog_request(self),
                    )
                else:
                    show_message(
                        self, "question",
                        "Download local transcription components?",
                        f"This one-time setup will download approximately {download_mib + 8:.0f} MiB "
                        "of checksum-verified whisper.cpp runtime/model files. They are stored only "
                        "for your Windows user and can be deleted later from local application data. "
                        "Continue?",
                        consent, QMessageBox.StandardButton.Yes,
                    )
                return
        start(True)

    def _start_automatic_refinement(self) -> None:
        if self.job_manager is not None:
            self._start_pending_refinement()
            self.start_scan()
            return
        self._start_pending_refinement(start_whisper=True)

    def _start_pending_refinement(self, *, start_whisper: bool = False) -> None:
        running = self.refinement_worker if self.job_manager is not None else self.worker
        if running is not None or self._close_after_cancel or self._scan_canceled:
            diagnostic_event(
                "automatic_refinement_skipped", already_running=self.worker is not None,
                closing=self._close_after_cancel, canceled=self._scan_canceled,
            )
            return
        self._whisper_after_refinement = start_whisper
        self.start_refinement()

    def start_refinement(self) -> None:
        running = self.refinement_worker if self.job_manager is not None else self.worker
        if running is not None or self._close_after_cancel or self._pending_refine:
            return
        if not self.source_captions:
            show_message(
                self, "information", "No imported captions",
                "No original YouTube caption evidence is available."
            )
            return
        self._commit_draft_editors()
        def start(accepted: bool) -> None:
            self._pending_refine = False
            self._update_scan_button()
            if accepted and not self._close_after_cancel:
                self._start_worker(use_whisper=False, refine=True)
            else:
                self._whisper_after_refinement = False

        if self.refined_table.rowCount():
            self._pending_refine = True
            show_message(
                self, "question",
                "Replace YouTube draft?",
                "A successful refinement will replace only the YouTube draft and its edits, "
                "using the original imported captions. Your Whisper draft will not change. "
                "Edits made while refining are kept until you apply the new result. "
                "A failed or canceled scan keeps all drafts. Continue?",
                start,
            )
            self._update_scan_button()
            return
        start(True)

    def _start_worker(
        self, *, use_whisper: bool, refine: bool = False,
        model_key: str | None = None, language: str | None = None,
    ) -> None:
        self._commit_draft_editors()
        self._run_revisions[refine] = self._draft_revisions[refine]
        self._scan_canceled = False
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        target_status = self.refined_status if refine else self.local_status
        target_status.setText(
            "Measuring audio pauses for YouTube captions..." if refine else
            "Whisper is running..." if use_whisper else "Scanning audio activity..."
        )
        self._update_selection_controls()
        if not refine or self.job_manager is None:
            self.whisper_check.setEnabled(False)
            self.model_combo.setEnabled(False)
            self.language_combo.setEnabled(False)
            self.sensitivity_combo.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Starting local analysis…")
        worker = AnalysisWorker(
            self.media,
            self.video,
            self.duration,
            self.data_root,
            str(self.sensitivity_combo.currentData()),
            use_whisper,
            model_key or str(self.model_combo.currentData()),
            language or str(self.language_combo.currentData()),
            source_captions=list(self.source_captions) if refine else None,
            pause_threshold=self.pause_spin.value(),
        )
        if refine and self.job_manager is not None:
            self.refinement_worker = worker
        else:
            self.worker = worker
        if self.job_manager is not None:
            worker.configure_job(
                self.job_manager, self.project_id,
                "refinement" if refine else "analysis",
                "Refine YouTube captions" if refine else
                "Whisper transcription" if use_whisper else "Scan audio",
                resource_class="cpu", read_paths=(self.video,),
                resource_keys=("whisper-inference",) if use_whisper else (),
                source_snapshot=self.source_snapshot,
            )
        diagnostic_event(
            "analysis_worker_start_requested", worker_id=worker.worker_id,
            use_whisper=use_whisper, refine=refine, model=worker.model_key, language=worker.language,
        )
        self._update_scan_button()
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        register_job_detail(
            self, worker, retry=self.start_refinement if refine else self.start_scan,
            available=lambda: (
                (self.refinement_worker if refine else self.worker) is None
                and not self._close_after_cancel
                and getattr(self.parentWidget(), "_analysis_dialog", self) is self
            ),
        )

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        if self._scan_canceled or self._close_after_cancel:
            return
        self.progress_label.setText(message)
        worker = self._callback_worker()
        if worker is not None:
            status = (
                self.refined_status if worker.source_captions is not None else self.local_status
            )
            status.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(value)

    @Slot(object)
    def _completed(self, value: object, *, apply: bool = False) -> None:
        diagnostic_event(
            "analysis_result_received", canceled=self._scan_canceled,
            closing=self._close_after_cancel, valid=isinstance(value, AnalysisResult),
        )
        # A result can already be queued when the user cancels or closes the review.
        worker = self._callback_worker()
        if not apply and (
            self._scan_canceled or self._close_after_cancel
            or (worker is not None and worker.isInterruptionRequested())
        ):
            self._canceled()
            return
        if not isinstance(value, AnalysisResult):
            self._failed("Analysis returned an unexpected result")
            return
        refine = value.refined_captions is not None
        self._commit_draft_editors()
        revision = None if apply else self._run_revisions.pop(refine, None)
        has_rows = bool(value.refined_captions) if refine else any(
            item.source == "Whisper" or not self.source_choice for item in value.suggestions
        )
        if has_rows and revision is not None and revision != self._draft_revisions[refine]:
            self._pending_results[refine] = value
            button = self.apply_refined_result_button if refine else self.apply_local_result_button
            button.show()
            status = self.refined_status if refine else self.local_status
            status.setText(
                "A new result is ready. Your edits made during processing are unchanged. "
                "Use the replacement button to review the new result instead."
            )
            self.progress_label.setText("New result saved separately; current draft edits kept.")
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(1000)
            self._set_idle()
            return
        if has_rows:
            self._pending_results.pop(refine, None)
            (self.apply_refined_result_button if refine else self.apply_local_result_button).hide()
            self._draft_revisions[refine] += 1
        if value.refined_captions is not None:
            if not value.refined_captions:
                self._empty_result(refine=True)
                return
            self._populate_rows(self.refined_table, [
                AnalysisDraftRow(f"{cue.start:.3f}", f"{cue.end:.3f}", cue.text, cue.source)
                for cue in value.refined_captions
            ])
            self.refined_status.setText(
                f"{len(value.refined_captions)} YouTube rows. "
                "Review Source notes for timing limitations. "
                "Music can hide pauses; speaker changes are not detected."
            )
            self.refined_radio.setEnabled(bool(self.refined_table.rowCount()))
            if self.refined_radio.isEnabled():
                self.refined_radio.setChecked(True)
                self.refined_table.selectRow(0)
            elif self.refined_radio.isChecked():
                self.local_radio.setChecked(True)
            self.progress_label.setText(
                "YouTube refinement complete; the local draft is unchanged."
            )
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(1000)
            self._set_idle()
            self.review_changed.emit(self.review_state())
            return
        suggestions = [
            item for item in value.suggestions if item.source == "Whisper"
        ] if self.source_choice else value.suggestions
        if not suggestions:
            self._empty_result()
            return
        self.analysis_result = value
        self.local_source = "Whisper" if value.model_name else "Audio activity"
        self.local_model_name = value.model_name or ""
        self.local_detected_language = value.detected_language or ""
        self.local_panel.setTitle(
            "Whisper Transcript" if value.model_name else "Detected Audio Ranges"
        )
        self._populate(suggestions)
        self.local_status.setText(
            f"{len(suggestions)} {self.local_source} rows with their own text and timings."
        )
        language = f" · detected {value.detected_language}" if value.detected_language else ""
        self.progress_label.setText(
            f"Review {len(suggestions)} local suggestion(s) · "
            f"{value.activity_regions} activity / {value.transcript_regions} transcript regions{language}"
        )
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self._set_idle()
        if self.local_table.rowCount():
            self.local_table.selectRow(0)
        self.review_changed.emit(self.review_state())

    def apply_new_result(self, *, refine: bool = False) -> None:
        value = self._pending_results.pop(refine, None)
        if value is None:
            return
        self._commit_draft_editors()
        self._completed(value, apply=True)

    def _callback_worker(self) -> AnalysisWorker | None:
        sender = self.sender()
        if isinstance(sender, AnalysisWorker):
            return sender
        return self.worker or self.refinement_worker

    def _recovery_hint(self, *, refine: bool = False) -> str:
        table = self.refined_table if refine else self.local_table
        source = "YouTube" if refine else self.local_source
        if table.rowCount():
            action = f"Use {source} Transcript" if source != "Audio activity" else "Use Detected Ranges"
            return (
                f"The current {source} draft and edits are unchanged. "
                f"Click '{action}' to use its checked rows without rerunning."
            )
        return (
            f"No {source} draft is available. Choose another available transcript, "
            "or retry the scan with suitable settings."
        )

    def _empty_result(self, *, refine: bool = False) -> None:
        self._whisper_after_refinement = False
        diagnostic_event("analysis_empty_result_retained_drafts", refine=refine)
        message = f"No new rows were found. {self._recovery_hint(refine=refine)}"
        (self.refined_status if refine else self.local_status).setText(message)
        self.progress_label.setText(message)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self._set_idle()

    def _populate(self, suggestions: list[AnalysisSuggestion]) -> None:
        self._populate_rows(self.local_table, [
            AnalysisDraftRow(
                f"{item.start:.3f}", f"{item.end:.3f}", item.caption,
                item.source, item.confidence,
            )
            for item in suggestions
        ])
        self._update_selection_controls()

    @staticmethod
    def _populate_rows(table: QTableWidget, rows: list[AnalysisDraftRow]) -> None:
        with QSignalBlocker(table):
            table.setRowCount(len(rows))
            for row, draft in enumerate(rows):
                use = QTableWidgetItem()
                use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                use.setCheckState(Qt.CheckState.Checked if draft.checked else Qt.CheckState.Unchecked)
                table.setItem(row, 0, use)
                for column, text in ((1, draft.start), (2, draft.end), (3, draft.caption)):
                    cell = QTableWidgetItem(text)
                    cell.setToolTip(text)
                    table.setItem(row, column, cell)
                evidence = QTableWidgetItem(draft.source)
                evidence.setToolTip(draft.source)
                evidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, 4, evidence)
                confidence = QTableWidgetItem(
                    f"{draft.confidence:.0%}" if draft.confidence is not None else "—"
                )
                confidence.setData(Qt.ItemDataRole.UserRole, draft.confidence)
                confidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, 5, confidence)

    def _set_idle(self) -> None:
        self._update_scan_button()
        running = self.worker is not None or self.refinement_worker is not None
        self.cancel_button.setEnabled(running and not self._scan_canceled)
        self.local_table.setEnabled(not self._close_after_cancel)
        self.refined_table.setEnabled(not self._close_after_cancel)
        self._update_selection_controls()
        self.whisper_check.setEnabled(self.worker is None)
        self.sensitivity_combo.setEnabled(self.worker is None)
        self._whisper_toggled(self.whisper_check.isChecked())
        if self.worker is not None:
            self.model_combo.setEnabled(False)
            self.language_combo.setEnabled(False)

    @Slot(str)
    def _failed(self, message: str) -> None:
        self._whisper_after_refinement = False
        diagnostic_event("analysis_failure_displayed", message=message)
        if self._scan_canceled or self._close_after_cancel:
            self._canceled()
            return
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        worker = self._callback_worker()
        refine = worker is not None and worker.source_captions is not None
        hint = self._recovery_hint(refine=refine)
        status = f"Analysis failed. {hint}"
        self.progress_label.setText(status)
        (self.refined_status if refine else self.local_status).setText(status)
        self._set_idle()
        show_message(
            self, "critical", "Video analysis failed",
            f"{message}\n\n{hint}\n\nDiagnostic log: {self.log_path}"
        )

    @Slot()
    def _canceled(self) -> None:
        self._whisper_after_refinement = False
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        worker = self._callback_worker()
        refine = worker is not None and worker.source_captions is not None
        status = f"Analysis canceled. {self._recovery_hint(refine=refine)}"
        self.progress_label.setText(status)
        (self.refined_status if refine else self.local_status).setText(status)
        self._set_idle()

    @Slot()
    def _worker_finished(self) -> None:
        diagnostic_event("analysis_worker_finished", closing=self._close_after_cancel)
        if self.sender() is self.worker:
            self.worker = None
        if self.sender() is self.refinement_worker:
            self.refinement_worker = None
        self._set_idle()
        if self._close_after_cancel and self.worker is None and self.refinement_worker is None:
            self._finish_review()
        elif self._whisper_after_refinement:
            self._whisper_after_refinement = False
            self.start_scan()

    def checked_suggestions(self) -> list[AnalysisSuggestion]:
        if not self.table.isEnabled():
            raise ValueError("Wait for this transcript to finish processing before using it.")
        selected: list[AnalysisSuggestion] = []
        for row in range(self.table.rowCount()):
            use = self.table.item(row, 0)
            if not use or use.checkState() != Qt.CheckState.Checked:
                continue
            try:
                start = float(self.table.item(row, 1).text())
                end = float(self.table.item(row, 2).text())
                if not math.isfinite(start) or not math.isfinite(end):
                    raise ValueError("Non-finite range")
            except (AttributeError, ValueError):
                raise ValueError(f"Suggestion {row + 1} has invalid In/Out values") from None
            start = max(0.0, min(self.duration, start))
            end = max(0.0, min(self.duration, end))
            if end - start < 0.05:
                raise ValueError(f"Suggestion {row + 1} must be at least 0.05 seconds long")
            caption = self.table.item(row, 3).text().strip()
            evidence = self.table.item(row, 4).text()
            confidence = self.table.item(row, 5).data(Qt.ItemDataRole.UserRole)
            selected.append(
                AnalysisSuggestion(
                    round(start, 3),
                    round(end, 3),
                    caption,
                    evidence,
                    confidence,
                )
            )
        return selected

    def preview_current_row(self) -> None:
        self._commit_draft_editors()
        row = self.table.currentRow()
        if row >= 0:
            self.preview_row(row)

    def preview_row(self, row: int, table: QTableWidget | None = None) -> None:
        table = self.table if table is None else table
        if not table.isEnabled():
            show_message(
                self, "information", "Transcript processing",
                "Wait for this transcript to finish processing."
            )
            return
        try:
            start = float(table.item(row, 1).text())
            end = float(table.item(row, 2).text())
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("Non-finite range")
        except (AttributeError, ValueError):
            show_message(self, "warning", "Invalid suggestion", "Fix this row's In/Out values first.")
            return
        start = max(0.0, min(self.duration, start))
        end = max(0.0, min(self.duration, end))
        if end - start < 0.05:
            show_message(self, "warning", "Invalid suggestion", "The preview range is too short.")
            return
        self.preview_requested.emit(start, end)

    def accept_suggestions(self) -> None:
        if self._close_after_cancel:
            return
        self._commit_draft_editors()
        try:
            suggestions = self.checked_suggestions()
        except ValueError as error:
            show_message(self, "warning", "Invalid suggestion", str(error))
            return
        if not suggestions:
            show_message(
                self, "information", "No suggestions selected", "Check at least one row first."
            )
            return
        def use(accepted: bool) -> None:
            if not accepted or self._close_after_cancel:
                return
            self._accepted_suggestions = suggestions
            if self.job_manager is not None:
                self.review_changed.emit(self.review_state())
                self.suggestions_accepted.emit(suggestions)
                super(AnalysisDialog, self).accept()
                return
            self._accept_after_cancel = True
            self._close_review()

        if self.existing_segments:
            show_message(
                self, "question",
                "Add alongside existing segments?",
                f"This project already has {self.existing_segments} segment(s). Add "
                f"{len(suggestions)} checked suggestion(s) without replacing anything?",
                use,
            )
            return
        use(True)

    def _close_review(self) -> None:
        self._commit_draft_editors()
        self.review_changed.emit(self.review_state())
        if self.job_manager is not None:
            self.hide()
            return
        self._close_after_cancel = True
        if self.worker is not None:
            self.refined_table.setEnabled(False)
            self.local_table.setEnabled(False)
            self._update_selection_controls()
            self.cancel_scan()
        else:
            self._finish_review()

    def _commit_draft_editors(self) -> None:
        for table in (self.refined_table, self.local_table):
            delegate = table.itemDelegate()
            if isinstance(delegate, DraftDelegate):
                delegate.commit_pending()

    def _finish_review(self) -> None:
        self.review_changed.emit(self.review_state())
        if self._accept_after_cancel:
            self.suggestions_accepted.emit(self._accepted_suggestions)
            super().accept()
        else:
            super().reject()

    def cancel_scan(self) -> None:
        self._whisper_after_refinement = False
        diagnostic_event("analysis_cancel_requested", running=self.worker is not None)
        workers = [worker for worker in (self.worker, self.refinement_worker) if worker is not None]
        if workers:
            self._scan_canceled = True
        for worker in workers:
            worker.requestInterruption()
            self.scan_button.setText("Canceling...")
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("Canceling analysis; current captions will be kept...")

    def reject(self) -> None:
        if not self._close_after_cancel:
            self._close_review()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.ignore()
        self.reject()
