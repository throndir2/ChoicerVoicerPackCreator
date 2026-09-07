from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QSettings, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from choicer_voicer_pack_creator.history import ProjectHistory
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.timeline_audit import (
    TimelineOverlap,
    audit_timeline_overlaps,
    describe_timeline_overlaps,
)
from choicer_voicer_pack_creator.ui.commands import describe_action
from choicer_voicer_pack_creator.ui.timeline import segment_lanes

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import ProjectEditor

CONFIRM_DELETE_SETTING = "editing/confirmSegmentDeletion"


@dataclass
class ReplayView:
    project: PackProject
    errors: list[str]
    warnings: list[TimelineOverlap]
    details: list[str]
    lanes: dict[str, int]

    @classmethod
    def prepare(cls, project: PackProject) -> ReplayView:
        warnings = audit_timeline_overlaps(project.segments)
        return cls(
            project, project.validate(), warnings,
            describe_timeline_overlaps(project.segments, warnings),
            segment_lanes(project.segments),
        )


class EditHistoryController(QObject):
    """Publish a worker-prepared state atomically, then populate Qt rows in short batches."""

    def __init__(self, editor: ProjectEditor) -> None:
        super().__init__(editor)
        self.editor = editor
        self.history = ProjectHistory(editor.project, limit=100)
        self.suspended = False
        self.busy = False
        self.selected_id = ""
        self._saving: dict[int, int] = {}
        self._dialog: QDialog | None = None
        self._list: QListWidget | None = None
        self._restore_button: QPushButton | None = None
        self._row_timer = QTimer(self)
        self._row_timer.setInterval(0)
        self._row_timer.timeout.connect(self._populate_rows)
        self._view: ReplayView | None = None
        self._next_row = 0
        self._warning_ids: set[str] = set()
        self.undo_action = QAction("Undo", editor)
        self.undo_action.setObjectName("undoProjectEdit")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction("Redo", editor)
        self.redo_action.setObjectName("redoProjectEdit")
        shortcuts = QKeySequence.keyBindings(QKeySequence.StandardKey.Redo)
        if QKeySequence("Ctrl+Shift+Z") not in shortcuts:
            shortcuts.append(QKeySequence("Ctrl+Shift+Z"))
        self.redo_action.setShortcuts(shortcuts)
        self.redo_action.triggered.connect(self.redo)
        self.show_action = QAction("Edit History...", editor)
        self.show_action.setObjectName("showEditHistory")
        self.show_action.triggered.connect(self.show)
        for action in (self.undo_action, self.redo_action):
            action.setAutoRepeat(False)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        describe_action(self.undo_action, "restore", "Undo the last project edit. Text fields keep their own undo.")
        describe_action(self.redo_action, "restore", "Redo the next project edit. Text fields keep their own redo.")
        describe_action(self.show_action, "logs", "Review the last 100 edits for this open project.")
        self.refresh()

    def bind_text_shortcuts(self) -> None:
        for widget in (
            *self.editor.findChildren(QLineEdit), *self.editor.findChildren(QPlainTextEdit),
        ):
            widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() != QEvent.Type.ShortcutOverride or not isinstance(event, QKeyEvent)
            or not isinstance(watched, (QLineEdit, QPlainTextEdit))
            or watched.window() is not self.editor.window()
        ):
            return False
        undo = QKeySequence(event.keyCombination()) in self.undo_action.shortcuts()
        redo = QKeySequence(event.keyCombination()) in self.redo_action.shortcuts()
        if not undo and not redo:
            return False
        local = watched if isinstance(watched, QLineEdit) else watched.document()
        available = local.isUndoAvailable() if undo else local.isRedoAvailable()
        action = self.undo_action if undo else self.redo_action
        if not available and action.isEnabled():
            # Qt text widgets otherwise swallow Undo even when their local stack is empty.
            event.ignore()
            return True
        return False

    def reset(self, *, dirty: bool) -> None:
        self.history.reset(self.editor.project)
        self.history.mark_saved(-1 if dirty else self.history.current_id)
        self.selected_id = ""
        self._saving.clear()
        self.refresh()

    def record(
        self, label: str, *, segment: Segment | None = None,
        merge_key: str | None = None, fields_only: bool = False,
    ) -> bool:
        if not self.suspended:
            changed = self.history.record(
                self.editor.project, label, segment=segment,
                merge_key=merge_key, fields_only=fields_only,
            )
            self.refresh()
            return changed
        return False

    def saving(self, revision: int) -> None:
        self.history.break_merge()
        self._saving[revision] = self.history.current_id

    def saved(self, revision: int) -> None:
        state = self._saving.pop(revision, None)
        if state is not None:
            self.history.mark_saved(state)
        elif revision == self.editor.session.revision:
            self.history.mark_saved()
        self._sync_clean()
        self.refresh()

    def _sync_clean(self) -> None:
        session = self.editor.session
        if self.history.is_clean:
            session.saved_revision = session.revision
        elif session.saved_revision == session.revision:
            session.saved_revision = -1

    def refresh(self) -> None:
        available = not self.busy and not self.editor.session.loading
        self.show_action.setEnabled(available)
        self.undo_action.setEnabled(available and self.history.can_undo)
        self.redo_action.setEnabled(available and self.history.can_redo)
        self.undo_action.setText(
            f"Undo {self.history.undo_label or ''}".rstrip().replace("&", "&&")
        )
        self.redo_action.setText(
            f"Redo {self.history.redo_label or ''}".rstrip().replace("&", "&&")
        )
        if self._list is not None:
            self._list.clear()
            for index, label in enumerate(("Oldest retained state", *self.history.labels)):
                item = QListWidgetItem(
                    f"{'Current: ' if index == self.history.index else ''}{label}"
                )
                item.setData(Qt.ItemDataRole.UserRole, index)
                self._list.addItem(item)
            self._list.setCurrentRow(self.history.index)
            self._list.setEnabled(available)
            self._restore_button.setEnabled(available)

    def undo(self) -> None:
        self.editor._commit_editors()
        self.go_to(self.history.index - 1)

    def redo(self) -> None:
        self.editor._commit_editors()
        self.go_to(self.history.index + 1)

    def go_to(self, index: int) -> None:
        editor = self.editor
        if (
            self.busy or editor.session.loading or editor._range_edit_record is not None
            or editor._range_decision_active
        ):
            editor.statusBar().showMessage("Finish the current edit before using history.", 5000)
            return
        editor._commit_editors()
        if index == self.history.index:
            return
        if not 0 <= index <= len(self.history.labels):
            editor.statusBar().showMessage("No further edit history in that direction.", 5000)
            return
        self.history.break_merge()
        request = self.history.prepare(index)
        revision = editor.session.revision
        editor.prompt_player.stop()
        editor._preview_end = None
        self.busy = True
        editor.statusBar().clear_issue("history")
        editor._set_loading(True, message="Restoring edit history...")
        editor._validation_timer.stop()
        editor._recovery_timer.stop()
        self.refresh()

        def build(context):
            context.check_cancelled()
            view = ReplayView.prepare(request.build_project())
            context.check_cancelled()
            return view

        def completed(view: ReplayView) -> None:
            if (
                editor.session.id in editor.workspace._closed_ids
                or editor.session.revision != revision
                or not self.history.accept(request, view.project)
            ):
                editor.statusBar().showMessage(
                    "History restore skipped because the project changed; newer edits were kept."
                )
                return
            self.suspended = True
            try:
                editor._set_project(
                    view.project, editor.project_path, mark_dirty=True,
                    preserve_view=True, history_replay=True,
                )
                self._sync_clean()
            finally:
                self.suspended = False
            self._view = view
            self._warning_ids = {
                identity for warning in view.warnings
                for identity in (warning.first_id, warning.second_id)
            }
            self._next_row = 0
            with QSignalBlocker(editor.segment_table):
                editor.segment_table.clearSelection()
            self._row_timer.start()

        def failed(message: str) -> None:
            editor.statusBar().set_issue("history", "Could not restore edit history", details=message)
            editor.workspace.notice("Could not restore edit history", message)

        def finished() -> None:
            editor.workspace.job_manager.release_result(job.id)
            job.completed.disconnect(completed)
            job.failed.disconnect(failed)
            job.finished.disconnect(finished)
            if self._view is None:
                if job.record.state == "cancelled":
                    editor.statusBar().showMessage("History restore cancelled; edits were kept.")
                self._finish()

        try:
            job = editor.workspace.job_manager.submit(
                editor.session.id, "edit-history", "Restoring edit history", build,
                resource_class="io", priority=10,
            )
        except (RuntimeError, ValueError) as error:
            self._finish()
            failed(str(error))
            return
        job.completed.connect(completed)
        job.failed.connect(failed)
        job.finished.connect(finished)

    def _populate_rows(self) -> None:
        view = self._view
        if view is None:
            self._row_timer.stop()
            return
        editor = self.editor
        deadline = monotonic() + 0.006
        with QSignalBlocker(editor.segment_table):
            count, target = editor.segment_table.rowCount(), len(view.project.segments)
            if count != target:
                editor.segment_table.setRowCount(
                    min(target, count + 128) if count < target else max(target, count - 128)
                )
                return
            while self._next_row < len(view.project.segments):
                row = self._next_row
                segment = view.project.segments[row]
                editor._populate_table_row(row, segment, warned=segment.id in self._warning_ids)
                self._next_row += 1
                if monotonic() >= deadline:
                    return
        self._row_timer.stop()
        editor.timeline.set_segments(view.project.segments, lanes=view.lanes)
        editor.video_widget.set_segments(view.project.segments)
        selected = view.project.segment_by_id(self.selected_id)
        if selected is not None:
            editor._show_selected_segment(selected)
        else:
            editor.selected_segment_id = ""
            editor.timeline.set_selected("")
            editor.timeline.set_marks(editor.mark_in_spin.value(), editor.mark_out_spin.value())
            editor._sync_selected_editor()
        editor.speaker_matching.history_replayed()
        editor._refresh_validation_label(prepared=(view.errors, view.warnings, view.details))
        editor.statusBar().showMessage(
            "Edit history restored."
            + (f" {len(view.errors)} item(s) need attention." if view.errors else ""),
            5000,
        )
        self._view = None
        self._warning_ids.clear()
        self._finish()

    def _finish(self) -> None:
        self.busy = False
        self.editor._set_loading(False)
        self.editor.workspace.refresh_tabs()
        if self.editor.dirty:
            self.editor._recovery_timer.start()
        else:
            self.editor._clear_recovery_snapshot()
        self.refresh()

    def show(self) -> None:
        if self._dialog is not None:
            self._dialog.show()
            self._dialog.raise_()
            return
        dialog = QDialog(self.editor)
        dialog.setWindowTitle("Edit History")
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.resize(460, 400)
        layout = QVBoxLayout(dialog)
        self._list = QListWidget(dialog)
        self._list.setObjectName("editHistoryList")
        self._list.setToolTip(
            "The latest 100 project edits are retained until this project closes. "
            "Restoring an earlier state keeps later edits available for redo until you make a new edit. "
            "Saved/exported files and other project tabs are not rolled back. "
            "Media files must remain available at their recorded paths."
        )
        layout.addWidget(self._list)
        confirm = QCheckBox("Confirm segment deletion", dialog)
        confirm.setChecked(self.editor.settings.value(CONFIRM_DELETE_SETTING, True, type=bool))
        confirm.toggled.connect(self.set_confirm_delete)
        layout.addWidget(confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        self._restore_button = buttons.addButton(
            "Restore Selected State", QDialogButtonBox.ButtonRole.ActionRole,
        )
        self._restore_button.clicked.connect(lambda: self.go_to(self._list.currentRow()))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def finished() -> None:
            self._dialog = None
            self._list = None
            self._restore_button = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        self._dialog = dialog
        self.refresh()
        dialog.show()

    def set_confirm_delete(self, confirm: bool) -> None:
        settings = self.editor.settings
        settings.setValue(CONFIRM_DELETE_SETTING, confirm)
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            self.editor.workspace.notice(
                "Could not save deletion preference",
                "The deletion preference could not be saved to application settings.",
            )

    def confirm_delete(self, segment: Segment) -> bool:
        if not self.editor.settings.value(CONFIRM_DELETE_SETTING, True, type=bool):
            return True
        box = QMessageBox(
            QMessageBox.Icon.Question, "Delete segment",
            f"Delete {segment.primary_character}: \"{segment.caption or 'Untitled line'}\"?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self.editor,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setTextFormat(Qt.TextFormat.PlainText)
        check = QCheckBox("Don't ask again", box)
        check.setObjectName("dontAskDeleteAgain")
        box.setCheckBox(check)
        confirmed = box.exec() == QMessageBox.StandardButton.Yes
        if confirmed and check.isChecked():
            self.set_confirm_delete(False)
        box.deleteLater()
        return confirmed
