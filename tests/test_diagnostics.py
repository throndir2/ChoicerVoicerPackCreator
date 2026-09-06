from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import analysis, diagnostics
from choicer_voicer_pack_creator.analysis import AnalysisCancelled, AnalysisError
from choicer_voicer_pack_creator.diagnostics import (
    AnalysisDiagnostics,
    ApplicationDiagnostics,
    DiagnosticProgress,
    analysis_log_path,
    application_log_path,
    diagnostic_event,
    diagnostic_operation,
    forward_diagnostics,
    save_diagnostic_bundle,
)
from choicer_voicer_pack_creator.ui import analysis_dialog
from choicer_voicer_pack_creator.ui.analysis_dialog import AnalysisDialog, AnalysisWorker
from choicer_voicer_pack_creator.ui.main_window import MainWindow


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_worker_diagnostics_are_redacted_before_forwarding_and_context_is_restored(tmp_path):
    forwarded = []
    with ApplicationDiagnostics(tmp_path):
        with (
            forward_diagnostics(lambda event, details: forwarded.append((event, details))),
            diagnostic_operation("network"),
        ):
            diagnostic_event(
                "request", url="https://youtube.com/watch?signature=private",
                cookie="private", path=Path.home() / "Downloads",
            )
        diagnostic_event("parent_resumed")
    request = next(details for event, details in forwarded if event == "request")
    assert request["url"] == "https://youtube.com/watch"
    assert request["cookie"] == "[redacted]"
    assert request["path"].startswith("<USER>")
    assert request["worker_pid"] > 0
    assert request["worker_operation"]
    saved = records(application_log_path(tmp_path))
    assert any(row["event"] == "parent_resumed" for row in saved)
    assert not any(row["event"] == "request" for row in saved)


def test_live_diagnostics_include_process_state_but_not_normal_transcript_stdout(
    tmp_path, monkeypatch,
):
    child = tmp_path / "child.py"
    child.write_text(
        "import sys, time\n"
        "print('private transcript text', flush=True)\n"
        "print('model loaded; backend=CPU', file=sys.stderr, flush=True)\n"
        "time.sleep(0.5)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(analysis, "DIAGNOSTIC_HEARTBEAT_SECONDS", 0.1)
    path = analysis_log_path(tmp_path)
    observed_before_exit = []

    def output(line):
        if "model loaded" in line:
            observed_before_exit.append(
                any(item["event"] == "process_stderr" for item in records(path))
            )

    with AnalysisDiagnostics(tmp_path) as log:
        log.progress("Loading model", None)
        analysis._run_cancellable(
            [sys.executable, str(child)], "Local Whisper transcription",
            lambda: False, output_line=output, timeout=5,
        )
    data = records(path)
    events = [item["event"] for item in data]
    assert observed_before_exit == [True]
    assert events[0] == "analysis_started"
    assert events[-1] == "analysis_completed"
    assert "process_starting" in events and "process_started" in events
    assert "process_heartbeat" in events
    assert next(item for item in data if item["event"] == "process_exited")["return_code"] == 0
    assert "private transcript text" not in path.read_text(encoding="utf-8")
    assert "model loaded; backend=CPU" in path.read_text(encoding="utf-8")
    assert all(item["time"].endswith("+00:00") for item in data)
    before = path.read_bytes()
    diagnostic_event("outside_session")
    assert path.read_bytes() == before


def test_canceling_retains_last_stage_and_child_termination(tmp_path):
    seen = []
    with pytest.raises(AnalysisCancelled), AnalysisDiagnostics(tmp_path) as log:
        log.progress("Transcribing with Whisper", None)
        analysis._run_cancellable(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(10)"],
            "Local Whisper transcription", lambda: bool(seen),
            output_line=seen.append, timeout=5,
        )
    data = records(analysis_log_path(tmp_path))
    assert data[-1]["event"] == "analysis_canceled"
    assert any(item["event"] == "cancellation_observed" for item in data)
    exited = next(item for item in data if item["event"] == "process_exited")
    assert exited["termination"] in {"terminate", "kill"}
    assert exited["return_code"] is not None


def test_timeout_is_logged_and_next_run_appends_without_erasing_evidence(tmp_path):
    with pytest.raises(AnalysisError, match="time limit"), AnalysisDiagnostics(tmp_path):
        analysis._run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            "Local Whisper transcription", lambda: False, timeout=0.1,
        )
    path = analysis_log_path(tmp_path)
    first = records(path)
    assert first[-1]["event"] == "analysis_failed"
    assert any(item["event"] == "process_timeout" for item in first)
    with AnalysisDiagnostics(tmp_path):
        diagnostic_event("second_run")
    combined = records(path)
    assert combined[:len(first)] == first
    assert combined[-1]["event"] == "analysis_completed"
    assert combined[-1]["run"] != combined[0]["run"]


