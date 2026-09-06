from __future__ import annotations

import http.client
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QProgressDialog

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.diagnostics import (
    DiagnosticProgress,
    diagnostic_event,
    diagnostic_exception,
    diagnostic_operation,
)
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.updates import (
    RELEASES_URL,
    PreparedUpdate,
    Release,
    UpdateCancelled,
    UpdateError,
    find_release,
    installation_directory,
    launch_update,
    prepare_update,
    read_update_result,
)

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UpdateWorker(JobWorker):
    progress = Signal(str, float)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()

    def __init__(
        self, *, include_prereleases: bool, release: Release | None = None,
        target: Path | None = None,
    ) -> None:
        super().__init__()
        self.include_prereleases = include_prereleases
        self.release = release
        self.target = target
        self.result: Release | PreparedUpdate | None = None
        self.error = ""
        self.was_cancelled = False

    def run(self) -> None:
        logged_progress = DiagnosticProgress("update_progress")

        def report(message: str, fraction: float) -> None:
            logged_progress.report(message, fraction)
            self.progress.emit(message, fraction)

        try:
            if self.release is None:
                with diagnostic_operation(
                    "update_check", include_prereleases=self.include_prereleases,
                    current_version=__version__,
                ):
                    self.result = find_release(
                        include_prereleases=self.include_prereleases,
                        cancelled=self.isInterruptionRequested,
                    )
                    diagnostic_event(
                        "update_check_result", available=self.result is not None,
                        version=self.result.version if self.result else None,
                    )
            else:
                with diagnostic_operation(
                    "update_prepare", version=self.release.version, target=self.target,
                    archive_bytes=self.release.archive_size,
                ):
                    if self.target is None:
                        raise UpdateError("In-place updates require a portable Windows application.")
                    diagnostic_event(
                        "update_download_requested", version=self.release.version,
                        archive_bytes=self.release.archive_size,
                    )
                    self.result = prepare_update(
                        self.release, self.target, report, self.isInterruptionRequested
                    )
                    diagnostic_event(
                        "update_prepared", directory=self.result.directory,
                        target=self.result.target, version=self.result.version,
                    )
            self.completed.emit(self.result)
        except UpdateCancelled as error:
            diagnostic_exception("update_worker_canceled", error)
            self.was_cancelled = True
            self.canceled.emit()
        except (OSError, ValueError, UpdateError, zipfile.BadZipFile, http.client.HTTPException) as error:
            diagnostic_exception("update_worker_failed", error, preparing=self.release is not None)
            self.error = str(error)
            self.failed.emit(self.error)


