from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import zipfile
from functools import partial
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt, QThread, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QStyleOptionMenuItem,
)

from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui import update_controller
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.update_controller import UpdateWorker
from choicer_voicer_pack_creator.updates import (
    PreparedUpdate,
    Release,
    UpdateCancelled,
    UpdateError,
)


class UnusedMedia:
    def probe_audio_duration(self, _path: Path) -> float:
        return 1.75


class DialogReplies:
    def __init__(self) -> None:
        self.answers: dict[str, QMessageBox.StandardButton] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.threads: list[QThread] = []

    def respond(self, kind, _parent, title, text, *_args, **_kwargs):
        self.calls.append((kind, title, text))
        self.threads.append(QThread.currentThread())
        default = (
            QMessageBox.StandardButton.Cancel
            if title == "Unsaved changes"
            else QMessageBox.StandardButton.No
        )
        return self.answers.get(title, default)

    @property
    def titles(self) -> list[str]:
        return [title for _kind, title, _text in self.calls]


@pytest.fixture(autouse=True)
def no_external_updates(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unexpected network, browser, or update-helper operation")

    monkeypatch.setattr(update_controller, "find_release", forbidden)
    monkeypatch.setattr(update_controller, "prepare_update", forbidden)
    monkeypatch.setattr(update_controller, "launch_update", forbidden)
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(forbidden))


@pytest.fixture
def dialogs(monkeypatch):
    replies = DialogReplies()
    for kind in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(
            QMessageBox, kind, staticmethod(partial(replies.respond, kind))
        )
    return replies


@pytest.fixture
def make_window(qtbot, tmp_path: Path, dialogs):
    windows: list[MainWindow] = []

    def create(*, automatic=True, prereleases=True):
        settings = QSettings(
            str(tmp_path / f"settings-{len(windows)}.ini"), QSettings.Format.IniFormat
        )
        settings.setValue("updates/automatic", automatic)
        settings.setValue("updates/prereleases", prereleases)
        window = MainWindow(
            UnusedMedia(),  # type: ignore[arg-type]
            settings=settings,
            analysis_data_root=tmp_path / "analysis",
        )
        original_decision = window._show_decision

        def show_decision(box):
            title = box.windowTitle()
            if title.startswith("Unsaved changes -"):
                title = "Unsaved changes"
            answer = dialogs.respond("question", window, title, box.text())
            original_decision(box)
            QTimer.singleShot(0, lambda: box.done(int(answer)))

        window._show_decision = show_decision
        windows.append(window)
        qtbot.addWidget(window)
        window.show()
        qtbot.waitUntil(window.isVisible)
        return window

    yield create

    for window in windows:
        updater = window.updater
        updater.shutting_down = True
        updater.startup_timer.stop()
        updater.prompt_timer.stop()
        if updater.worker is not None:
            updater._cancel_worker()
            qtbot.waitUntil(lambda updater=updater: updater.worker is None)
        updater.prepared = None
        updater.downloaded = None
        window._export_worker = None
        window._commit_editors()
        window.dirty = False
        window.close()


@pytest.fixture
def release():
    return Release(
        version="99.0.0-beta.1",
        tag="v99.0.0-beta.1",
        prerelease=True,
        archive_url="https://github.com/example/releases/download/v99.0.0-beta.1/app.zip",
        archive_size=2 * 1024**2,
        checksum_url="https://github.com/example/releases/download/v99.0.0-beta.1/SHA256SUMS",
        digest="a" * 64,
    )


@pytest.fixture
def prepared(tmp_path: Path, release):
    target = tmp_path / "portable"
    target.mkdir()
    (target / "installed.txt").write_text("original application", encoding="utf-8")
    prepared = PreparedUpdate(tmp_path / ".cvpc-update-test", target, release.version)
    prepared.staged.mkdir(parents=True)
    (prepared.staged / "payload.txt").write_text("verified application", encoding="utf-8")
    (prepared.directory / "backup").mkdir()
    (prepared.directory / "backup" / "original.txt").write_text("backup", encoding="utf-8")
    (prepared.directory / "plan.json").write_text(
        json.dumps({"target": str(target)}), encoding="utf-8"
    )
    return prepared