def test_diagnostic_logs_rotate_with_bounded_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "LOG_BYTES", 1024)
    monkeypatch.setattr(diagnostics, "LOG_BACKUPS", 2)
    with AnalysisDiagnostics(tmp_path):
        for index in range(60):
            diagnostic_event("detail", index=index, message="x" * 150)
    path = analysis_log_path(tmp_path)
    files = sorted(path.parent.iterdir())
    assert len(files) == 3
    assert all(item.stat().st_size <= 1024 for item in files)
    assert records(path)[-1]["event"] == "analysis_completed"


def test_diagnostic_write_failure_is_not_silently_swallowed(tmp_path, monkeypatch):
    handler = diagnostics._DiagnosticHandler(tmp_path / "test.log", encoding="utf-8")

    def fail_write(_value):
        raise OSError("Disk full")

    monkeypatch.setattr(handler.stream, "write", fail_write)
    try:
        with pytest.raises(OSError, match="Could not write diagnostic log"):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "event", (), None))
    finally:
        handler.close()


def test_worker_reports_unwritable_log_location_before_starting_analysis(qtbot, tmp_path, monkeypatch):
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    calls = []
    monkeypatch.setattr(analysis_dialog, "analyze_video", lambda *_args, **_kwargs: calls.append(True))
    worker = AnalysisWorker(
        object(), tmp_path / "video.mp4", 10, tmp_path, "balanced", True, "tiny", "auto",
    )
    errors = []
    worker.failed.connect(errors.append)
    worker.start()
    qtbot.waitUntil(lambda: bool(errors))
    assert worker.wait(1000)
    assert not calls


