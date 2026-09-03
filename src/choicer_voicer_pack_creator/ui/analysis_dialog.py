from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
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
    ) -> None:
        super().__init__(parent)
        self.media = media
        self.video = video.resolve()
        self.duration = duration
        self.data_root = data_root.resolve()
        self.existing_segments = existing_segments
        self.worker: AnalysisWorker | None = None
        self._close_after_cancel = False
        self.result: AnalysisResult | None = None
        self.hardware = detect_hardware()

        self.setWindowTitle(
            "Initial Video Analysis" if initial_scan else "Analyze Video & Suggest Segments"
        )
        self.resize(1050, 720)
        self.setMinimumSize(760, 520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Create editable starting points from local audio. Activity scanning is deterministic. "
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
        self.whisper_check.setChecked(True)
        self.whisper_check.toggled.connect(self._whisper_toggled)
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

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Use", "In", "Out", "Suggested transcript", "Evidence", "Token score"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(4, 165)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda row, _column: self.preview_row(row))
        layout.addWidget(self.table, 1)

        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.scan_button = QPushButton("Scan Video")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.start_scan)
        controls.addButton(self.scan_button, QDialogButtonBox.ButtonRole.ActionRole)
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
        if self.worker and self.worker.isRunning():
            return
        if self.table.rowCount():
            answer = QMessageBox.question(
                self,
                "Replace current analysis results?",
                "Starting another scan clears the current editable rows. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        use_whisper = self.whisper_check.isChecked()
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
                    return
        self.table.setRowCount(0)
        self.result = None
        self.scan_button.setEnabled(False)
        self.add_button.setEnabled(False)
        self.preview_button.setEnabled(False)
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
        self.result = value
        self._populate(value.suggestions)
        language = f" · detected {value.detected_language}" if value.detected_language else ""
        self.progress_label.setText(
            f"Review {len(value.suggestions)} suggestion(s) · "
            f"{value.activity_regions} activity / {value.transcript_regions} transcript regions{language}"
        )
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self._set_idle()
        self.add_button.setEnabled(bool(value.suggestions))
        self.preview_button.setEnabled(bool(value.suggestions))
        if value.suggestions:
            self.table.selectRow(0)

    def _populate(self, suggestions: list[AnalysisSuggestion]) -> None:
        self.table.setRowCount(len(suggestions))
        for row, suggestion in enumerate(suggestions):
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            use.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, use)
            for column, text in (
                (1, f"{suggestion.start:.3f}"),
                (2, f"{suggestion.end:.3f}"),
                (3, suggestion.caption),
            ):
                self.table.setItem(row, column, QTableWidgetItem(text))
            evidence = QTableWidgetItem(suggestion.source)
            evidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 4, evidence)
            confidence = QTableWidgetItem(
                f"{suggestion.confidence:.0%}" if suggestion.confidence is not None else "—"
            )
            confidence.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 5, confidence)

    def _set_idle(self) -> None:
        self.scan_button.setEnabled(True)
        self.whisper_check.setEnabled(True)
        self.sensitivity_combo.setEnabled(True)
        self._whisper_toggled(self.whisper_check.isChecked())

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis failed")
        self._set_idle()
        QMessageBox.critical(self, "Video analysis failed", message)

    @Slot()
    def _canceled(self) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Analysis canceled")
        self._set_idle()

    @Slot()
    def _worker_finished(self) -> None:
        if self.sender() is self.worker:
            self.worker = None
        if self._close_after_cancel:
            self._close_after_cancel = False
            super().reject()

    def checked_suggestions(self) -> list[AnalysisSuggestion]:
        selected: list[AnalysisSuggestion] = []
        for row in range(self.table.rowCount()):
            use = self.table.item(row, 0)
            if not use or use.checkState() != Qt.CheckState.Checked:
                continue
            try:
                start = float(self.table.item(row, 1).text())
                end = float(self.table.item(row, 2).text())
            except (AttributeError, ValueError):
                raise ValueError(f"Suggestion {row + 1} has invalid In/Out values") from None
            start = max(0.0, min(self.duration, start))
            end = max(0.0, min(self.duration, end))
            if end - start < 0.05:
                raise ValueError(f"Suggestion {row + 1} must be at least 0.05 seconds long")
            caption = self.table.item(row, 3).text().strip()
            evidence = self.table.item(row, 4).text()
            confidence_text = self.table.item(row, 5).text().strip("%— ")
            confidence = float(confidence_text) / 100 if confidence_text else None
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

    def preview_row(self, row: int) -> None:
        try:
            start = float(self.table.item(row, 1).text())
            end = float(self.table.item(row, 2).text())
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
        self.suggestions_accepted.emit(suggestions)
        self.accept()

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            self._close_after_cancel = True
            self.worker.requestInterruption()
            self.progress_label.setText("Canceling analysis…")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker and self.worker.isRunning():
            self._close_after_cancel = True
            self.worker.requestInterruption()
            self.progress_label.setText("Canceling analysis…")
            event.ignore()
            return
        super().closeEvent(event)
