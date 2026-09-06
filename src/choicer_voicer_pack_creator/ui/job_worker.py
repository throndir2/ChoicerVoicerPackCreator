from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Slot

from choicer_voicer_pack_creator.export_progress import ExportProgress
from choicer_voicer_pack_creator.operations import OperationCancelled

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.jobs import JobContext, JobHandle, JobManager


class JobWorker(QThread):
    """Keep worker signal contracts while running configured work in the shared scheduler.

    An unconfigured worker retains its standalone QThread behavior. In the application,
    the manager owns the bound operation until completion; no native QThread is started.
    """

    def __init__(self) -> None:
        super().__init__()
        self.job_handle: JobHandle | None = None
        self._job_manager: JobManager | None = None
        self._job_options: dict = {}
        self._job_context: JobContext | None = None
        self._job_error = ""
        self._job_exception: BaseException | None = None
        self._job_cancel_emitted = False
        self._job_result: object = None

    def configure_job(
        self, manager: JobManager, project_id: str | None, kind: str, title: str, *,
        resource_class: str = "cpu", read_paths: Sequence[Path] = (),
        write_paths: Sequence[Path] = (), resource_keys: Sequence[str] = (),
        source_snapshot: Mapping | None = None,
    ) -> None:
        self._job_manager = manager
        self._job_options = {
            "project_id": project_id, "kind": kind, "title": title,
            "resource_class": resource_class, "read_paths": tuple(read_paths),
            "write_paths": tuple(write_paths), "resource_keys": tuple(resource_keys),
            "source_snapshot": source_snapshot,
        }
        if hasattr(self, "progress"):
            self.progress.connect(self._report_job_progress, Qt.ConnectionType.DirectConnection)
        if hasattr(self, "failed"):
            self.failed.connect(self._capture_error, Qt.ConnectionType.DirectConnection)
        if hasattr(self, "completed"):
            self.completed.connect(self._capture_result, Qt.ConnectionType.DirectConnection)
        if hasattr(self, "canceled"):
            self.canceled.connect(self._capture_cancelled, Qt.ConnectionType.DirectConnection)
        if hasattr(self, "download_required"):
            self.download_required.connect(
                self._capture_setup_required, Qt.ConnectionType.DirectConnection,
            )

    def _report_job_progress(self, *values: object) -> None:
        context = self._job_context
        if context is None:
            return
        if len(values) == 1 and isinstance(values[0], ExportProgress):
            update = values[0]
            context.report(update.message, update.fraction, detail=update)
        elif len(values) == 2:
            message, value = values
            fraction = None if value is None or float(value) < 0 else (
                float(value) if isinstance(value, float) else float(value) / 1000
            )
            context.report(str(message), fraction)

    def _capture_error(self, *values: object) -> None:
        self._job_error = str(values[-1])
        self._job_exception = sys.exception()

    def _capture_cancelled(self) -> None:
        self._job_cancel_emitted = True
        error = sys.exception()
        self._job_exception = (
            error if isinstance(error, OperationCancelled) else
            OperationCancelled(str(error) if error else "Operation cancelled")
        )

    def _capture_setup_required(self) -> None:
        self._job_exception = sys.exception() or RuntimeError("Download consent required")
        self._job_error = str(self._job_exception)

    def _capture_result(self, *values: object) -> None:
        self._job_result = values[0] if len(values) == 1 else values

    def _execute_job(self, context: JobContext) -> object:
        self._job_context = context
        try:
            context.check_cancelled()
            self.run()
            if self._job_exception is not None:
                raise self._job_exception
            if self._job_error:
                raise RuntimeError(self._job_error)
            return self._job_result
        finally:
            self._job_context = None
            # Tracebacks include the executor's manager frame after re-raising.
            self._job_exception = None

    def start(self, priority=QThread.Priority.InheritPriority) -> None:
        if self._job_manager is None:
            super().start(priority)
            return
        if self.job_handle is not None:
            raise RuntimeError("A managed worker cannot be started twice")
        self.job_handle = self._job_manager.submit(
            operation=self._execute_job, **self._job_options,
        )
        self.job_handle.finished.connect(self._job_finished)

    @Slot()
    def _job_finished(self) -> None:
        record = self.job_handle.record
        if record.state == "cancelled" and not self._job_cancel_emitted and hasattr(self, "canceled"):
            self.canceled.emit()
        elif record.state in {"failed", "blocked"} and not self._job_error and hasattr(self, "failed"):
            self._emit_job_failure(record.error or record.message)
        self.finished.emit()

    def _emit_job_failure(self, message: str) -> None:
        self.failed.emit(message)

    def isInterruptionRequested(self) -> bool:  # noqa: N802
        if self._job_context is not None:
            return self._job_context.cancelled()
        if self.job_handle is not None:
            return self.job_handle.record.cancel_requested
        return super().isInterruptionRequested()

    def requestInterruption(self) -> None:  # noqa: N802
        if self.job_handle is not None:
            self.job_handle.cancel()
        else:
            super().requestInterruption()

    def isRunning(self) -> bool:  # noqa: N802
        if self.job_handle is not None:
            return self.job_handle.record.active
        return super().isRunning()

    def wait(self, time: int = 0xFFFFFFFF) -> bool:
        if self.job_handle is not None:
            return not self.job_handle.record.active
        return super().wait(time)
