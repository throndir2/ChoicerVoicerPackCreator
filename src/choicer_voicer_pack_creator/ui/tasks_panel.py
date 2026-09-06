from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.jobs import JobManager, JobRecord
    from choicer_voicer_pack_creator.ui.main_window import MainWindow


class TasksPanel(QDockWidget):
    def __init__(self, manager: JobManager, workspace: MainWindow) -> None:
        super().__init__("Tasks", workspace)
        self.setObjectName("tasksDock")
        self.manager = manager
        self.workspace = workspace
        self.project_id: str | None = None
        self._records: dict[str, JobRecord] = {}
        self._logs: dict[str, list[str]] = {}
        self._starts: dict[str, float] = {}
        self._elapsed: dict[str, float] = {}
        self._details: dict[str, QWidget] = {}
        self._retry: dict[str, Callable[[], None]] = {}
        self._shown_for_task = False
        content = QWidget(self)
        layout = QVBoxLayout(content)
        controls = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.setObjectName("taskProjectFilter")
        self.filter.addItems(["All projects", "Current project"])
        self.filter.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.filter)
        self.project_button = QPushButton("Show project")
        self.project_button.setObjectName("taskShowProject")
        self.project_button.clicked.connect(self._show_project)
        controls.addWidget(self.project_button)
        self.cancel_button = QPushButton("Cancel task")
        self.cancel_button.setObjectName("taskCancel")
        self.cancel_button.clicked.connect(self._cancel)
        controls.addWidget(self.cancel_button)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("taskRetry")
        self.retry_button.clicked.connect(self._retry_selected)
        controls.addWidget(self.retry_button)
        self.output_button = QPushButton("Open output")
        self.output_button.setObjectName("taskOpenOutput")
        self.output_button.clicked.connect(self._open_output)
        controls.addWidget(self.output_button)
        self.detail_button = QPushButton("Open review / details")
        self.detail_button.setObjectName("taskDetails")
        self.detail_button.clicked.connect(self._show_details)
        controls.addWidget(self.detail_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("tasksTable")
        self.table.setHorizontalHeaderLabels(["Project", "Task", "State", "Stage progress", "Elapsed"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setObjectName("taskLog")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(95)
        layout.addWidget(self.details)
        self.setWidget(content)
        manager.changed.connect(self._changed)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def register_detail(self, job_id: str, widget: QWidget) -> None:
        self._details[job_id] = widget
        self._selection_changed()

    def register_retry(self, job_id: str, callback: Callable[[], None]) -> None:
        self._retry[job_id] = callback
        self._selection_changed()

    def _changed(self, record: JobRecord) -> None:
        if not self._shown_for_task and record.kind not in {"save", "recovery", "workspace"}:
            self._shown_for_task = True
            self.show()
            self.workspace.resizeDocks([self], [200], Qt.Orientation.Vertical)
        previous = self._records.get(record.id)
        self._records[record.id] = record
        if record.state == "running" and record.id not in self._starts:
            self._starts[record.id] = time.monotonic()
        if not record.active and record.id in self._starts:
            self._elapsed.setdefault(record.id, time.monotonic() - self._starts[record.id])
        line = f"{record.state}: {record.message}"
        if record.error:
            line += f"\n{record.error}"
        logs = self._logs.setdefault(record.id, [])
        if not logs or line != logs[-1]:
            logs.append(line)
            del logs[:-300]
        if record.project_id in self.workspace.editors:
            editor = self.workspace.editors[record.project_id]
            if record.state in {"failed", "blocked"}:
                editor.session.attention = record.error or record.message
            elif previous is None or previous.active:
                self.workspace.refresh_tabs()
        self.refresh()
        self.workspace.refresh_tabs()

    def refresh(self) -> None:
        selected = self._selected_id()
        records = [
            record for record in self.manager.tasks()
            if self.filter.currentIndex() == 0 or record.project_id == self.project_id
        ]
        self.table.blockSignals(True)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            self._records[record.id] = record
            editor = self.workspace.editors.get(record.project_id)
            label = editor.project.title if editor else "Application"
            fraction = "" if record.fraction is None else f" ({record.fraction:.0%} of stage)"
            elapsed = self._elapsed.get(
                record.id, time.monotonic() - self._starts.get(record.id, time.monotonic())
            )
            values = (
                label, record.title, record.state.capitalize(),
                record.message + fraction, f"{max(0, int(elapsed)) // 60:02}:{max(0, int(elapsed)) % 60:02}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, record.id)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            if record.id == selected:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self._selection_changed()

    def _selected_id(self) -> str | None:
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    @staticmethod
    def _output(record: JobRecord) -> Path | None:
        if record.state != "succeeded":
            return None
        value = record.result
        if isinstance(value, Path):
            return value
        for attribute in ("pack_path", "video_path"):
            path = getattr(value, attribute, None)
            if isinstance(path, Path):
                return path
        return None

    def _selection_changed(self) -> None:
        job_id = self._selected_id()
        record = self._records.get(job_id)
        self.cancel_button.setEnabled(bool(record and record.active and not record.cancel_requested))
        self.project_button.setEnabled(bool(record and record.project_id in self.workspace.editors))
        self.output_button.setEnabled(bool(record and self._output(record)))
        self.detail_button.setEnabled(job_id in self._details)
        self.retry_button.setEnabled(bool(
            record and record.state in {"failed", "cancelled", "blocked"} and job_id in self._retry
        ))
        self.details.setPlainText("\n".join(self._logs.get(job_id, [])))

    def _cancel(self) -> None:
        job_id = self._selected_id()
        if job_id is not None:
            self.manager.cancel(job_id)

    def _show_project(self) -> None:
        record = self._records.get(self._selected_id())
        if record and record.project_id in self.workspace.editors:
            self.workspace.focus_project(record.project_id)

    def _open_output(self) -> None:
        record = self._records.get(self._selected_id())
        path = self._output(record) if record else None
        if path is not None and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.details.appendPlainText(f"Could not open output. Open it manually: {path}")

    def _show_details(self) -> None:
        widget = self._details.get(self._selected_id())
        if widget is not None:
            widget.show()
            widget.raise_()

    def _retry_selected(self) -> None:
        callback = self._retry.get(self._selected_id())
        if callback is not None:
            callback()
