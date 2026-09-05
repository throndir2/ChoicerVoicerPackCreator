from __future__ import annotations

import os
import traceback
from contextvars import copy_context

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def inline_youtube_worker(monkeypatch):
    from choicer_voicer_pack_creator import youtube

    def run(target, args, *, on_event, cancelled, **_kwargs):
        youtube._check_cancel(cancelled)
        parent = copy_context()

        def emit(event, details):
            parent.run(on_event, event, details)

        try:
            result = target(emit, *args)
        except youtube.YouTubeCancelled:
            raise
        except Exception as error:
            raise youtube.ProcessWorkerError(
                str(error), error_type=type(error).__name__,
                remote_traceback=traceback.format_exc(),
            ) from error
        youtube._check_cancel(cancelled)
        return result

    monkeypatch.setattr(youtube, "run_process_worker", run)
    return run