def run_worker(qtbot, worker: UpdateWorker) -> None:
    try:
        with qtbot.waitSignal(worker.finished, timeout=5000):
            worker.start()
    finally:
        worker.requestInterruption()
        assert worker.wait(5000)


def wait_for_cancellation(cancelled, started: threading.Event) -> None:
    started.set()
    deadline = time.monotonic() + 5
    while not cancelled():
        if time.monotonic() >= deadline:
            raise UpdateError("The test worker was not interrupted")
        time.sleep(0.005)


@pytest.mark.parametrize("has_release", [False, True])
def test_check_worker_delivers_success_off_gui_thread(
    qtbot, monkeypatch, release, has_release
) -> None:
    calls = []

    def find(*, include_prereleases, cancelled):
        calls.append((include_prereleases, cancelled(), threading.get_ident()))
        return release if has_release else None

    monkeypatch.setattr(update_controller, "find_release", find)
    worker = UpdateWorker(include_prereleases=False)
    finished = QSignalSpy(worker.finished)
    run_worker(qtbot, worker)

    assert finished.count() == 1
    assert worker.result is (release if has_release else None)
    assert worker.error == ""
    assert not worker.was_cancelled
    assert calls[0][:2] == (False, False)
    assert calls[0][2] != threading.get_ident()


def test_download_worker_delivers_progress_and_prepared_update(
    qtbot, monkeypatch, release, prepared
) -> None:
    calls = []

    def prepare(found, target, progress, cancelled):
        calls.append((found, target, cancelled(), threading.get_ident()))
        progress("Downloading", 0.25)
        progress("Verified", 1.0)
        return prepared

    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    worker = UpdateWorker(include_prereleases=True, release=release, target=prepared.target)
    progress = QSignalSpy(worker.progress)
    finished = QSignalSpy(worker.finished)
    run_worker(qtbot, worker)

    assert finished.count() == 1
    assert progress.count() == 2
    assert progress.at(0) == ["Downloading", 0.25]
    assert progress.at(1) == ["Verified", 1.0]
    assert worker.result is prepared
    assert worker.error == ""
    assert not worker.was_cancelled
    assert calls[0][:3] == (release, prepared.target, False)
    assert calls[0][3] != threading.get_ident()


@pytest.mark.parametrize(
    "error_type",
    [OSError, ValueError, UpdateError, zipfile.BadZipFile, http.client.HTTPException],
)
@pytest.mark.parametrize("downloading", [False, True], ids=["check", "download"])
def test_worker_reports_errors_and_still_finishes(
    qtbot, monkeypatch, release, tmp_path: Path, error_type, downloading
) -> None:
    def fail(*_args, **_kwargs):
        raise error_type("controlled update failure")

    monkeypatch.setattr(update_controller, "find_release", fail)
    monkeypatch.setattr(update_controller, "prepare_update", fail)
    worker = UpdateWorker(
        include_prereleases=True,
        release=release if downloading else None,
        target=tmp_path,
    )
    finished = QSignalSpy(worker.finished)
    run_worker(qtbot, worker)

    assert finished.count() == 1
    assert worker.result is None
    assert worker.error == "controlled update failure"
    assert not worker.was_cancelled


@pytest.mark.parametrize("downloading", [False, True], ids=["check", "download"])
def test_worker_interruption_is_cancellation_not_an_error(
    qtbot, monkeypatch, release, tmp_path: Path, downloading
) -> None:
    started = threading.Event()

    def cancel(*args, **kwargs):
        cancelled = args[-1] if args else kwargs["cancelled"]
        wait_for_cancellation(cancelled, started)
        raise UpdateCancelled("Canceled")

    monkeypatch.setattr(update_controller, "find_release", cancel)
    monkeypatch.setattr(update_controller, "prepare_update", cancel)
    worker = UpdateWorker(
        include_prereleases=True,
        release=release if downloading else None,
        target=tmp_path,
    )
    finished = QSignalSpy(worker.finished)
    try:
        worker.start()
        qtbot.waitUntil(started.is_set)
        with qtbot.waitSignal(worker.finished, timeout=5000):
            worker.requestInterruption()
    finally:
        worker.requestInterruption()
        assert worker.wait(5000)

    assert finished.count() == 1
    assert worker.result is None
    assert worker.error == ""
    assert worker.was_cancelled


