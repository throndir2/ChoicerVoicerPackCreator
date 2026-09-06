from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog

from choicer_voicer_pack_creator.automation import (
    PackAutomation,
    ProjectSnapshot,
    require_revision,
    save_snapshot,
)
from choicer_voicer_pack_creator.mcp_server import create_server
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow

T = TypeVar("T")


class EditorBridge(QObject):
    requested = Signal(object, object)
    disconnected = Signal()

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.closed = False
        self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)
        self.disconnected.connect(self._disconnect, Qt.ConnectionType.QueuedConnection)
        window.destroyed.connect(self._closed)

    def _closed(self) -> None:
        self.closed = True

    def call(self, function: Callable[[], T]) -> T:
        if self.closed:
            raise RuntimeError("The live editor has closed.")
        from PySide6.QtCore import QThread
        if QThread.currentThread() == self.thread():
            return function()
        future: Future[T] = Future()
        self.requested.emit(function, future)
        try:
            return future.result(timeout=30)
        except TimeoutError:
            # Do not let a delayed queued edit execute after reporting it as failed.
            if future.cancel():
                raise RuntimeError("The editor is not responding. Close any modal dialog and retry.") from None
            return future.result()

    @Slot(object, object)
    def _execute(self, function: Callable[[], T], future: Future[T]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function())
        except Exception as error:
            # Carry GUI-thread errors back to the MCP tool, never hide them in Qt's event loop.
            future.set_exception(error)

    def begin(self, label: str) -> None:
        def apply() -> None:
            if QApplication.activeModalWidget() is not None:
                raise ValueError("Close the editor's modal dialog before making MCP calls.")
            self.window.statusBar().showMessage(f"MCP: {label}")

        self.call(apply)

    def end(self) -> None:
        self.call(lambda: self.window.statusBar().showMessage("MCP ready"))

    @Slot()
    def _disconnect(self) -> None:
        self.window._automation_disconnected = True
        modal = QApplication.activeModalWidget()
        owner = modal.parent() if modal is not None else None
        while owner is not None and owner is not self.window:
            owner = owner.parent()
        if isinstance(modal, QDialog) and owner is self.window:
            modal.reject()
            if modal.isVisible():
                QTimer.singleShot(100, self._disconnect)
                return
        # Workspace close questions are deliberately nonmodal. Reject pending choices
        # before requesting EOF shutdown so their in-progress close state cannot trap it.
        for decision in tuple(self.window._decisions):
            decision.reject()
        self.window.close()


