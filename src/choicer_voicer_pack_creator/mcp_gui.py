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
            if self.window._export_worker is not None:
                raise ValueError("Wait for the editor's current export to finish.")
            if self.window._backing_dialog is not None:
                raise ValueError("Close the backing-track workflow before making MCP calls.")
            self.window._commit_editors()
            self.window._automation_active = True
            self.window._set_busy(True, f"MCP: {label}")
            self.window.statusBar().showMessage(f"MCP: {label}")

        self.call(apply)

    def end(self) -> None:
        def apply() -> None:
            self.window._automation_active = False
            self.window._set_busy(False, "MCP ready")

        self.call(apply)

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
        if self.window._export_worker is not None:
            QTimer.singleShot(100, self._disconnect)
            return
        if not self.window.close():
            QTimer.singleShot(100, self._disconnect)


class EditorProjectAccess:
    live = True

    def __init__(self, bridge: EditorBridge) -> None:
        self.bridge = bridge

    def _snapshot(self) -> ProjectSnapshot:
        window = self.bridge.window
        window._commit_editors()
        return ProjectSnapshot(
            window.project, window.project_path, window.dirty, window._saved_project_hash
        ).copy()

    def snapshot(self) -> ProjectSnapshot:
        return self.bridge.call(self._snapshot)

    def replace(self, snapshot: ProjectSnapshot, expected_revision: str) -> None:
        def apply() -> None:
            require_revision(self._snapshot(), expected_revision)
            window = self.bridge.window
            preserve_view = window.project_path == snapshot.path
            window._set_project(
                snapshot.project, snapshot.path, snapshot.dirty, preserve_view=preserve_view
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
        def apply() -> ProjectSnapshot:
            snapshot = self._snapshot()
            require_revision(snapshot, expected_revision)
            saved = save_snapshot(snapshot, destination, overwrite)
            window = self.bridge.window
            window.project_path = saved.path
            window._saved_project_hash = saved.saved_hash
            window._clear_recovery_snapshot()
            window._set_dirty(False)
            window._remember_recent_project(destination)
            return saved

        return self.bridge.call(apply)

    def show(self, segment_id: str | None, timestamp: float | None) -> None:
        def apply() -> None:
            window = self.bridge.window
            if segment_id is not None and window.project.segment_by_id(segment_id) is None:
                raise ValueError(f"Unknown segment id: {segment_id}")
            if timestamp is not None and not 0 <= timestamp <= window.project.video_duration:
                raise ValueError("Timestamp must be within the source video.")
            if segment_id is not None:
                window.select_segment(segment_id)
            if timestamp is not None:
                window.seek(timestamp)
            window.show()
            window.raise_()

        self.bridge.call(apply)


def start_live_server(window: MainWindow) -> EditorBridge:
    bridge = EditorBridge(window)
    automation = PackAutomation(EditorProjectAccess(bridge), window.analysis_data_root, window.media)
    server = create_server(automation, bridge.begin, bridge.end)

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
