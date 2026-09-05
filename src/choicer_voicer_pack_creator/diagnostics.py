from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

from choicer_voicer_pack_creator import __version__

LOG_BYTES = 2 * 1024**2
LOG_BACKUPS = 3
_active_log: ContextVar[AnalysisDiagnostics | None] = ContextVar("analysis_diagnostics", default=None)
_operation: ContextVar[str | None] = ContextVar("diagnostic_operation", default=None)
_forward_log: ContextVar[Callable[[str, dict[str, Any]], None] | None] = ContextVar(
    "forward_diagnostics", default=None,
)
_application_log: ApplicationDiagnostics | None = None
_log_lock = threading.RLock()
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|password|(?:access_|refresh_|id_)?token|api[_-]?key|secret)"
    r"(\s*[:=]\s*)([^\r\n,;]+)"
)
_PRIVATE_KEYS = {
    "authorization", "proxy-authorization", "cookie", "cookies", "set-cookie", "password",
    "token", "access_token", "refresh_token", "id_token", "api_key", "api-key", "apikey",
    "x-api-key", "secret", "client_secret",
}


def _safe_detail(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).lower() in _PRIVATE_KEYS else _safe_detail(item)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return diagnostic_text(str(value))


def diagnostic_text(text: str, *, limit: int = 16000) -> str:
    # Drop credentials, signed query strings, and fragments even inside third-party errors.
    text = _URL.sub(
        lambda match: re.sub(
            r"^(https?://)[^/]*@", r"\1[redacted]@",
            re.split(r"[?#]", match.group(), maxsplit=1)[0], flags=re.IGNORECASE,
        ),
        text,
    )
    text = _SECRET.sub(r"\1\2[redacted]", text)
    for home in (str(Path.home()), Path.home().as_posix()):
        text = re.sub(re.escape(home), "<USER>", text, flags=re.IGNORECASE)
    return text[-limit:]


def analysis_log_path(data_root: Path) -> Path:
    return data_root.resolve() / "logs" / "analysis.log"


def application_log_path(data_root: Path) -> Path:
    return analysis_log_path(data_root).with_name("application.log")