def test_download_worker_rejects_missing_installation(qtbot, release) -> None:
    worker = UpdateWorker(include_prereleases=True, release=release)
    run_worker(qtbot, worker)
    assert worker.result is None
    assert "portable Windows application" in worker.error


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "portable"])
@pytest.mark.parametrize("automatic", [False, True], ids=["disabled", "enabled"])
def test_startup_checks_only_enabled_portable_installations(
    qtbot, monkeypatch, make_window, dialogs, frozen, automatic
) -> None:
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        update_controller, "find_release", lambda **kwargs: calls.append(kwargs)
    )
    window = make_window(automatic=automatic)
    updater = window.updater

    updater.startup()

    assert updater.startup_timer.isActive() is (frozen and automatic)
    assert updater.worker is None
    assert calls == []
    if frozen and automatic:
        assert updater.startup_timer.interval() == 3000
        updater.startup_timer.stop()
        updater.startup_timer.timeout.emit()
        qtbot.waitUntil(lambda: updater.worker is None)
        assert len(calls) == 1
        assert not updater.manual
    elif not automatic:
        updater.check(manual=False)
        assert updater.worker is None
    assert dialogs.calls == []


@pytest.mark.parametrize("include_prereleases", [False, True])
def test_manual_menu_check_uses_and_persists_prerelease_preference(
    qtbot, monkeypatch, make_window, dialogs, include_prereleases
) -> None:
    calls = []
    monkeypatch.setattr(
        update_controller, "find_release", lambda **kwargs: calls.append(kwargs)
    )
    window = make_window(automatic=False, prereleases=not include_prereleases)
    updater = window.updater
    updater.prerelease_action.trigger()
    updater.auto_action.trigger()
    updater.auto_action.trigger()
    updater.startup_timer.start(60000)

    updater.check_action.trigger()

    assert not updater.startup_timer.isActive()
    assert not updater.check_action.isEnabled()
    qtbot.waitUntil(lambda: updater.worker is None)
    assert updater.manual
    assert updater.check_action.isEnabled()
    assert len(calls) == 1
    assert calls[0]["include_prereleases"] is include_prereleases
    assert "No update available" in dialogs.titles
    window.settings.sync()
    restored = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert restored.value("updates/prereleases", type=bool) is include_prereleases
    assert restored.value("updates/automatic", type=bool) is False


