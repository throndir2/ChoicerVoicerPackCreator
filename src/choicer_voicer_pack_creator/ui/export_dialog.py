from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.export_progress import (
    VIDEO_CONVERSION_STEP,
    ExportEstimator,
    ExportProgress,
    format_remaining,
)
from choicer_voicer_pack_creator.exporter import ExportResult


class ExportProgressDialog(QDialog):
    def __init__(self, destination: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = True
        self._outcome: bool | None = None
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._step_started = 0
        self._step = ""
        self._last_position = 0.0
        self._last_advance = 0
        self._live = False
        self._estimator = ExportEstimator()
        self.setWindowTitle("Exporting pack")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(760, 620)

        layout = QVBoxLayout(self)
        self.destination_label = QLabel(f"Export destination:\n{destination}")
        self.destination_label.setTextFormat(Qt.TextFormat.PlainText)
        self.destination_label.setWordWrap(True)
        self.destination_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.destination_label)
        self.progress_label = QLabel()
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setToolTip(
            "Measured progress for the current step, when available."
        )
        layout.addWidget(self.progress_bar)
        self.step_eta_label = QLabel()
        self.step_eta_label.setWordWrap(True)
        self.step_eta_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.step_eta_label)
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 0)
        self.overall_bar.setToolTip(
            "An estimate for the whole export, including prompts, validation, ZIP, and publishing. "
            "It may move backward as measured timings replace initial estimates."
        )
        layout.addWidget(self.overall_bar)
        self.overall_eta_label = QLabel()
        self.overall_eta_label.setWordWrap(True)
        layout.addWidget(self.overall_eta_label)
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName("muted")
        layout.addWidget(self.elapsed_label)
        self.activity_label = QLabel()
        self.activity_label.setWordWrap(True)
        layout.addWidget(self.activity_label)
        layout.addWidget(QLabel("Activity details"))
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(2000)
        layout.addWidget(self.details, 1)
        self.note_label = QLabel(
            "Please keep this window open until export finishes. "
            "Existing output is kept as a rollback backup during publishing."
        )
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setEnabled(False)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_elapsed)
        self._timer.start()
        self.report_progress(ExportProgress("Preparing export..."))

    @staticmethod
    def _format_elapsed(milliseconds: int) -> str:
        seconds = milliseconds // 1000
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @Slot()
    def _update_elapsed(self) -> None:
        elapsed = self._elapsed.elapsed()
        text = f"Elapsed: {self._format_elapsed(elapsed)}"
        if self._outcome is None:
            text += f" | Current step: {self._format_elapsed(elapsed - self._step_started)}"
        self.elapsed_label.setText(text)
        if self._outcome is None:
            estimate = self._estimator.estimate(elapsed / 1000)
            self.step_eta_label.setText(
                f"{estimate.step_title or 'Current step'}: {format_remaining(estimate.step_remaining)}"
            )
            self.overall_eta_label.setText(
                f"Whole export: {format_remaining(estimate.total_remaining)} "
                "(estimate adjusts as work completes)"
            )
            if estimate.total_fraction is None:
                self.overall_bar.setRange(0, 0)
            else:
                self.overall_bar.setRange(0, 1000)
                self.overall_bar.setValue(int(estimate.total_fraction * 1000))
                self.overall_bar.setFormat("Estimated overall progress: %p%")
        else:
            self.step_eta_label.clear()
            self.overall_eta_label.setText("Export complete" if self._outcome else "Export failed")
        encoding = self._outcome is None and self._step == VIDEO_CONVERSION_STEP and self._live
        self.activity_label.setVisible(encoding)
        if encoding:
            idle = elapsed - self._last_advance
            if idle >= 15000:
                self.activity_label.setText(
                    f"No encoding advance for {self._format_elapsed(idle)}. "
                    "Waiting for FFmpeg's next frame report; encoding can be slow."
                )
            elif self._last_position == 0:
                self.activity_label.setText("Waiting for FFmpeg to encode the first frame...")
            else:
                self.activity_label.setText("Encoding the full video; prompt extraction follows.")

    @Slot(object)
    def report_progress(self, update: ExportProgress) -> None:
        elapsed = self._elapsed.elapsed()
        same_step = bool(update.step) and update.step == self._step
        replace_history = same_step and update.live and self._live
        self._live = update.live
        self._step = update.step or update.message
        self._estimator.observe(update, elapsed / 1000)
        if not same_step:
            self._step_started = elapsed
            self._last_advance = elapsed
            self._last_position = 0.0
        if update.position is not None and update.position > self._last_position:
            self._last_position = update.position
            self._last_advance = elapsed
        self.progress_label.setText(update.message)
        history = " | ".join(update.message.splitlines())
        if replace_history:
            cursor = self.details.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.movePosition(
                QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.insertText(history)
        else:
            self.details.appendPlainText(history)
        if update.fraction is None:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setToolTip(
                "This operation has no measurable percentage; this is not overall export progress."
            )
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(update.fraction * 1000))
            self.progress_bar.setFormat("Current step: %p%")
            self.progress_bar.setToolTip(
                "Measured progress for this step only, not the whole export."
            )
        self._update_elapsed()

    def show_result(self, result: ExportResult) -> None:
        self._outcome = True
        self.setWindowTitle("Pack exported")
        self.report_progress(ExportProgress(
            "Export complete with cleanup notes" if result.warnings else "Export complete"
        ))
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat("Complete")
        self.overall_bar.setRange(0, 1)
        self.overall_bar.setValue(1)
        self.overall_bar.setFormat("Complete")
        summary = (
            f"Validated pack folder:\n{result.pack_path}\n\n"
            f"{result.validation['clip_count']} prompts / {result.validation['file_count']} files"
        )
        if result.zip_path:
            summary += f"\n\nValidated ZIP:\n{result.zip_path}"
        if result.warnings:
            summary += "\n\nCleanup notes:\n" + "\n".join(result.warnings)
        self.details.appendPlainText(summary)

    def show_error(self, message: str) -> None:
        self._outcome = False
        self.setWindowTitle("Export failed")
        failed_step = self.progress_label.text()
        self.report_progress(ExportProgress("Export failed"))
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Failed")
        self.overall_bar.setRange(0, 1)
        self.overall_bar.setValue(0)
        self.overall_bar.setFormat("Failed")
        self.details.appendPlainText(f"Last operation: {failed_step}\n\n{message}")

    def worker_finished(self) -> None:
        if self._outcome is None:
            self.show_error("The exporter stopped without returning a result.")
        self._running = False
        self._update_elapsed()
        self._timer.stop()
        self.note_label.setText(
            "Export finished. Output locations and any cleanup notes are listed above."
            if self._outcome
            else "Export did not complete. Review the error details above before trying again."
        )
        self.close_button.setEnabled(True)
        self.close_button.setFocus()

    def done(self, result: int) -> None:
        if not self._running:
            super().done(result)

    def reject(self) -> None:
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._running:
            event.ignore()
        else:
            super().closeEvent(event)
