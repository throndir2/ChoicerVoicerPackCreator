from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import AnalysisDraftRow, AnalysisReview, SourceCaption


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

    def run(self) -> None:
        def report(message: str, fraction: float | None) -> None:
            value = -1 if fraction is None else max(0, min(1000, round(fraction * 1000)))
            self.progress.emit(message, value)

        try:
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
            )
            if self.isInterruptionRequested():
                self.canceled.emit()
            else:
                self.completed.emit(result)
        except AnalysisCancelled:
            self.canceled.emit()
        except Exception as error:
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
        self.existing_segments = existing_segments
        self.worker: AnalysisWorker | None = None
        self._close_after_cancel = False
        self._accept_after_cancel = False
        self._accepted_suggestions: list[AnalysisSuggestion] = []
        self.source_captions = list(source_captions or [])
        self.source_choice = youtube_import or bool(self.source_captions)
        self.local_source = review.local_source if review else "Whisper"
        self.analysis_result: AnalysisResult | None = None
        self.hardware = detect_hardware()

        self.setWindowTitle(
            "Initial Video Analysis" if initial_scan else "Analyze Video & Suggest Segments"
        )
        self.resize(1300 if self.source_choice else 1050, 720)
        self.setMinimumSize(760, 520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            (
                "Review YouTube and Whisper independently, then choose one transcript. "
                "Each uses its own text, In/Out timings, and segment boundaries; rows are not "
                "matched or flagged as conflicts. Choosing a source adds its checked rows to "
                "the project. Closing keeps both drafts for later, without adding segments. "
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

        progress_row = QHBoxLayout()
        self.progress_label = QLabel("Ready to scan")
        self.progress_label.setObjectName("muted")
        progress_row.addWidget(self.progress_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(320)
        progress_row.addWidget(self.progress_bar)
        layout.addLayout(progress_row)

        self.youtube_table = self._create_table()
        self.local_table = self._create_table()
        if self.source_choice:
            self.youtube_table.setColumnHidden(5, True)
            self.local_table.setColumnHidden(4, True)
        self.youtube_radio = QRadioButton("YouTube text + timings")
        self.local_radio = QRadioButton("Whisper text + timings")
        self.source_group = QButtonGroup(self)
        self.source_group.addButton(self.youtube_radio)
        self.source_group.addButton(self.local_radio)
        selected = review.selected_source if review else (
            "youtube" if self.source_captions else "local"
        )
        (self.youtube_radio if selected == "youtube" else self.local_radio).setChecked(True)
        self.youtube_status = QLabel()
        self.local_status = QLabel("Whisper has not run yet.")
        if self.source_choice:
            splitter = QSplitter(Qt.Orientation.Horizontal)
            for radio, status, table in (
                (self.youtube_radio, self.youtube_status, self.youtube_table),
                (self.local_radio, self.local_status, self.local_table),
            ):
                panel = QWidget()
                panel_layout = QVBoxLayout(panel)
                panel_layout.setContentsMargins(0, 0, 0, 0)
                panel_layout.addWidget(radio)
                status.setWordWrap(True)
                panel_layout.addWidget(status)
                panel_layout.addWidget(table)
                splitter.addWidget(panel)
            splitter.setChildrenCollapsible(False)
            layout.addWidget(splitter, 1)
        else:
            self.youtube_table.hide()
            layout.addWidget(self.local_table, 1)

        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        controls.button(QDialogButtonBox.StandardButton.Close).setText("Keep Drafts && Close")
        self.scan_button = QPushButton(
            "Run Whisper" if self.source_choice else "Scan Video"
        )
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.start_scan)
        controls.addButton(self.scan_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_button = QPushButton("Cancel Scan")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        controls.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.preview_button = QPushButton("Preview Row")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview_current_row)
        controls.addButton(self.preview_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.add_button = QPushButton("Add Checked Suggestions")
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
            self.local_status.setText(f"Saved {self.local_source} draft: {len(review.local_rows)} rows.")
        self.youtube_status.setText(
            f"{self.youtube_table.rowCount()} YouTube caption rows."
            if self.youtube_table.rowCount() else "No YouTube captions are available for this video."
        )
        self.youtube_radio.setEnabled(bool(self.youtube_table.rowCount()))
        if not self.youtube_radio.isEnabled():
            self.local_radio.setChecked(True)
        self.source_group.buttonToggled.connect(self._source_changed)
        for table in (self.youtube_table, self.local_table):
            table.itemChanged.connect(self._draft_edited)
        self._update_selection_controls()
        if review:
            self.progress_label.setText("Saved drafts restored; choose a source or rerun local analysis.")
        elif self.source_choice:
            self.progress_label.setText("Choose YouTube now or wait for the separate Whisper transcript.")
        if auto_start:
            QTimer.singleShot(0, self.start_scan)

    @property
    def table(self) -> QTableWidget:
        return self.youtube_table if self.source_choice and self.youtube_radio.isChecked() else self.local_table

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
            "youtube" if self.source_choice and self.youtube_radio.isChecked() else "local",
            self.local_source,
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
            self._update_selection_controls()
            self.review_changed.emit(self.review_state())

    def _update_selection_controls(self) -> None:
        usable = bool(self.table.rowCount()) and self.table.isEnabled()
        self.add_button.setEnabled(usable and not self._close_after_cancel)
        self.preview_button.setEnabled(usable and not self._close_after_cancel)
        if self.source_choice:
            source = "YouTube" if self.youtube_radio.isChecked() else "Whisper"
            self.add_button.setText(f"Use {source} Transcript")
        else:
            self.add_button.setText("Add Checked Suggestions")

    def _whisper_toggled(self, checked: bool) -> None:
        self.model_combo.setEnabled(checked)
        self.language_combo.setEnabled(checked)
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
        except Exception:
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
        if self.worker is not None or self._close_after_cancel:
            return
        if self.local_table.rowCount():
            answer = QMessageBox.question(
                self,
                "Replace local analysis draft?",
                "A successful scan will replace the local draft and its edits. "
                "The YouTube draft will not change. A failed or canceled scan keeps both drafts. "
                "Continue?" if self.source_choice else
                "A successful scan will replace the current draft and its edits. "
                "A failed or canceled scan keeps the draft. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        use_whisper = self.source_choice or self.whisper_check.isChecked()
        model_key = str(self.model_combo.currentData())
        if use_whisper:
            try:
                manager = WhisperManager(self.data_root)
            except Exception as error:
                QMessageBox.critical(self, "Whisper setup is unavailable", str(error))
                return
            model_missing = not manager.model_path(model_key).is_file()
            runtime_missing = not manager.cli_path.is_file()
            if model_missing or runtime_missing:
                download_mib = manager.model_download_bytes(model_key) / 1024**2
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
                if answer != QMessageBox.StandardButton.Yes:
                    self.progress_label.setText(
                        "Whisper not started. You can still edit and add available captions."
                    )
                    return
        self.analysis_result = None
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.local_table.setEnabled(False)
        self.local_status.setText("Whisper is running..." if use_whisper else "Scanning audio activity...")
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
            model_key,
            str(self.language_combo.currentData()),
        )
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        self.progress_label.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(value)

    @Slot(object)
    def _completed(self, value: object) -> None:
        if not isinstance(value, AnalysisResult):
            self._failed("Analysis returned an unexpected result")
            return
        self.analysis_result = value
        self.local_source = "Whisper" if value.model_name else "Audio activity"
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
                evidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, 4, evidence)
                confidence = QTableWidgetItem(
                    f"{draft.confidence:.0%}" if draft.confidence is not None else "—"
                )
                confidence.setData(Qt.ItemDataRole.UserRole, draft.confidence)
                confidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, 5, confidence)

    def _set_idle(self) -> None:
        self.scan_button.setEnabled(self.worker is None and not self._close_after_cancel)
        self.cancel_button.setEnabled(False)
        self.local_table.setEnabled(True)
        self._update_selection_controls()
        self.whisper_check.setEnabled(True)
        self.sensitivity_combo.setEnabled(True)
        self._whisper_toggled(self.whisper_check.isChecked())

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis failed")
        self.local_status.setText("Local analysis failed; any saved draft is unchanged.")
        self._set_idle()
        QMessageBox.critical(self, "Video analysis failed", message)

    @Slot()
    def _canceled(self) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis canceled")
        self.local_status.setText("Local analysis canceled; any saved draft is unchanged.")
        self._set_idle()

    @Slot()
    def _worker_finished(self) -> None:
        if self.sender() is self.worker:
            self.worker = None
        self._set_idle()
        if self._close_after_cancel:
            self._finish_review()

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
            self.local_table.setEnabled(False)
            self._update_selection_controls()
            self.cancel_scan()
        else:
            self._finish_review()

    def _commit_draft_editors(self) -> None:
        for table in (self.youtube_table, self.local_table):
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
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.progress_label.setText("Canceling analysis; current captions will be kept...")

    def reject(self) -> None:
        if not self._close_after_cancel:
            self._close_review()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.ignore()
        self.reject()