@pytest.mark.parametrize("automatic", [False, True], ids=["disabled", "enabled"])
@pytest.mark.parametrize("control", ["mouse", "keyboard"])
def test_update_menu_preferences_show_native_checks_and_persist(
    qtbot, make_window, automatic, control,
) -> None:
    window = make_window(automatic=automatic, prereleases=not automatic)
    menu = window.help_menu
    updater = window.updater
    option = QStyleOptionMenuItem()
    menu.initStyleOption(option, updater.check_action)
    assert option.checkType == QStyleOptionMenuItem.CheckType.NotCheckable
    assert not option.icon.isNull()

    for action, key, initial in (
        (updater.auto_action, "updates/automatic", automatic),
        (updater.prerelease_action, "updates/prereleases", not automatic),
    ):
        option = QStyleOptionMenuItem()
        menu.initStyleOption(option, action)
        assert option.checkType == QStyleOptionMenuItem.CheckType.NonExclusive
        assert option.checked is initial
        assert option.icon.isNull()
        menu.popup(window.menuBar().mapToGlobal(QPoint(0, window.menuBar().height())))
        qtbot.waitUntil(menu.isVisible)
        if control == "mouse":
            qtbot.mouseClick(
                menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center(),
            )
        else:
            menu.setActiveAction(action)
            qtbot.keyClick(menu, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        option = QStyleOptionMenuItem()
        menu.initStyleOption(option, action)
        assert option.checked is not initial
        assert option.icon.isNull()
        window.settings.sync()
        restored = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
        assert restored.value(key, type=bool) is not initial


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
def test_check_failure_is_silent_only_for_automatic_checks(
    qtbot, monkeypatch, make_window, dialogs, manual
) -> None:
    def fail(**_kwargs):
        raise UpdateError("GitHub is unavailable")

    monkeypatch.setattr(update_controller, "find_release", fail)
    window = make_window()
    window.updater.check(manual=manual)
    qtbot.waitUntil(lambda: window.updater.worker is None)

    assert "GitHub is unavailable" in window.statusBar().currentMessage()
    assert window.updater.check_action.isEnabled()
    assert ("Application update unavailable" in dialogs.titles) is manual
    assert window.isVisible()


def test_declining_available_update_never_downloads(
    qtbot, monkeypatch, make_window, dialogs, release, tmp_path: Path
) -> None:
    monkeypatch.setattr(update_controller, "installation_directory", lambda: tmp_path)
    monkeypatch.setattr(update_controller, "find_release", lambda **_kwargs: release)
    window = make_window()
    window.updater.check(manual=True)
    qtbot.waitUntil(lambda: "Update available" in dialogs.titles)

    assert dialogs.titles == ["Update available"]
    assert window.updater.worker is None
    assert window.updater.pending_release is None
    assert window.updater.downloaded is None
    assert window.updater.prepared is None
    assert window.updater.check_action.isEnabled()
    assert not window.updater.prompt_timer.isActive()
    assert window.isVisible()


def test_manual_source_check_can_open_release_page_without_downloading(
    qtbot, monkeypatch, make_window, dialogs, release
) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(update_controller, "find_release", lambda **_kwargs: release)
    opened = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl",
        staticmethod(lambda url: opened.append(url.toString()) or True),
    )
    dialogs.answers["Update available"] = QMessageBox.StandardButton.Yes
    window = make_window()
    window.updater.check_action.trigger()
    qtbot.waitUntil(lambda: bool(opened))

    assert opened == [release.page_url]
    assert "Source installations cannot update in place" in dialogs.calls[0][2]
    assert window.updater.worker is None
    assert window.updater.downloaded is None


def test_verified_download_restart_refusal_removes_staging_not_installed_files(
    qtbot, monkeypatch, make_window, dialogs, release, prepared
) -> None:
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)
    monkeypatch.setattr(update_controller, "find_release", lambda **_kwargs: release)

    def prepare(found, target, progress, cancelled):
        assert found is release
        assert target == prepared.target
        assert not cancelled()
        progress("Verified download", 1.0)
        return prepared

    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    dialogs.answers["Update available"] = QMessageBox.StandardButton.Yes
    window = make_window()
    window.updater.check_action.trigger()
    qtbot.waitUntil(lambda: "Update ready" in dialogs.titles)

    assert dialogs.titles == ["Update available", "Update ready"]
    assert all(thread == window.thread() for thread in dialogs.threads)
    assert not prepared.directory.exists()
    assert (prepared.target / "installed.txt").read_text(encoding="utf-8") == "original application"
    assert window.updater.progress is None
    assert window.updater.worker is None
    assert window.updater.downloaded is None
    assert window.updater.prepared is None
    assert window.updater.check_action.isEnabled()
    assert window.isVisible()