def _environment() -> dict[str, Any]:
    packages = {}
    for name in ("PySide6", "yt-dlp", "yt-dlp-ejs", "deno"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "package metadata unavailable"
    return {
        "app_version": __version__, "python": sys.version, "platform": platform.platform(),
        "machine": platform.machine(), "cpu_count": os.cpu_count(),
        "pointer_bits": 64 if sys.maxsize > 2**32 else 32,
        "frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
        "packages": packages,
    }


class _DiagnosticHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # Windowed builds have no stderr on which logging can report a disk failure.
        raise OSError(f"Could not write diagnostic log: {self.baseFilename}") from sys.exception()


class ApplicationDiagnostics:
    """One process-wide sink, including Qt and Python worker threads."""

    def __init__(
        self, data_root: Path, *, on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.path = application_log_path(data_root)
        self.session_id = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.on_error = on_error
        self.failure: str | None = None
        self.exit_code: int | None = None
        self.handler: _DiagnosticHandler | None = None
        self.logger = logging.Logger(f"application-{self.session_id}", logging.INFO)
        self.crash_stream = None
        self.owns_fault_handler = False
        self.old_hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)

    def __enter__(self) -> ApplicationDiagnostics:
        global _application_log
        with _log_lock:
            if _application_log is not None:
                raise RuntimeError("Application diagnostics are already active")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handler = _DiagnosticHandler(
                self.path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
            )
            self.handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(self.handler)
            _application_log = self
            try:
                self.write("application_started", **_environment())
                if self.failure:
                    raise OSError(self.failure)
                if not faulthandler.is_enabled():
                    crash_path = self.path.with_name("crash.log")
                    for index in range(LOG_BACKUPS, 0, -1):
                        source = crash_path if index == 1 else crash_path.with_suffix(f".log.{index - 1}")
                        if source.exists():
                            source.replace(crash_path.with_suffix(f".log.{index}"))
                    self.crash_stream = crash_path.open("w", encoding="utf-8")
                    self.crash_stream.write(
                        f"Session {self.session_id} started {datetime.now(UTC).isoformat()}\n"
                    )
                    self.crash_stream.flush()
                    faulthandler.enable(self.crash_stream, all_threads=True)
                    self.owns_fault_handler = True
                sys.excepthook = self._exception_hook
                threading.excepthook = self._thread_hook
                sys.unraisablehook = self._unraisable_hook
            except BaseException:
                self._close()
                raise
        return self

    def write(self, event: str, **details: Any) -> None:
        with _log_lock:
            if self.failure is not None:
                return  # The UI has been notified; do not recursively fail in exception/Qt hooks.
            try:
                self.logger.info(json.dumps({
                    "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "session": self.session_id, "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "thread_id": threading.get_ident(), "operation": _operation.get(),
                    "elapsed_seconds": round(time.monotonic() - self.started, 3),
                    "event": event, **_safe_detail(details),
                }, ensure_ascii=True))
            except OSError as error:
                self._report_failure(error)

    def _report_failure(self, error: OSError) -> None:
        if self.failure is not None:
            return
        self.failure = str(error)
        if self.on_error is None:
            raise error
        self.on_error(
            f"Diagnostic logging stopped: {error}\n"
            "This run may have incomplete logs. Check disk space and folder permissions."
        )

    def _exception_hook(self, kind: type[BaseException], error: BaseException, tb: TracebackType) -> None:
        diagnostic_exception("unhandled_exception", error)
        if self.on_error:
            self.on_error(f"An unexpected application error occurred: {error}\nLogs: {self.path.parent}")
        self.old_hooks[0](kind, error, tb)

    def _thread_hook(self, args: Any) -> None:
        diagnostic_exception(
            "unhandled_thread_exception", args.exc_value,
            failed_thread=args.thread.name if args.thread else None,
        )
        if self.on_error:
            self.on_error(f"A background task failed unexpectedly: {args.exc_value}\nLogs: {self.path.parent}")
        self.old_hooks[1](args)

    def _unraisable_hook(self, args: Any) -> None:
        diagnostic_exception("unraisable_exception", args.exc_value)
        self.old_hooks[2](args)

    def __exit__(self, kind: Any, error: BaseException | None, tb: Any) -> None:
        try:
            if error is not None:
                diagnostic_exception("application_failed", error)
            self.write("application_stopped", exit_code=self.exit_code, failed=error is not None)
        finally:
            self._close()

    def _close(self) -> None:
        global _application_log
        with _log_lock:
            sys.excepthook, threading.excepthook, sys.unraisablehook = self.old_hooks
            if self.owns_fault_handler:
                faulthandler.disable()
                self.owns_fault_handler = False
            if self.handler is not None:
                self.logger.removeHandler(self.handler)
            resources = (self.crash_stream, self.handler)
            self.crash_stream = self.handler = None
            _application_log = None
            error = None
            for resource in resources:
                if resource is not None:
                    try:
                        resource.close()
                    except OSError as close_error:
                        error = close_error
            if error is not None:
                self._report_failure(error)


class AnalysisDiagnostics:
    def __init__(self, data_root: Path) -> None:
        self.path = analysis_log_path(data_root)
        self.run_id = uuid.uuid4().hex[:12]
        self.logger = logging.Logger(f"analysis-{self.run_id}", logging.INFO)
        self.handler: _DiagnosticHandler | None = None
        self.token: Token[AnalysisDiagnostics | None] | None = None
        self.started = time.monotonic()
        self.last_progress = float("-inf")

    def __enter__(self) -> AnalysisDiagnostics:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = _DiagnosticHandler(
            self.path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)
        self.token = _active_log.set(self)
        try:
            self.write(
                "analysis_started", app_version=__version__, python=sys.version,
                platform=platform.platform(), frozen=bool(getattr(sys, "frozen", False)),
            )
        except OSError:
            self._close()
            raise
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if error is None:
                self.write("analysis_completed")
            else:
                self.write(
                    "analysis_canceled" if type(error).__name__ == "AnalysisCancelled" else "analysis_failed",
                    error_type=type(error).__name__, error=str(error),
                    traceback="".join(traceback.format_exception(error_type, error, tb)),
                )
        finally:
            self._close()

    def _close(self) -> None:
        if self.token is not None:
            _active_log.reset(self.token)
            self.token = None
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None

    def write(self, event: str, **details: Any) -> None:
        with _log_lock:
            if _application_log is not None:
                _application_log.write(event, analysis_run=self.run_id, **details)
            self.logger.info(json.dumps({
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "run": self.run_id,
                "session": _application_log.session_id if _application_log else None,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "event": event,
                **_safe_detail(details),
            }, ensure_ascii=True))

    def progress(self, message: str, fraction: float | None) -> None:
        now = time.monotonic()
        if fraction is None or fraction in (0, 1) or now - self.last_progress >= 1:
            self.write("progress", message=message, fraction=fraction)
            self.last_progress = now


@contextmanager
def forward_diagnostics(callback: Callable[[str, dict[str, Any]], None]) -> Iterator[None]:
    """Forward worker diagnostics to the parent process's single log writer."""
    token = _forward_log.set(callback)
    try:
        yield
    finally:
        _forward_log.reset(token)


def diagnostic_event(event: str, **details: Any) -> None:
    forward = _forward_log.get()
    if forward is not None:
        forward(event, _safe_detail({
            **details, "worker_pid": os.getpid(),
            "worker_thread": threading.current_thread().name,
            "worker_operation": _operation.get(),
        }))
        return
    log = _active_log.get()
    if log is not None:
        log.write(event, **details)
    else:
        app_log = _application_log
        if app_log is not None:
            app_log.write(event, **details)


def diagnostic_exception(event: str, error: BaseException, **details: Any) -> None:
    diagnostic_event(
        event, error_type=type(error).__name__, error=str(error),
        traceback="".join(traceback.format_exception(error)), **details,
    )


@contextmanager
def diagnostic_operation(name: str, **details: Any) -> Iterator[None]:
    parent = _operation.get()
    token = _operation.set(uuid.uuid4().hex[:12])
    started = time.monotonic()
    try:
        diagnostic_event(f"{name}_started", parent_operation=parent, **details)
        try:
            yield
        except BaseException as error:
            suffix = "canceled" if type(error).__name__.endswith(("Cancelled", "Canceled")) else "failed"
            diagnostic_exception(
                f"{name}_{suffix}", error,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            raise
        else:
            diagnostic_event(
                f"{name}_completed", duration_seconds=round(time.monotonic() - started, 3),
            )
    finally:
        _operation.reset(token)


class DiagnosticProgress:
    def __init__(self, event: str) -> None:
        self.event = event
        self.last_time = float("-inf")
        self.last_phase = ""
        self.lock = threading.Lock()

    def report(self, message: str, fraction: float | None) -> None:
        with self.lock:
            now = time.monotonic()
            phase = re.sub(r"\d+(?:\.\d+)?", "#", message)
            if phase != self.last_phase or now - self.last_time >= 1 or fraction == 1:
                diagnostic_event(self.event, message=message, fraction=fraction)
                self.last_phase, self.last_time = phase, now


def save_diagnostic_bundle(data_root: Path, destination: Path) -> Path:
    folder = analysis_log_path(data_root).parent
    destination = destination.resolve()
    if destination.parent == folder or (destination.parent / "_pack_info.ini").is_file():
        raise ValueError("Save the diagnostic ZIP outside the logs and exported pack folders.")
    diagnostic_event("diagnostic_bundle_requested")
    # Snapshot under the same lock used by writers so rollover cannot remove an in-flight file.
    with _log_lock:
        app_log = _application_log
        snapshots = {}
        for name in ("application.log", "analysis.log", "crash.log"):
            for suffix in ("", *(f".{index}" for index in range(1, LOG_BACKUPS + 1))):
                path = folder / f"{name}{suffix}"
                if path.is_symlink():
                    raise OSError(f"Refusing linked diagnostic file: {path}")
                if path.is_file():
                    # Native fatal-error output is not handled by RotatingFileHandler.
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - LOG_BYTES))
                        payload = stream.read(LOG_BYTES)
                    text = payload.decode("utf-8", errors="replace")
                    sanitized = []
                    for line in text.splitlines():
                        if name == "crash.log":
                            sanitized.append(str(_safe_detail(line)))
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            # Retain a partial final write after a crash as explicit evidence.
                            record = {"event": "partial_log_record", "text": line}
                        sanitized.append(json.dumps(_safe_detail(record), ensure_ascii=True))
                    snapshots[path.name] = "\n".join(sanitized) + "\n"
    if not snapshots:
        raise FileNotFoundError(f"No diagnostic logs are available in {folder}")
    handle, temporary = tempfile.mkstemp(prefix=".cvpc-diagnostics-", suffix=".zip", dir=destination.parent)
    stage = Path(temporary)
    try:
        os.close(handle)
        with zipfile.ZipFile(stage, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "support-info.json",
                json.dumps(_safe_detail({
                    **_environment(), "created_utc": datetime.now(UTC).isoformat(),
                    "session": app_log.session_id if app_log else None,
                    "logging_failure": app_log.failure if app_log else None,
                    "notice": (
                        "Review before sharing. Contains local paths and technical errors. "
                        "No media, project files, model files or normal transcript output included. "
                        "URL queries/credentials and the current user's home path are redacted."
                    ),
                }), indent=2) + "\n",
            )
            for name, text in snapshots.items():
                archive.writestr(name, text)
        os.replace(stage, destination)
    finally:
        stage.unlink(missing_ok=True)
    diagnostic_event("diagnostic_bundle_saved", destination=destination, files=len(snapshots))
    return destination