class UpdateController(QObject):
    def __init__(self, window: MainWindow, menu: QMenu) -> None:
        super().__init__(window)
        self.window = window
        self.worker: UpdateWorker | None = None
        self.progress: QProgressDialog | None = None
        self.prepared: PreparedUpdate | None = None
        self.downloaded: PreparedUpdate | None = None
        self.pending_release: Release | None = None
        self.prompt_active = False
        self.cancel_requested = False
        self.manual = False
        self.shutting_down = False
        self.check_action = menu.addAction("Check for Updates...")
        self.check_action.triggered.connect(lambda: self.check(manual=True))
        self.auto_action = menu.addAction("Check for Updates on Startup")
        self.auto_action.setCheckable(True)
        self.auto_action.setChecked(window.settings.value("updates/automatic", True, type=bool))
        self.auto_action.toggled.connect(
            lambda enabled: window.settings.setValue("updates/automatic", enabled)
        )
        self.prerelease_action = menu.addAction("Include Prereleases")
        self.prerelease_action.setCheckable(True)
        self.prerelease_action.setChecked(
            window.settings.value("updates/prereleases", True, type=bool)
        )
        self.prerelease_action.toggled.connect(
            lambda enabled: window.settings.setValue("updates/prereleases", enabled)
        )
        menu.addSeparator()
        self.prompt_timer = QTimer(self)
        self.prompt_timer.setInterval(1000)
        self.prompt_timer.timeout.connect(self._offer_pending_release)
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.timeout.connect(lambda: self.check(manual=False))

    def startup(self, result_directory: Path | None = None) -> None:
        target = installation_directory()
        diagnostic_event(
            "update_startup", target=target, result_directory=result_directory,
            automatic=self.auto_action.isChecked(),
        )
        if target is None:
            return  # Source checkouts offer manual discovery, never self-replacement.
        if result_directory is not None:
            self._show_result(result_directory, target)
        if self.auto_action.isChecked():
            self.startup_timer.start(3000)

    def check(self, *, manual: bool) -> None:
        if (
            self.worker or self.prepared or self.downloaded or self.pending_release
            or self.prompt_active or self.shutting_down
        ):
            diagnostic_event("update_check_skipped", manual=manual, reason="busy_or_shutting_down")
            return
        if not manual and not self.auto_action.isChecked():
            diagnostic_event("update_check_skipped", manual=manual, reason="automatic_disabled")
            return
        diagnostic_event(
            "update_check_requested", manual=manual,
            include_prereleases=self.prerelease_action.isChecked(),
        )
        self.manual = manual
        if manual:
            self.startup_timer.stop()
        self.window.statusBar().showMessage("Checking GitHub for updates...")
        self._start_worker(UpdateWorker(include_prereleases=self.prerelease_action.isChecked()))

    def _start_worker(self, worker: UpdateWorker) -> None:
        self.cancel_requested = False
        self.worker = worker
        self.check_action.setEnabled(False)
        worker.progress.connect(self._progress_changed)
        worker.finished.connect(self._worker_finished)
        worker.configure_job(
            self.window.job_manager, None, "update",
            "Downloading application update" if worker.release is not None else "Checking for updates",
            resource_class="network", resource_keys=("application-update",),
        )
        diagnostic_event("update_worker_launching", preparing=worker.release is not None)
        worker.start()

    def _progress_changed(self, message: str, fraction: float) -> None:
        if self.progress:
            self.progress.setLabelText(message)
            self.progress.setValue(min(99, int(fraction * 100)))

    @Slot()
    def _worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, UpdateWorker):
            raise TypeError("Update completion must come from an update worker.")
        self._finished(worker)

    def _finished(self, worker: UpdateWorker) -> None:
        if worker is not self.worker:
            worker.deleteLater()
            return
        canceled = self.cancel_requested or worker.was_cancelled
        diagnostic_event(
            "update_worker_finished", canceled=canceled, failed=bool(worker.error),
            result_type=type(worker.result).__name__, preparing=worker.release is not None,
        )
        self.worker = None
        self.check_action.setEnabled(True)
        if self.progress:
            self.progress.close()
            self.progress.deleteLater()
            self.progress = None
        worker.deleteLater()
        if canceled:
            if isinstance(worker.result, PreparedUpdate):
                self._discard(worker.result.directory)
            self.window.statusBar().showMessage("Update canceled; application files were not changed.")
        elif worker.error:
            self.window.statusBar().showMessage(f"Update check/download failed: {worker.error}")
            if self.manual or worker.release is not None:
                self._show_error(worker.error, worker.release)
        elif isinstance(worker.result, PreparedUpdate):
            self.downloaded = worker.result
            self._ready_to_install()
        elif isinstance(worker.result, Release):
            self.pending_release = worker.result
            self.check_action.setEnabled(False)
            self.prompt_timer.start()
            self._offer_pending_release()
        else:
            self.window.statusBar().showMessage("No newer compatible GitHub release is available.")
            if self.manual:
                QMessageBox.information(
                    self.window, "No update available",
                    f"You are running {__version__}. No newer compatible release was found.",
                )

    def _offer_pending_release(self) -> None:
        if self.shutting_down or self.pending_release is None:
            self.prompt_timer.stop()
            return
        if QApplication.activeModalWidget() or self.window._export_worker or not self.window.isVisible():
            return
        release = self.pending_release
        self.pending_release = None
        self.prompt_timer.stop()
        self.prompt_active = True
        try:
            self._offer_release(release)
        finally:
            self.prompt_active = False
            self.check_action.setEnabled(self.worker is None)

    def _offer_release(self, release: Release) -> None:
        target = installation_directory()
        diagnostic_event(
            "update_release_offered", version=release.version, prerelease=release.prerelease,
            target=target, archive_bytes=release.archive_size,
        )
        message = (
            f"Version {release.version}{' (prerelease)' if release.prerelease else ''} "
            f"is available on GitHub. You are running {__version__}.\n\n"
        )
        if target is None:
            message += "Source installations cannot update in place. Open the release page?"
        else:
            message += (
                f"Download the update ({release.archive_size / 1024**2:.1f} MiB)?\n\n"
                "You can keep working until it is ready to restart. Unsaved changes must be "
                "saved or explicitly discarded before restarting. Projects, media, preferences, "
                "and extra files are preserved. Locally modified app files are not overwritten."
            )
        answer = QMessageBox.question(
            self.window, "Update available", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            diagnostic_event("update_release_declined", version=release.version)
            return
        if target is None:
            self._open_release_page(release.page_url)
            return
        self.progress = QProgressDialog("Preparing update...", "Cancel", 0, 100, self.window)
        self.progress.setWindowTitle("Downloading application update")
        self.progress.setWindowModality(Qt.WindowModality.NonModal)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        worker = UpdateWorker(
            include_prereleases=self.prerelease_action.isChecked(), release=release, target=target
        )
        self.progress.canceled.connect(self._cancel_worker)
        self.progress.show()
        self._start_worker(worker)

    def _ready_to_install(self) -> None:
        prepared = self.downloaded
        if prepared is None or self.shutting_down:
            return
        # Wait for import/analysis dialogs and exports before offering a restart.
        if (
            QApplication.activeModalWidget() or self.window.job_manager.active_jobs()
            or any(editor._export_worker for editor in self.window.editors.values())
        ):
            QTimer.singleShot(1000, self._ready_to_install)
            return
        answer = QMessageBox.question(
            self.window, "Update ready",
            f"Version {prepared.version} is downloaded and verified. Restart and update now?\n\n"
            "The usual Save / Discard / Cancel prompt will protect unsaved edits.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self.downloaded = None
        diagnostic_event(
            "update_restart_decision", version=prepared.version,
            accepted=answer == QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.prepared = prepared
            if self.window.close() or self.window._closing:
                return
            if self.prepared is None:
                return  # A launched helper owns the staging folder, including on startup failure.
        self.prepared = None
        self._discard(prepared.directory)
        self.window.statusBar().showMessage("Update postponed; the installed application is unchanged.")

    def cancel_close_update(self) -> None:
        if self.prepared is not None:
            prepared, self.prepared = self.prepared, None
            self._discard(prepared.directory)
            self.window.statusBar().showMessage(
                "Update postponed; the installed application is unchanged."
            )

    def can_close(self) -> bool:
        if self.worker:
            self._cancel_worker()
            QMessageBox.information(
                self.window, "Update task is stopping",
                "The update check/download is being canceled. Please close again when it stops.",
            )
            return False
        return True

    def _cancel_worker(self) -> None:
        if self.worker:
            diagnostic_event("update_cancel_requested", preparing=self.worker.release is not None)
            self.cancel_requested = True
            self.worker.requestInterruption()

    def install_on_close(self) -> bool:
        if self.prepared:
            prepared = self.prepared
            # Once a helper may be running, leave its workspace intact even on launch failure.
            self.prepared = None
            try:
                with diagnostic_operation(
                    "update_handoff", directory=prepared.directory,
                    target=prepared.target, version=prepared.version,
                    project_path=self.window.project_path,
                ):
                    launch_update(prepared, self.window.project_path)
            except (OSError, ValueError, UpdateError) as error:
                diagnostic_exception("update_handoff_failed_in_controller", error)
                self._show_error(str(error))
                return False
        if self.downloaded:
            self._discard(self.downloaded.directory)
            self.downloaded = None
        self.shutting_down = True
        self.startup_timer.stop()
        self.prompt_timer.stop()
        return True

    def _show_error(self, message: str, release: Release | None = None) -> None:
        answer = QMessageBox.warning(
            self.window, "Application update unavailable",
            f"{message}\n\nThe editor will stay open. Open GitHub for a manual update?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._open_release_page(release.page_url if release else RELEASES_URL)

    def _open_release_page(self, url: str) -> None:
        opened = QDesktopServices.openUrl(QUrl(url))
        diagnostic_event("update_release_page_opened", url=url, opened=opened)
        if not opened:
            QMessageBox.warning(self.window, "Could not open browser", f"Open this URL manually:\n{url}")

    def _show_result(self, directory: Path, target: Path) -> None:
        diagnostic_event("update_result_requested", directory=directory, target=target)
        try:
            success, message = read_update_result(directory, target)
        except (OSError, ValueError, UpdateError) as error:
            diagnostic_exception("update_result_read_failed", error, directory=directory, target=target)
            QMessageBox.warning(self.window, "Could not read update result", str(error))
            return
        diagnostic_event(
            "update_result", success=success, message=message, directory=directory, target=target,
        )
        self.window.statusBar().showMessage(message)
        if success:
            QTimer.singleShot(1500, lambda: self._discard(directory, retries=10))
        else:
            QMessageBox.warning(
                self.window, "Application update failed",
                f"{message}\n\nUpdate files and any rollback backups were retained at:\n{directory}",
            )

    def _discard(self, directory: Path, *, retries: int = 0) -> None:
        try:
            shutil.rmtree(directory)
        except OSError as error:
            diagnostic_exception(
                "update_cleanup_failed", error, directory=directory, retries_remaining=retries,
            )
            if retries:
                QTimer.singleShot(1000, lambda: self._discard(directory, retries=retries - 1))
            else:
                self.window.statusBar().showMessage(
                    f"Could not clean temporary update files at {directory}: {error}"
                )
        else:
            diagnostic_event("update_cleanup_completed", directory=directory)