def test_nested_availability_dialog_blocks_reentrant_discovery_before_download(
    qtbot, monkeypatch, make_window, dialogs, release, prepared
) -> None:
    discoveries = []
    downloads = []
    prompt_states = []
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)

    def find(**kwargs):
        discoveries.append(kwargs)
        if len(discoveries) > 1:
            wait_for_cancellation(kwargs["cancelled"], threading.Event())
            raise UpdateCancelled("Unexpected overlapping discovery canceled")
        return release

    def prepare(found, target, _progress, cancelled):
        downloads.append((found, target, cancelled()))
        return prepared

    monkeypatch.setattr(update_controller, "find_release", find)
    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    window = make_window()
    updater = window.updater

    def question(parent, title, text, buttons, default):
        reply = dialogs.respond("question", parent, title, text, buttons, default)
        if title != "Update available":
            return reply
        box = QMessageBox(QMessageBox.Icon.Question, title, text, buttons, parent)
        box.setDefaultButton(default)
        qtbot.addWidget(box)

        def during_prompt():
            # Exercise the startup callback inside the real dialog's nested event loop.
            updater.startup_timer.timeout.emit()
            updater.check(manual=True)
            prompt_states.append((QApplication.activeModalWidget() is box, updater.worker))
            # Do not overwrite an unexpected live worker if the regression returns.
            answer = (
                QMessageBox.StandardButton.No
                if updater.worker is not None
                else QMessageBox.StandardButton.Yes
            )
            box.button(answer).click()

        QTimer.singleShot(0, during_prompt)
        return QMessageBox.StandardButton(box.exec())

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    updater.startup()
    assert updater.startup_timer.isActive()
    updater.check_action.trigger()
    qtbot.waitUntil(lambda: bool(prompt_states))

    assert prompt_states == [(True, None)]
    assert not updater.startup_timer.isActive()
    qtbot.waitUntil(lambda: "Update ready" in dialogs.titles)
    assert len(discoveries) == 1
    assert downloads == [(release, prepared.target, False)]
    assert dialogs.titles == ["Update available", "Update ready"]
    assert all(thread == window.thread() for thread in dialogs.threads)
    assert updater.worker is None
    assert updater.check_action.isEnabled()
    assert not prepared.directory.exists()
    assert window.isVisible()


def test_finished_signal_retires_its_sender_not_a_replacement_worker(
    qtbot, monkeypatch, make_window, dialogs
) -> None:
    started = threading.Event()
    finish = threading.Event()

    def find(*, include_prereleases, cancelled):
        started.set()
        while not finish.wait(0.005):
            if cancelled():
                raise UpdateCancelled("Canceled")
        return None

    monkeypatch.setattr(update_controller, "find_release", find)
    window = make_window()
    updater = window.updater
    original = UpdateWorker(include_prereleases=True)
    replacement = UpdateWorker(include_prereleases=False)
    retired = QSignalSpy(original.destroyed)
    updater._start_worker(original)
    try:
        qtbot.waitUntil(started.is_set)
        # Hold the replacement unstarted so a broken callback cannot destroy a live thread.
        updater.worker = replacement
        finish.set()
        qtbot.waitUntil(lambda: retired.count() == 1)

        assert updater.worker is replacement
        assert not updater.check_action.isEnabled()
        assert dialogs.calls == []
    finally:
        finish.set()
        if not retired.count():
            original.requestInterruption()
            assert original.wait(5000)
        if updater.worker is replacement:
            updater.worker = None


def test_download_cancel_button_discards_late_success_without_restart_prompt(
    qtbot, monkeypatch, make_window, dialogs, release, prepared
) -> None:
    started = threading.Event()
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)

    def prepare(_release, _target, progress, cancelled):
        progress("Downloading", 0.5)
        wait_for_cancellation(cancelled, started)
        # Completion can race with the user pressing Cancel after verification.
        return prepared

    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    dialogs.answers["Update available"] = QMessageBox.StandardButton.Yes
    window = make_window()
    window.updater._offer_release(release)
    qtbot.waitUntil(started.is_set)
    progress = window.updater.progress
    assert progress is not None
    qtbot.waitUntil(lambda: progress.value() == 50)
    cancel_button = progress.findChild(QPushButton)
    assert cancel_button is not None
    qtbot.mouseClick(cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.updater.worker is None)

    assert not prepared.directory.exists()
    assert window.updater.downloaded is None
    assert window.updater.prepared is None
    assert window.updater.progress is None
    assert "Update ready" not in dialogs.titles
    assert "canceled" in window.statusBar().currentMessage()
    assert window.isVisible()