def test_log_folder_is_available_from_analysis_and_help(qtbot, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(
        analysis_dialog.QDesktopServices, "openUrl", lambda url: opened.append(url) or True
    )
    dialog = AnalysisDialog(object(), tmp_path / "video.mp4", 10, tmp_path, 0)
    qtbot.addWidget(dialog)
    dialog.logs_button.click()
    window = MainWindow(object(), analysis_data_root=tmp_path)
    qtbot.addWidget(window)
    window.action_logs.trigger()
    assert len(opened) == 2
    assert all(url.isLocalFile() for url in opened)
    assert all(Path(url.toLocalFile()) == analysis_log_path(tmp_path).parent for url in opened)
    assert analysis_log_path(tmp_path).parent.is_dir()
    window.dirty = False
    window.close()


def test_application_logs_persist_and_correlate_python_and_analysis_workers(tmp_path):
    path = application_log_path(tmp_path)

    def work():
        with diagnostic_operation("download"):
            diagnostic_event("download_detail")
        with AnalysisDiagnostics(tmp_path):
            diagnostic_event("analysis_detail")

    with ApplicationDiagnostics(tmp_path) as app_log:
        thread = threading.Thread(target=work, name="test-downloader")
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        live = records(path)
        assert live[0]["event"] == "application_started"
        assert all(item["session"] == app_log.session_id for item in live)
        detail = next(item for item in live if item["event"] == "download_detail")
        assert detail["thread"] == "test-downloader"
        assert detail["operation"]
        analysis_detail = next(item for item in live if item["event"] == "analysis_detail")
        assert analysis_detail["analysis_run"] == records(analysis_log_path(tmp_path))[1]["run"]
    first = records(path)
    assert first[-1]["event"] == "application_stopped"
    with ApplicationDiagnostics(tmp_path):
        diagnostic_event("next_launch")
    assert records(path)[:len(first)] == first
    assert records(path)[-1]["session"] != first[0]["session"]
    original = path.read_bytes()
    diagnostic_event("after_shutdown")
    assert path.read_bytes() == original


def test_nested_operations_record_failures_without_swallowing_them(tmp_path):
    with (
        ApplicationDiagnostics(tmp_path), pytest.raises(ValueError, match="bad input"),
        diagnostic_operation("outer"), diagnostic_operation("inner"),
    ):
        raise ValueError("bad input")
    data = records(application_log_path(tmp_path))
    outer = next(item for item in data if item["event"] == "outer_started")
    inner = next(item for item in data if item["event"] == "inner_started")
    assert inner["parent_operation"] == outer["operation"]
    failed = next(item for item in data if item["event"] == "inner_failed")
    assert failed["operation"] == inner["operation"]
    assert "ValueError: bad input" in failed["traceback"]
    assert failed["duration_seconds"] >= 0
    assert not any(item["event"] == "inner_completed" for item in data)


def test_application_error_hooks_log_and_restore_previous_handlers(tmp_path, monkeypatch):
    previous = []
    monkeypatch.setattr(sys, "excepthook", lambda *_args: previous.append("main"))
    monkeypatch.setattr(threading, "excepthook", lambda *_args: previous.append("thread"))
    monkeypatch.setattr(sys, "unraisablehook", lambda *_args: previous.append("unraisable"))
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    notices = []
    with ApplicationDiagnostics(tmp_path, on_error=notices.append):
        try:
            raise RuntimeError("worker startup failed")
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
            threading.excepthook(SimpleNamespace(exc_value=error, thread=threading.current_thread()))
            sys.unraisablehook(SimpleNamespace(exc_value=error))
    assert (sys.excepthook, threading.excepthook, sys.unraisablehook) == hooks
    assert previous == ["main", "thread", "unraisable"]
    assert len(notices) == 2
    data = records(application_log_path(tmp_path))
    assert {"unhandled_exception", "unhandled_thread_exception", "unraisable_exception"} <= {
        item["event"] for item in data
    }


def test_disk_failure_notifies_once_without_breaking_background_work(tmp_path, monkeypatch):
    notices = []
    with ApplicationDiagnostics(tmp_path, on_error=notices.append) as log:
        def fail_write(_value):
            raise OSError("Disk full")

        monkeypatch.setattr(log.handler.stream, "write", fail_write)
        diagnostic_event("first_failed_write")
        diagnostic_event("second_failed_write")
        assert log.failure
        assert len(notices) == 1
        assert "Diagnostic logging stopped" in notices[0]


def test_log_close_failure_is_reported_and_does_not_leave_global_hooks(tmp_path, monkeypatch):
    notices = []
    previous = sys.excepthook
    with ApplicationDiagnostics(tmp_path, on_error=notices.append) as log:
        close = log.handler.close

        def fail_close():
            close()
            raise OSError("Final log flush failed")

        monkeypatch.setattr(log.handler, "close", fail_close)
    assert diagnostics._application_log is None
    assert sys.excepthook is previous
    assert len(notices) == 1
    assert "Final log flush failed" in notices[0]


def test_failed_application_setup_leaves_no_global_logger_or_hooks(tmp_path):
    (tmp_path / "logs").write_text("blocked", encoding="utf-8")
    previous = sys.excepthook
    with pytest.raises(OSError), ApplicationDiagnostics(tmp_path):
        pytest.fail("Unwritable logger must not start")
    assert diagnostics._application_log is None
    assert sys.excepthook is previous


def test_app_retention_and_progress_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "LOG_BYTES", 4096)
    monkeypatch.setattr(diagnostics, "LOG_BACKUPS", 2)
    with ApplicationDiagnostics(tmp_path):
        tracker = DiagnosticProgress("transfer_progress")
        monkeypatch.setattr(diagnostics.time, "monotonic", lambda: 10)
        for index in range(1000):
            tracker.report(f"Downloading {index} MiB", index / 1000)
        tracker.report("Verifying checksum", None)
        tracker.report("Download ready", 1)
        assert len([
            item for item in records(application_log_path(tmp_path))
            if item["event"] == "transfer_progress"
        ]) == 3
        for index in range(100):
            diagnostic_event("detail", index=index, message="x" * 300)
    path = application_log_path(tmp_path)
    files = sorted(path.parent.glob("application.log*"))
    assert len(files) == 3
    assert all(item.stat().st_size <= 4096 for item in files)


