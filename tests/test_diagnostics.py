from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from choicer_voicer_pack_creator import analysis, diagnostics
from choicer_voicer_pack_creator.analysis import AnalysisCancelled, AnalysisError
from choicer_voicer_pack_creator.diagnostics import (
    AnalysisDiagnostics,
    analysis_log_path,
    diagnostic_event,
)
from choicer_voicer_pack_creator.ui import analysis_dialog
from choicer_voicer_pack_creator.ui.analysis_dialog import AnalysisDialog, AnalysisWorker
from choicer_voicer_pack_creator.ui.main_window import MainWindow


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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
