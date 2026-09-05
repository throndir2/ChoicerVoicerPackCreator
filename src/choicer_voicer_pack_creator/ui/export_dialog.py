from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
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
        self.setWindowTitle("Exporting pack")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(700, 500)

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
            "Export stages take different amounts of time; an overall percentage is not available."
        )
        layout.addWidget(self.progress_bar)
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName("muted")
        layout.addWidget(self.elapsed_label)
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
        self.report_progress("Preparing export...")

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

    @Slot(str)
    def report_progress(self, message: str) -> None:
        self._step_started = self._elapsed.elapsed()
        self.progress_label.setText(message)
        self.details.appendPlainText(
            f"[{self._format_elapsed(self._step_started)}] {message}"
        )
        self._update_elapsed()

    def show_result(self, result: ExportResult) -> None:
        self._outcome = True
        self.setWindowTitle("Pack exported")
        self.report_progress(
            "Export complete with cleanup notes" if result.warnings else "Export complete"
        )
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat("Complete")
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
        self.report_progress("Export failed")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Failed")
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