def test_bundle_is_allowlisted_redacted_and_usable_during_an_active_run(tmp_path):
    output = tmp_path / "support.zip"
    with ApplicationDiagnostics(tmp_path), AnalysisDiagnostics(tmp_path):
        diagnostic_event(
            "details", token="never-share-me", path=Path.home() / "private-video.mp4",
            url="https://username:password@example.com/runtime?signature=private#fragment",
            message="Authorization: Bearer top-secret",
        )
        folder = application_log_path(tmp_path).parent
        (folder / "private-project.json").write_text("private caption", encoding="utf-8")
        # Old logs are sanitized too, without corrupting their JSON structure.
        (folder / "analysis.log.1").write_text(
            json.dumps({"url": "https://example.com/file?key=legacy-secret"}) + "\n",
            encoding="utf-8",
        )
        save_diagnostic_bundle(tmp_path, output)
        diagnostic_event("after_bundle")
    with zipfile.ZipFile(output) as archive:
        assert {"support-info.json", "application.log", "analysis.log", "analysis.log.1"} <= set(
            archive.namelist()
        )
        assert "private-project.json" not in archive.namelist()
        text = "\n".join(archive.read(name).decode() for name in archive.namelist())
        for secret in ("never-share-me", "username", "password@", "signature", "top-secret",
                       "legacy-secret", "private caption"):
            assert secret not in text
        assert "<USER>" in text
        for name in ("application.log", "analysis.log", "analysis.log.1"):
            assert all(isinstance(json.loads(line), dict) for line in archive.read(name).splitlines())
    assert any(
        item["event"] == "after_bundle" for item in records(application_log_path(tmp_path))
    )


def test_bundle_failure_preserves_existing_destination_and_cleans_staging(tmp_path, monkeypatch):
    output = tmp_path / "support.zip"
    output.write_bytes(b"previous bundle")
    with ApplicationDiagnostics(tmp_path):
        def fail_replace(*_args):
            raise PermissionError("Destination locked")

        monkeypatch.setattr(diagnostics.os, "replace", fail_replace)
        with pytest.raises(PermissionError, match="locked"):
            save_diagnostic_bundle(tmp_path, output)
    assert output.read_bytes() == b"previous bundle"
    assert not list(tmp_path.glob(".cvpc-diagnostics-*"))


def test_bundle_refuses_game_pack_and_log_destinations(tmp_path):
    with ApplicationDiagnostics(tmp_path):
        with pytest.raises(ValueError, match="outside"):
            save_diagnostic_bundle(tmp_path, application_log_path(tmp_path))
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "_pack_info.ini").touch()
        with pytest.raises(ValueError, match="outside"):
            save_diagnostic_bundle(tmp_path, pack / "support.zip")