class EditorProjectAccess:
    live = True

    def __init__(self, bridge: EditorBridge, project_id: str | None = None) -> None:
        self.bridge = bridge
        self.project_id = project_id

    def _editor(self):
        window = self.bridge.window
        if self.project_id is not None:
            if self.project_id not in {session.id for session in window.project_sessions}:
                raise ValueError(f"Unknown project_id: {self.project_id}")
            return window.editor_for_project(self.project_id)
        if window.active_editor is None:
            raise ValueError("No active project. Create or open a project first.")
        return window.active_editor

    def bind(self, project_id: str | None = None) -> EditorProjectAccess:
        def bind():
            bound = EditorProjectAccess(
                self.bridge, self._editor().session.id if project_id is None else project_id
            )
            bound._editor()
            return bound
        return self.bridge.call(bind)

    def list_projects(self):
        def read():
            window = self.bridge.window
            snapshots = [
                EditorProjectAccess(self.bridge, session.id)._snapshot()
                for session in window.project_sessions
            ]
            return {
                "active_project_id": window.active_editor.session.id if window.active_editor else None,
                "projects": [
                    {"project_id": item.project_id, "title": item.project.title,
                     "project_path": str(item.path) if item.path else None,
                     "dirty": item.dirty, "loading": item.loading, "revision": item.revision}
                    for item in snapshots
                ],
            }
        return self.bridge.call(read)

    def activate(self, project_id: str) -> ProjectSnapshot:
        def activate():
            EditorProjectAccess(self.bridge, project_id)._editor()
            self.bridge.window.focus_project(project_id)
            return EditorProjectAccess(self.bridge, project_id)._snapshot()
        return self.bridge.call(activate)

    def create(self, snapshot: ProjectSnapshot) -> ProjectSnapshot:
        def create():
            window = self.bridge.window
            if snapshot.path is not None:
                for session in window.project_sessions:
                    editor = window.editor_for_project(session.id)
                    if editor.project_path == snapshot.path:
                        return self.activate(editor.session.id)
            editor = window.add_project(snapshot.project, snapshot.path, dirty=snapshot.dirty)
            editor._saved_project_hash = snapshot.saved_hash
            if snapshot.dirty:
                editor._write_recovery_snapshot()
            if snapshot.path is not None:
                editor._remember_recent_project(snapshot.path)
            return EditorProjectAccess(self.bridge, editor.session.id)._snapshot()
        return self.bridge.call(create)

    def _snapshot(self) -> ProjectSnapshot:
        window = self._editor()
        window._commit_editors()
        return ProjectSnapshot(
            window.project, window.project_path, window.dirty, window._saved_project_hash,
            window.session.id, window.session.loading,
        ).copy()

    def snapshot(self) -> ProjectSnapshot:
        return self.bridge.call(self._snapshot)

    def replace(self, snapshot: ProjectSnapshot, expected_revision: str) -> None:
        def apply() -> None:
            require_revision(self._snapshot(), expected_revision)
            window = self._editor()
            window._set_project(
                snapshot.project, snapshot.path, snapshot.dirty, preserve_view=True
            )
            window._saved_project_hash = snapshot.saved_hash
            # Use the same recovery journal as manual edits.
            if snapshot.dirty:
                window._write_recovery_snapshot()
            else:
                window._clear_recovery_snapshot()
                if snapshot.path is not None:
                    window._remember_recent_project(snapshot.path)

        self.bridge.call(apply)

    def save(self, destination: Path, expected_revision: str, overwrite: bool) -> ProjectSnapshot:
        future: Future[ProjectSnapshot] = Future()

        def submit() -> None:
            snapshot = self._snapshot()
            require_revision(snapshot, expected_revision)
            window = self.bridge.window
            editor = self._editor()
            revision = editor.session.revision

            def operation(ctx):
                with ctx.critical_stage("Saving editable project"):
                    return save_snapshot(snapshot, destination, overwrite)

            reservation = window.reserve_project_save(snapshot.project_id, destination)
            try:
                handle = window.job_manager.submit(
                    snapshot.project_id, "save", f"MCP save: {snapshot.project.title}", operation,
                    resource_class="io", resource_keys=(f"document-save:{snapshot.project_id}",),
                    write_paths=(destination, ProjectStore.previous_path(destination)),
                    source_snapshot={"project_id": snapshot.project_id, "revision": expected_revision},
                )
            except Exception:
                window.release_project_save(reservation)
                raise

            def completed(saved: ProjectSnapshot) -> None:
                try:
                    window.complete_project_save(
                        snapshot.project_id, destination, revision, saved.saved_hash
                    )
                    future.set_result(self._snapshot())
                except Exception as error:
                    future.set_exception(error)

            def finished() -> None:
                window.release_project_save(reservation)
                if not future.done():
                    record = handle.record
                    future.set_exception(RuntimeError(
                        f"Save {record.state}: {record.error or record.message}"
                    ))

            handle.completed.connect(completed)
            handle.finished.connect(finished)

        self.bridge.call(submit)
        return future.result()

    def show(self, segment_id: str | None, timestamp: float | None) -> None:
        def apply() -> None:
            window = self._editor()
            if segment_id is not None and window.project.segment_by_id(segment_id) is None:
                raise ValueError(f"Unknown segment id: {segment_id}")
            if timestamp is not None and not 0 <= timestamp <= window.project.video_duration:
                raise ValueError("Timestamp must be within the source video.")
            if segment_id is not None:
                window.select_segment(segment_id)
            if timestamp is not None:
                window.seek(timestamp)
            self.bridge.window.focus_project(window.session.id)
            self.bridge.window.show()
            self.bridge.window.raise_()

        self.bridge.call(apply)


def start_live_server(window: MainWindow, *, ui_test_hooks: bool = False) -> EditorBridge:
    from choicer_voicer_pack_creator.mcp_jobs import LiveJobs
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    bridge = EditorBridge(window)
    automation = PackAutomation(EditorProjectAccess(bridge), window.analysis_data_root, window.media)
    server = create_server(
        automation, bridge.begin, bridge.end,
        jobs=LiveJobs(bridge), ui=UIAutomation(bridge) if ui_test_hooks else None,
    )

    def run() -> None:
        try:
            server.run(transport="stdio")
        except Exception:
            logging.getLogger(__name__).exception("MCP server stopped with an error")
        finally:
            bridge.disconnected.emit()

    # Qt owns the main thread; the SDK owns its own asyncio loop and stdio reader.
    thread = threading.Thread(target=run, name="choicer-voicer-mcp", daemon=True)
    thread.start()
    window.statusBar().showMessage("Live MCP control enabled. The connected client can edit this project.")
    return bridge
