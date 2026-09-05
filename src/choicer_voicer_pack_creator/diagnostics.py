from __future__ import annotations

import json
import logging
import platform
import sys
import time
import traceback
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

from choicer_voicer_pack_creator import __version__

LOG_BYTES = 2 * 1024**2
LOG_BACKUPS = 3
_active_log: ContextVar[AnalysisDiagnostics | None] = ContextVar("analysis_diagnostics", default=None)


def analysis_log_path(data_root: Path) -> Path:
    return data_root.resolve() / "logs" / "analysis.log"


class _DiagnosticHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # Windowed builds have no stderr on which logging can report a disk failure.
        raise OSError(f"Could not write diagnostic log: {self.baseFilename}") from sys.exception()


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
                    error_type=type(error).__name__, error=str(error)[-16000:],
                    traceback="".join(traceback.format_exception(error_type, error, tb))[-32000:],
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
        self.logger.info(json.dumps({
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run": self.run_id,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "event": event,
            **details,
        }, ensure_ascii=True))

    def progress(self, message: str, fraction: float | None) -> None:
        now = time.monotonic()
        if fraction is None or fraction in (0, 1) or now - self.last_progress >= 1:
            self.write("progress", message=message, fraction=fraction)
            self.last_progress = now


def diagnostic_event(event: str, **details: Any) -> None:
    log = _active_log.get()
    if log is not None:
        log.write(event, **details)