def test_native_fault_log_lifetime_and_previous_launch_retention(tmp_path, monkeypatch):
    captures, stops = [], []
    monkeypatch.setattr(diagnostics.faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(
        diagnostics.faulthandler, "enable", lambda stream, **_kwargs: captures.append(stream),
    )
    monkeypatch.setattr(diagnostics.faulthandler, "disable", lambda: stops.append(True))
    for _index in range(6):
        with ApplicationDiagnostics(tmp_path):
            captures[-1].write("native failure stack\n")
            captures[-1].flush()
    folder = application_log_path(tmp_path).parent
    files = sorted(folder.glob("crash.log*"))
    assert len(files) == 4
    assert all("native failure stack" in item.read_text() for item in files)
    assert all(stream.closed for stream in captures)
    assert len(stops) == 6


def test_missing_whisper_executable_records_launch_error(tmp_path):
    with ApplicationDiagnostics(tmp_path), pytest.raises(OSError):
        analysis._run_cancellable(
            [str(tmp_path / "missing-whisper.exe")], "Whisper startup", lambda: False,
        )
    data = records(application_log_path(tmp_path))
    error = next(item for item in data if item["event"] == "process_launch_failed")
    assert error["error_type"] == "FileNotFoundError"
    assert error["description"] == "Whisper startup"
    assert "traceback" in error


def test_qt_warnings_are_captured(tmp_path):
    from choicer_voicer_pack_creator.app import _qt_message

    with ApplicationDiagnostics(tmp_path):
        _qt_message(
            SimpleNamespace(name="QtWarningMsg"),
            SimpleNamespace(category="qt.multimedia", file=None, line=0),
            "Could not initialize decoder",
        )
    warning = next(
        item for item in records(application_log_path(tmp_path)) if item["event"] == "qt_message"
    )
    assert warning["category"] == "qt.multimedia"
    assert warning["level"] == "QtWarningMsg"


def test_save_bundle_is_available_from_help_analysis_and_youtube(qtbot, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator.ui.youtube_dialog import YouTubeDialog

    saved = []
    monkeypatch.setattr(
        analysis_dialog.QFileDialog, "getSaveFileName",
        lambda *_args: (str(tmp_path / "support.zip"), ""),
    )
    monkeypatch.setattr(
        analysis_dialog, "save_diagnostic_bundle", lambda root, path: saved.append((root, path)),
    )
    monkeypatch.setattr(analysis_dialog.QMessageBox, "information", lambda *_args: None)
    dialog = AnalysisDialog(object(), tmp_path / "video.mp4", 10, tmp_path, 0)
    youtube = YouTubeDialog(object(), str(tmp_path), data_root=tmp_path)
    window = MainWindow(object(), analysis_data_root=tmp_path)
    for widget in (dialog, youtube, window):
        qtbot.addWidget(widget)
    dialog.save_logs_button.click()
    youtube.save_logs_button.click()
    window.action_save_logs.trigger()
    qtbot.waitUntil(lambda: len(saved) == 3)
    assert saved == [(tmp_path, tmp_path / "support.zip")] * 3
    window.dirty = False
    window.close()


def test_declined_download_is_logged_before_any_analysis_worker_starts(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        analysis_dialog.QMessageBox, "question",
        lambda *_args: analysis_dialog.QMessageBox.StandardButton.Cancel,
    )
    with ApplicationDiagnostics(tmp_path):
        dialog = AnalysisDialog(
            object(), tmp_path / "video.mp4", 10, tmp_path, 0, youtube_import=True,
        )
        qtbot.addWidget(dialog)
        dialog.start_scan()
        assert dialog.worker is None
        assert "not started" in dialog.progress_label.text()
    data = records(application_log_path(tmp_path))
    assert any(item["event"] == "whisper_download_prompt_shown" for item in data)
    consent = next(item for item in data if item["event"] == "whisper_download_consent")
    assert consent["accepted"] is False
    assert not any(item["event"] == "analysis_worker_start_requested" for item in data)


def test_qthread_analysis_failure_reaches_application_log_and_ui(qtbot, tmp_path, monkeypatch):
    def fail_analysis(*_args, **_kwargs):
        raise OSError("Whisper executable was quarantined")

    monkeypatch.setattr(analysis_dialog, "analyze_video", fail_analysis)
    errors = []
    with ApplicationDiagnostics(tmp_path) as log:
        worker = AnalysisWorker(
            object(), tmp_path / "video.mp4", 10, tmp_path, "balanced", True, "tiny", "auto",
        )
        worker.failed.connect(errors.append)
        worker.start()
        qtbot.waitUntil(lambda: bool(errors))
        assert worker.wait(1000)
    assert errors == ["Whisper executable was quarantined"]
    data = records(application_log_path(tmp_path))
    failure = next(item for item in data if item["event"] == "analysis_failed")
    assert failure["session"] == log.session_id
    assert failure["analysis_run"]
    assert "OSError: Whisper executable was quarantined" in failure["traceback"]


def test_real_app_bootstrap_records_startup_qt_and_shutdown_in_persistent_logs(tmp_path):
    code = """
import sys
from choicer_voicer_pack_creator import app
from PySide6.QtCore import qWarning
app.QStandardPaths.writableLocation = lambda location: sys.argv[1]
def run(arguments, application, data_root, **kwargs):
    qWarning("bootstrap diagnostic warning")
    return 7
app._run_application = run
raise SystemExit(app.main(["test-app"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 7, result.stderr
    data = records(application_log_path(tmp_path / "analysis"))
    assert data[0]["event"] == "application_started"
    assert any(item.get("message") == "bootstrap diagnostic warning" for item in data)
    assert data[-1]["event"] == "application_stopped"
    assert data[-1]["exit_code"] == 7
    assert (application_log_path(tmp_path / "analysis").parent / "crash.log").is_file()


def test_long_download_errors_are_redacted_before_any_truncation(tmp_path):
    secret = "private-signed-query" * 3000
    with (
        ApplicationDiagnostics(tmp_path), pytest.raises(OSError),
        AnalysisDiagnostics(tmp_path),
    ):
        raise OSError("Download failed HTTPS://example.com/runtime?signature=" + secret)
    for path in (application_log_path(tmp_path), analysis_log_path(tmp_path)):
        text = path.read_text(encoding="utf-8")
        assert "private-signed-queryprivate-signed-query" not in text
        failure = next(item for item in records(path) if item["event"] == "analysis_failed")
        assert failure["error"] == "Download failed HTTPS://example.com/runtime"