@pytest.mark.parametrize("cancel_via", ["progress", "close"])
def test_cancel_after_worker_exits_discards_queued_result_and_next_check_resets_cancel(
    qtbot, monkeypatch, make_window, dialogs, release, prepared, cancel_via
) -> None:
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)
    entered, allow_result, result_ready = threading.Event(), threading.Event(), threading.Event()

    def prepare(*_args):
        entered.set()
        assert allow_result.wait(5)
        return prepared
    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    monkeypatch.setattr(update_controller, "find_release", lambda **_kwargs: None)
    dialogs.answers["Update available"] = QMessageBox.StandardButton.Yes
    window = make_window()
    updater = window.updater
    updater._offer_release(release)
    worker = updater.worker
    assert worker is not None

    worker.completed.connect(lambda _result: result_ready.set(), Qt.ConnectionType.DirectConnection)
    qtbot.waitUntil(entered.is_set)
    allow_result.set()
    assert result_ready.wait(5)
    assert worker.result is prepared
    assert not worker.was_cancelled
    assert updater.worker is worker
    if cancel_via == "progress":
        assert updater.progress is not None
        updater.progress.canceled.emit()
    else:
        assert not window.close()
        assert "Update task is stopping" in dialogs.titles
    assert updater.cancel_requested
    assert updater.worker is worker
    qtbot.waitUntil(lambda: updater.worker is None)

    assert "Update ready" not in dialogs.titles
    assert "canceled" in window.statusBar().currentMessage()
    assert not prepared.directory.exists()
    assert updater.downloaded is None
    assert updater.prepared is None
    assert updater.progress is None
    assert updater.check_action.isEnabled()
    assert window.isVisible()

    updater.check_action.trigger()
    assert not updater.cancel_requested
    qtbot.waitUntil(lambda: updater.worker is None)
    assert "No update available" in dialogs.titles
    assert "No newer compatible" in window.statusBar().currentMessage()


@pytest.mark.parametrize("blocker", ["export", "modal"])
def test_availability_prompt_waits_until_editor_is_available(
    qtbot, monkeypatch, make_window, dialogs, release, tmp_path: Path, blocker
) -> None:
    monkeypatch.setattr(update_controller, "installation_directory", lambda: tmp_path)
    discoveries = []
    monkeypatch.setattr(
        update_controller, "find_release",
        lambda **kwargs: discoveries.append(kwargs) or release,
    )
    window = make_window()
    modal = QDialog(window)
    qtbot.addWidget(modal)
    if blocker == "export":
        window._export_worker = object()
    else:
        modal.setWindowModality(Qt.WindowModality.ApplicationModal)
        modal.show()
        qtbot.waitUntil(lambda: QApplication.activeModalWidget() is modal)
    updater = window.updater
    updater.prompt_timer.setInterval(10)
    updater.check(manual=False)
    qtbot.waitUntil(lambda: updater.worker is None)

    assert updater.pending_release is release
    assert updater.prompt_timer.isActive()
    assert not updater.check_action.isEnabled()
    assert dialogs.calls == []
    updater.check(manual=False)
    updater.check(manual=True)
    assert updater.worker is None
    assert updater.pending_release is release
    assert len(discoveries) == 1
    window._export_worker = None
    modal.close()
    qtbot.waitUntil(lambda: "Update available" in dialogs.titles)

    assert updater.pending_release is None
    assert not updater.prompt_timer.isActive()
    assert updater.check_action.isEnabled()
    assert dialogs.titles == ["Update available"]


@pytest.mark.parametrize("blocker", ["export", "modal"])
def test_restart_prompt_waits_until_editor_is_available(
    qtbot, make_window, dialogs, prepared, blocker
) -> None:
    window = make_window()
    modal = QDialog(window)
    qtbot.addWidget(modal)
    if blocker == "export":
        window._export_worker = object()
    else:
        modal.setWindowModality(Qt.WindowModality.ApplicationModal)
        modal.show()
        qtbot.waitUntil(lambda: QApplication.activeModalWidget() is modal)
    window.updater.downloaded = prepared

    window.updater._ready_to_install()

    assert dialogs.calls == []
    assert window.updater.downloaded is prepared
    assert prepared.directory.is_dir()
    window._export_worker = None
    modal.close()
    qtbot.waitUntil(lambda: "Update ready" in dialogs.titles, timeout=3000)
    assert window.updater.downloaded is None
    assert not prepared.directory.exists()
    assert window.isVisible()


