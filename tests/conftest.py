from __future__ import annotations

import os
import traceback
from contextvars import copy_context

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def whisper_base_dialog_default(monkeypatch):
    """Keep the analysis dialog's Whisper tests on Whisper Base; Parakeet tests override this."""
    import sys

    dialog = sys.modules.get("choicer_voicer_pack_creator.ui.analysis_dialog")
    if dialog is None:
        return
    from dataclasses import replace

    from choicer_voicer_pack_creator import analysis

    monkeypatch.setattr(
        dialog, "detect_hardware",
        lambda: replace(analysis.detect_hardware(), recommended_model="base"),
    )


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
