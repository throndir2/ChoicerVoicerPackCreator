from __future__ import annotations

import math
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, QThread, QTimer, QUrl, Signal, Slot
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
    QTabWidget,
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
from choicer_voicer_pack_creator.diagnostics import (
    AnalysisDiagnostics,
    analysis_log_path,
    diagnostic_event,
    diagnostic_exception,
    save_diagnostic_bundle,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import AnalysisDraftRow, AnalysisReview, SourceCaption


def open_diagnostic_logs(parent: QWidget, data_root: Path) -> None:
    folder = analysis_log_path(data_root).parent
    diagnostic_event("diagnostic_folder_requested", folder=folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        diagnostic_exception("diagnostic_folder_failed", error)
        QMessageBox.warning(parent, "Could not open diagnostic logs", str(error))
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
        diagnostic_event("diagnostic_folder_failed", reason="desktop_open_failed")
        QMessageBox.warning(
            parent, "Could not open diagnostic logs", f"Open this folder manually:\n{folder}"
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
    if destination.suffix.lower() != ".zip":
        destination = destination.with_name(destination.name + ".zip")
        if destination.exists() and QMessageBox.question(
            parent, "Replace diagnostic bundle?", f"Replace {destination}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
    try:
        save_diagnostic_bundle(data_root, destination)
    except (OSError, ValueError) as error:
        diagnostic_exception("diagnostic_bundle_failed", error)
        QMessageBox.warning(parent, "Could not save diagnostic bundle", str(error))
        return
    QMessageBox.information(
        parent, "Diagnostic bundle saved",
        f"Saved to:\n{destination}\n\nSend this ZIP with a description of what went wrong. "
        "It contains recent runs, local file paths and technical errors, but no media, "
        "project files or normal transcript output. Review it before sharing. "
        "Nothing has been uploaded.",
    )


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


class AnalysisWorker(QThread):
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
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.video = video.resolve()
        self.duration = duration
        self.data_root = data_root.resolve()
        self.log_path = analysis_log_path(self.data_root)
        self.existing_segments = existing_segments
        self.worker: AnalysisWorker | None = None
        self._scan_canceled = False
        self._whisper_after_refinement = False
        self._close_after_cancel = False
        self._accept_after_cancel = False
        self._accepted_suggestions: list[AnalysisSuggestion] = []
        self.source_captions = list(source_captions or [])
        self.source_choice = youtube_import or bool(self.source_captions) or bool(
            review and (review.youtube_rows or review.refined_rows)
        )
        self.local_source = review.local_source if review else "Whisper"
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
                "Choose original YouTube, Refined YouTube, or Whisper. Each draft keeps its own "
                "text and timings; only checked rows from the selected source are added. "
                "Closing saves all drafts without adding segments. "
                if self.source_choice else
                "Create editable starting points from local audio. Activity scanning is deterministic. "
            ) +
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
        options.addRow("Whisper model", self.model_combo)

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
        options.addRow("Spoken language", self.language_combo)

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

        self.youtube_table = self._create_table()
        self.refined_table = self._create_table()
        self.refined_table.setMinimumHeight(140)
        self.local_table = self._create_table()
        if self.source_choice:
            self.youtube_table.setColumnHidden(5, True)
            self.refined_table.setColumnHidden(5, True)
            self.local_table.setColumnHidden(4, True)
        self.youtube_radio = QRadioButton("YouTube text + timings")
        self.refined_radio = QRadioButton("YouTube text + audio-pause boundaries")
        self.local_radio = QRadioButton("Whisper text + timings")
        self.source_group = QButtonGroup(self)
        self.source_group.addButton(self.youtube_radio)
        self.source_group.addButton(self.refined_radio)
        self.source_group.addButton(self.local_radio)
        selected = review.selected_source if review else (
            "youtube" if self.source_captions else "local"
        )
        {
            "youtube": self.youtube_radio,
            "refined": self.refined_radio,
            "local": self.local_radio,
        }[selected].setChecked(True)
        self.youtube_status = QLabel()
        self.refined_status = QLabel(
            "New YouTube imports are refined automatically using local audio pauses; no model download. "
            "Music can hide pauses, and speaker changes are not detected."
        )
        self.local_status = QLabel("Whisper has not run yet.")
        self.youtube_panel = QGroupBox("YouTube Captions", self)
        self.refined_panel = QGroupBox("Refined YouTube", self)
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
            "Uses local audio only. Wait for or cancel any running scan first."
        )
        self.youtube_tabs = QTabWidget()
        if self.source_choice:
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setObjectName("transcriptSplitter")
            splitter.setHandleWidth(1)
            for panel, radio, status, table in (
                (self.youtube_panel, self.youtube_radio, self.youtube_status, self.youtube_table),
                (self.refined_panel, self.refined_radio, self.refined_status, self.refined_table),
                (self.local_panel, self.local_radio, self.local_status, self.local_table),
            ):
                panel_layout = QVBoxLayout(panel)
                panel_layout.addWidget(radio)
                status.setWordWrap(True)
                panel_layout.addWidget(status)
                if panel is self.refined_panel:
                    refine_options = QHBoxLayout()
                    refine_options.addWidget(QLabel("Minimum pause"))
                    refine_options.addWidget(self.pause_spin)
                    refine_options.addWidget(self.refine_button)
                    panel_layout.addLayout(refine_options)
                panel_layout.addWidget(table)
            self.youtube_tabs.addTab(self.youtube_panel, "Original YouTube")
            self.youtube_tabs.addTab(self.refined_panel, "Refined YouTube")
            self.youtube_tabs.setCurrentIndex(1 if selected == "refined" else 0)
            self.youtube_tabs.currentChanged.connect(self._youtube_tab_changed)
            splitter.addWidget(self.youtube_tabs)
            splitter.addWidget(self.local_panel)
            splitter.setChildrenCollapsible(False)
            layout.addWidget(splitter, 1)
        else:
            self.youtube_table.hide()
            self.youtube_panel.hide()
            self.refined_table.hide()
            self.refined_panel.hide()
            self.youtube_tabs.hide()
            self.pause_spin.hide()
            self.refine_button.hide()
            local_layout = QVBoxLayout(self.local_panel)
            local_layout.addWidget(self.local_status)
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
        self.add_button = QPushButton("Add Checked Suggestions")
        self.add_button.setObjectName("primary")
        self.add_button.setDefault(True)
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.accept_suggestions)
        controls.addButton(self.add_button, QDialogButtonBox.ButtonRole.AcceptRole)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self._populate_rows(self.youtube_table, review.youtube_rows if review else [
            AnalysisDraftRow(f"{cue.start:.3f}", f"{cue.end:.3f}", cue.text, cue.source)
            for cue in self.source_captions
        ])
        if review:
            self._populate_rows(self.local_table, review.local_rows)
            self._populate_rows(self.refined_table, review.refined_rows)
            self.local_status.setText(f"Saved {self.local_source} draft: {len(review.local_rows)} rows.")
            if review.refined_rows:
                self.refined_status.setText(
                    f"Saved Refined YouTube draft: {len(review.refined_rows)} rows. "
                    "Review Source notes for limited timing or unchanged ranges."
                )
        self.youtube_status.setText(
            f"{self.youtube_table.rowCount()} YouTube caption rows."
            if self.youtube_table.rowCount() else "No YouTube captions are available for this video."
        )
        self.youtube_radio.setEnabled(bool(self.youtube_table.rowCount()))
        self.refined_radio.setEnabled(bool(self.refined_table.rowCount()))
        if (
            self.youtube_radio.isChecked() and not self.youtube_radio.isEnabled()
            or self.refined_radio.isChecked() and not self.refined_radio.isEnabled()
        ):
            self.local_radio.setChecked(True)
        self.source_group.buttonToggled.connect(self._source_changed)
        self.pause_spin.valueChanged.connect(lambda _value: self.review_changed.emit(self.review_state()))
        for table in (self.youtube_table, self.refined_table, self.local_table):
            table.itemChanged.connect(self._draft_edited)
            table.itemSelectionChanged.connect(self._update_selection_controls)
        self._update_selection_controls()
        self._update_scan_button()
        if review:
            self.progress_label.setText("Saved drafts restored; choose a source or regenerate a draft.")
        elif self.source_choice:
            self.progress_label.setText("Choose YouTube now or wait for the separate Whisper transcript.")
        if auto_start:
            diagnostic_event("analysis_auto_start_scheduled")
            if self.source_captions and review is None:
                self.progress_label.setText(
                    "Refining YouTube captions automatically before the separate Whisper transcript."
                )
                QTimer.singleShot(0, self._start_automatic_refinement)
            else:
                QTimer.singleShot(0, self.start_scan)

    @property
    def table(self) -> QTableWidget:
        return {
            "youtube": self.youtube_table,
            "refined": self.refined_table,
            "local": self.local_table,
        }[self.selected_source]

    @property
    def selected_source(self) -> str:
        if self.source_choice:
            if self.youtube_radio.isChecked():
                return "youtube"
            if self.refined_radio.isChecked():
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
            self._draft_rows(self.youtube_table), self._draft_rows(self.local_table),
            self.selected_source, self.local_source,
            self._draft_rows(self.refined_table), self.pause_spin.value(),
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
        if item.column() == 3 and item.toolTip() != item.text():
            with QSignalBlocker(item.tableWidget()):
                item.setToolTip(item.text())
        self.review_changed.emit(self.review_state())

    def _source_changed(self, _button: QWidget, checked: bool) -> None:
        if checked:
            if self.selected_source in {"youtube", "refined"}:
                self.youtube_tabs.setCurrentIndex(1 if self.selected_source == "refined" else 0)
            self._update_selection_controls()
            self.review_changed.emit(self.review_state())

    def _youtube_tab_changed(self, index: int) -> None:
        radio = self.refined_radio if index == 1 else self.youtube_radio
        if radio.isEnabled() and not self._close_after_cancel:
            radio.setChecked(True)

    def _update_selection_controls(self) -> None:
        usable = bool(self.table.rowCount()) and self.table.isEnabled()
        self.add_button.setEnabled(usable and not self._close_after_cancel)
        self.preview_button.setEnabled(
            usable and self.table.currentRow() >= 0 and not self._close_after_cancel
        )
        source = {
            "youtube": "YouTube", "refined": "Refined YouTube", "local": self.local_source,
        }[self.selected_source]
        if source in {"YouTube", "Refined YouTube", "Whisper"}:
            self.add_button.setText(f"Use {source} Transcript")
            self.preview_button.setText(f"Play Selected {source} Line")
        else:
            self.add_button.setText("Use Detected Ranges")
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
            rerun = self.analysis_result is not None or bool(self.local_table.rowCount())
            self.scan_button.setText(
                ("Rerun Whisper..." if rerun else "Run Whisper")
                if whisper else ("Rescan Audio..." if rerun else "Scan Audio")
            )
            self.scan_button.setEnabled(not self._close_after_cancel)
        self.refine_button.setText(
            "Refine YouTube Again..." if self.refined_table.rowCount() else "Refine YouTube"
        )
        self.refine_button.setEnabled(
            bool(self.source_captions) and self.worker is None and not self._close_after_cancel
        )
        self.pause_spin.setEnabled(self.worker is None and not self._close_after_cancel)
        self.scan_button.setToolTip(
            "Generate a new local draft. To import an existing result, use the highlighted transcript button."
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
        if self.worker is not None or self._close_after_cancel:
            return
        self._commit_draft_editors()
        if self.local_table.rowCount():
            answer = QMessageBox.question(
                self,
                "Replace local analysis draft?",
                "A successful scan will replace the local draft and its edits. "
                "The original and Refined YouTube drafts will not change. "
                "A failed or canceled scan keeps all drafts. "
                "Continue?" if self.source_choice else
                "A successful scan will replace the current draft and its edits. "
                "A failed or canceled scan keeps the draft. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                diagnostic_event("analysis_scan_declined", reason="keep_existing_draft")
                return
        use_whisper = self.source_choice or self.whisper_check.isChecked()
        model_key = str(self.model_combo.currentData())
        if use_whisper:
            try:
                manager = WhisperManager(self.data_root)
            except Exception as error:
                diagnostic_exception("whisper_setup_unavailable", error)
                QMessageBox.critical(self, "Whisper setup is unavailable", str(error))
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
                answer = QMessageBox.question(
                    self,
                    "Download local transcription components?",
                    f"This one-time setup will download approximately {download_mib + 8:.0f} MiB "
                    "of checksum-verified whisper.cpp runtime/model files. They are stored only "
                    "for your Windows user and can be deleted later from local application data. "
                    "Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                diagnostic_event(
                    "whisper_download_consent", accepted=answer == QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.progress_label.setText(
                        "Whisper not started. You can still edit and add available captions."
                    )
                    return
        self._start_worker(use_whisper=use_whisper)

    def _start_automatic_refinement(self) -> None:
        if self.worker is not None or self._close_after_cancel or self._scan_canceled:
            diagnostic_event(
                "automatic_refinement_skipped", already_running=self.worker is not None,
                closing=self._close_after_cancel, canceled=self._scan_canceled,
            )
            return
        self._whisper_after_refinement = True
        self.start_refinement()

    def start_refinement(self) -> None:
        if self.worker is not None or self._close_after_cancel:
            return
        if not self.source_captions:
            QMessageBox.information(
                self, "No imported captions", "No original YouTube caption evidence is available."
            )
            return
        self._commit_draft_editors()
        if self.refined_table.rowCount():
            answer = QMessageBox.question(
                self,
                "Replace Refined YouTube draft?",
                "A successful refinement will replace only the Refined YouTube draft and its edits, "
                "using the original imported captions. Your original YouTube and Whisper drafts "
                "will not change. A failed or canceled scan keeps all drafts. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_worker(use_whisper=False, refine=True)

    def _start_worker(self, *, use_whisper: bool, refine: bool = False) -> None:
        self._scan_canceled = False
        if not refine:
            self.analysis_result = None
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        target_table = self.refined_table if refine else self.local_table
        target_status = self.refined_status if refine else self.local_status
        target_table.setEnabled(False)
        target_status.setText(
            "Measuring audio pauses for YouTube captions..." if refine else
            "Whisper is running..." if use_whisper else "Scanning audio activity..."
        )
        self._update_selection_controls()
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
            str(self.model_combo.currentData()),
            str(self.language_combo.currentData()),
            source_captions=list(self.source_captions) if refine else None,
            pause_threshold=self.pause_spin.value(),
        )
        self.worker = worker
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

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        if self._scan_canceled or self._close_after_cancel:
            return
        self.progress_label.setText(message)
        if self.worker is not None:
            status = (
                self.refined_status if self.worker.source_captions is not None else self.local_status
            )
            status.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(value)

    @Slot(object)
    def _completed(self, value: object) -> None:
        diagnostic_event(
            "analysis_result_received", canceled=self._scan_canceled,
            closing=self._close_after_cancel, valid=isinstance(value, AnalysisResult),
        )
        # A result can already be queued when the user cancels or closes the review.
        if self._scan_canceled or self._close_after_cancel:
            self._canceled()
            return
        if not isinstance(value, AnalysisResult):
            self._failed("Analysis returned an unexpected result")
            return
        if value.refined_captions is not None:
            self._populate_rows(self.refined_table, [
                AnalysisDraftRow(f"{cue.start:.3f}", f"{cue.end:.3f}", cue.text, cue.source)
                for cue in value.refined_captions
            ])
            self.refined_status.setText(
                f"{len(value.refined_captions)} Refined YouTube rows. "
                "Review Source notes for limited timing or unchanged ranges. "
                "Music can hide pauses; speaker changes are not detected."
            )
            self.refined_radio.setEnabled(bool(self.refined_table.rowCount()))
            if self.refined_radio.isEnabled():
                self.refined_radio.setChecked(True)
                self.refined_table.selectRow(0)
            elif self.refined_radio.isChecked():
                self.local_radio.setChecked(True)
            self.progress_label.setText(
                "YouTube refinement complete; original YouTube and local drafts are unchanged."
            )
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(1000)
            self._set_idle()
            self.review_changed.emit(self.review_state())
            return
        self.analysis_result = value
        self.local_source = "Whisper" if value.model_name else "Audio activity"
        self.local_panel.setTitle(
            "Whisper Transcript" if value.model_name else "Detected Audio Ranges"
        )
        suggestions = [
            item for item in value.suggestions if item.source == "Whisper"
        ] if self.source_choice else value.suggestions
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
        self.cancel_button.setEnabled(False)
        self.local_table.setEnabled(not self._close_after_cancel)
        self.refined_table.setEnabled(not self._close_after_cancel)
        self._update_selection_controls()
        self.whisper_check.setEnabled(True)
        self.sensitivity_combo.setEnabled(True)
        self._whisper_toggled(self.whisper_check.isChecked())

    @Slot(str)
    def _failed(self, message: str) -> None:
        self._whisper_after_refinement = False
        diagnostic_event("analysis_failure_displayed", message=message)
        if self._scan_canceled or self._close_after_cancel:
            self._canceled()
            return
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis failed")
        if self.worker is not None and self.worker.source_captions is not None:
            self.refined_status.setText("YouTube refinement failed; all saved drafts are unchanged.")
        else:
            self.local_status.setText("Local analysis failed; any saved draft is unchanged.")
        self._set_idle()
        QMessageBox.critical(
            self, "Video analysis failed", f"{message}\n\nDiagnostic log: {self.log_path}"
        )

    @Slot()
    def _canceled(self) -> None:
        self._whisper_after_refinement = False
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis canceled; diagnostic log retained.")
        if self.worker is not None and self.worker.source_captions is not None:
            self.refined_status.setText("YouTube refinement canceled; all saved drafts are unchanged.")
        else:
            self.local_status.setText("Local analysis canceled; any saved draft is unchanged.")
        self._set_idle()

    @Slot()
    def _worker_finished(self) -> None:
        diagnostic_event("analysis_worker_finished", closing=self._close_after_cancel)
        if self.sender() is self.worker:
            self.worker = None
        self._set_idle()
        if self._close_after_cancel:
            self._finish_review()
        elif self._whisper_after_refinement:
            self._whisper_after_refinement = False
            self.start_scan()

    def checked_suggestions(self) -> list[AnalysisSuggestion]:
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
        try:
            start = float(table.item(row, 1).text())
            end = float(table.item(row, 2).text())
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("Non-finite range")
        except (AttributeError, ValueError):
            QMessageBox.warning(self, "Invalid suggestion", "Fix this row's In/Out values first.")
            return
        start = max(0.0, min(self.duration, start))
        end = max(0.0, min(self.duration, end))
        if end - start < 0.05:
            QMessageBox.warning(self, "Invalid suggestion", "The preview range is too short.")
            return
        self.preview_requested.emit(start, end)

    def accept_suggestions(self) -> None:
        if self._close_after_cancel:
            return
        self._commit_draft_editors()
        try:
            suggestions = self.checked_suggestions()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid suggestion", str(error))
            return
        if not suggestions:
            QMessageBox.information(self, "No suggestions selected", "Check at least one row first.")
            return
        if self.existing_segments:
            answer = QMessageBox.question(
                self,
                "Add alongside existing segments?",
                f"This project already has {self.existing_segments} segment(s). Add "
                f"{len(suggestions)} checked suggestion(s) without replacing anything?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._accepted_suggestions = suggestions
        self._accept_after_cancel = True
        self._close_review()

    def _close_review(self) -> None:
        self._commit_draft_editors()
        self._close_after_cancel = True
        self.review_changed.emit(self.review_state())
        if self.worker is not None:
            self.youtube_table.setEnabled(False)
            self.refined_table.setEnabled(False)
            self.local_table.setEnabled(False)
            self._update_selection_controls()
            self.cancel_scan()
        else:
            self._finish_review()

    def _commit_draft_editors(self) -> None:
        for table in (self.youtube_table, self.refined_table, self.local_table):
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
        if self.worker is not None:
            self._scan_canceled = True
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.scan_button.setText("Canceling...")
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("Canceling analysis; current captions will be kept...")

    def reject(self) -> None:
        if not self._close_after_cancel:
            self._close_review()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.ignore()
        self.reject()