@pytest.mark.parametrize(
    "answer",
    [
        QMessageBox.StandardButton.Save,
        QMessageBox.StandardButton.Discard,
        QMessageBox.StandardButton.Cancel,
    ],
    ids=["save", "discard", "cancel"],
)
def test_restart_uses_real_close_event_to_protect_unsaved_project(
    qtbot, monkeypatch, make_window, dialogs, prepared, tmp_path: Path, answer
) -> None:
    window = make_window()
    project_path = tmp_path / "project.cvpack.json"
    project = PackProject(title="Saved title", authors=["Creator"])
    ProjectStore.save(project, project_path)
    window._set_project(project, project_path, mark_dirty=False)
    window.title_edit.setText("Unsaved title")
    dialogs.answers["Update ready"] = QMessageBox.StandardButton.Yes
    dialogs.answers["Unsaved changes"] = answer
    launched = []

    def launch(update, reopen_project):
        launched.append((update, reopen_project, ProjectStore.load(project_path).title))

    monkeypatch.setattr(update_controller, "launch_update", launch)
    window.updater.downloaded = prepared

    window.updater._ready_to_install()
    qtbot.waitUntil(lambda: window.updater.prepared is None and not window._closing)
    if answer != QMessageBox.StandardButton.Cancel:
        qtbot.waitUntil(lambda: not window.isVisible())

    assert dialogs.titles == ["Update ready", "Unsaved changes"]
    assert window.project.title == "Unsaved title"
    expected_title = "Unsaved title" if answer == QMessageBox.StandardButton.Save else "Saved title"
    assert ProjectStore.load(project_path).title == expected_title
    if answer == QMessageBox.StandardButton.Cancel:
        assert launched == []
        assert window.isVisible()
        assert window.dirty
        assert not window.updater.shutting_down
        assert not prepared.directory.exists()
    else:
        assert launched == [(prepared, project_path, expected_title)]
        assert not window.isVisible()
        assert window.updater.shutting_down
        assert prepared.directory.is_dir()
    assert window.updater.prepared is None
    assert window.updater.downloaded is None


@pytest.mark.parametrize("save_failure", ["cancel-dialog", "write-error"])
def test_restart_is_aborted_when_requested_save_does_not_complete(
    qtbot, monkeypatch, make_window, dialogs, prepared, tmp_path: Path, save_failure
) -> None:
    window = make_window()
    window.title_edit.setText("Unsaved project")
    dialogs.answers["Update ready"] = QMessageBox.StandardButton.Yes
    dialogs.answers["Unsaved changes"] = QMessageBox.StandardButton.Save
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(
            lambda *_args, **_kwargs: (
                str(tmp_path / "unsaved.cvpack.json") if save_failure == "write-error" else "",
                "",
            )
        ),
    )
    if save_failure == "write-error":
        def fail_save(*_args):
            raise OSError("Disk is full")

        monkeypatch.setattr(ProjectStore, "save", staticmethod(fail_save))
    window.updater.downloaded = prepared

    window.updater._ready_to_install()
    qtbot.waitUntil(lambda: window.updater.prepared is None and not window._closing)

    assert dialogs.titles[:2] == ["Update ready", "Unsaved changes"]
    assert ("Could not save project" in dialogs.titles) is (save_failure == "write-error")
    assert window.isVisible()
    assert window.dirty
    assert window.project_path is None
    assert not window.updater.shutting_down
    assert window.updater.prepared is None
    assert not prepared.directory.exists()


@pytest.mark.parametrize("error_type", [OSError, UpdateError])
def test_helper_launch_failure_keeps_editor_open_and_preserves_staging(
    monkeypatch, make_window, dialogs, prepared, error_type
) -> None:
    window = make_window()
    dialogs.answers["Update ready"] = QMessageBox.StandardButton.Yes
    launched = []

    def fail_launch(update, project):
        launched.append((update, project))
        raise error_type("Helper startup failed")

    monkeypatch.setattr(update_controller, "launch_update", fail_launch)
    window.updater.downloaded = prepared

    window.updater._ready_to_install()

    assert launched == [(prepared, None)]
    assert dialogs.titles == ["Update ready", "Application update unavailable"]
    assert window.isVisible()
    assert not window.updater.shutting_down
    assert window.updater.prepared is None
    assert window.updater.downloaded is None
    assert (prepared.staged / "payload.txt").is_file()
    assert (prepared.directory / "backup" / "original.txt").read_text(encoding="utf-8") == "backup"
    assert window.close()
    assert len(launched) == 1
    assert prepared.directory.is_dir()


def test_normal_close_never_installs_download_before_restart_consent(
    qtbot, make_window, dialogs, prepared
) -> None:
    window = make_window()
    modal = QDialog(window)
    qtbot.addWidget(modal)
    modal.setWindowModality(Qt.WindowModality.ApplicationModal)
    modal.show()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is modal)
    window.updater.downloaded = prepared
    window.updater._ready_to_install()
    assert window.updater.downloaded is prepared
    modal.close()

    assert window.close()

    assert not window.isVisible()
    assert window.updater.shutting_down
    assert window.updater.downloaded is None
    assert not prepared.directory.exists()
    assert (prepared.target / "installed.txt").read_text(encoding="utf-8") == "original application"
    qtbot.wait(1200)
    assert dialogs.calls == []


@pytest.mark.parametrize("downloading", [False, True], ids=["check", "download"])
def test_normal_close_interrupts_active_worker_and_waits_for_safe_retry(
    qtbot, monkeypatch, make_window, dialogs, release, prepared, downloading
) -> None:
    started = threading.Event()

    def cancel(*args, **kwargs):
        cancelled = args[-1] if args else kwargs["cancelled"]
        wait_for_cancellation(cancelled, started)
        raise UpdateCancelled("Canceled")

    monkeypatch.setattr(update_controller, "find_release", cancel)
    monkeypatch.setattr(update_controller, "prepare_update", cancel)
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)
    dialogs.answers["Update available"] = QMessageBox.StandardButton.Yes
    window = make_window()
    if downloading:
        window.updater._offer_release(release)
    else:
        window.updater.check(manual=True)
    qtbot.waitUntil(started.is_set)
    worker = window.updater.worker
    assert worker is not None
    finished = QSignalSpy(worker.finished)

    assert not window.close()

    assert window.isVisible()
    assert window.updater.cancel_requested
    assert "Update task is stopping" in dialogs.titles
    qtbot.waitUntil(lambda: window.updater.worker is None)
    assert finished.count() == 1
    assert window.updater.progress is None
    assert window.updater.check_action.isEnabled()
    assert "canceled" in window.statusBar().currentMessage()
    assert "Application update unavailable" not in dialogs.titles
    assert window.close()
    assert window.updater.shutting_down
    assert not window.isVisible()


def test_failed_update_result_retains_payload_and_rollback_backups(
    qtbot, monkeypatch, make_window, dialogs, prepared
) -> None:
    monkeypatch.setattr(update_controller, "installation_directory", lambda: prepared.target)
    (prepared.directory / "result.json").write_text(
        json.dumps({"success": False, "message": "Rollback needs manual recovery"}),
        encoding="utf-8",
    )
    window = make_window(automatic=False)

    window.updater.startup(prepared.directory)

    assert dialogs.titles == ["Application update failed"]
    assert str(prepared.directory) in dialogs.calls[0][2]
    assert "Rollback needs manual recovery" in window.statusBar().currentMessage()
    assert not window.updater.startup_timer.isActive()
    qtbot.wait(1700)
    assert (prepared.directory / "backup" / "original.txt").read_text(encoding="utf-8") == "backup"
    assert (prepared.staged / "payload.txt").is_file()
    assert window.close()
    assert prepared.directory.is_dir()
