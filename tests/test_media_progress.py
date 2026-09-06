from __future__ import annotations

import subprocess
import sys

import pytest

from choicer_voicer_pack_creator import media as media_module
from choicer_voicer_pack_creator.media import MediaError, MediaTools, VideoEncodingProgress


def test_parses_ffmpeg_progress_and_unavailable_startup_measurements():
    assert VideoEncodingProgress.from_fields({
        "frame": "120", "fps": " 23.5 ", "speed": " 1.25x",
    }) == VideoEncodingProgress(120, 23.5, 1.25)
    assert VideoEncodingProgress.from_fields({
        "frame": "0", "fps": "0.00", "speed": "N/A",
    }) == VideoEncodingProgress(0, 0, None)
    assert VideoEncodingProgress.from_fields({}) == VideoEncodingProgress(None, None, None)


@pytest.mark.parametrize("value", ["garbage", "nan", "inf", "-1"])
def test_invalid_ffmpeg_measurements_are_explicit_errors(value):
    with pytest.raises(MediaError, match="Invalid FFmpeg progress field"):
        VideoEncodingProgress.from_fields({"frame": value})


def test_progress_arrives_before_process_exit_and_stderr_cannot_block_it(tmp_path):
    acknowledgement = tmp_path / "progress-received"
    script = """
import pathlib, sys, time
ack = pathlib.Path(sys.argv[1])
sys.stderr.write("diagnostics " * 50000)
sys.stderr.flush()
print("frame=30\\nfps=15\\nspeed=0.5x\\nprogress=continue", flush=True)
deadline = time.monotonic() + 5
while not ack.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not ack.exists():
    sys.stderr.write("No live callback arrived")
    sys.exit(3)
print("frame=60\\nfps=30\\nspeed=1x\\nprogress=end", flush=True)
"""
    updates = []

    def on_progress(update):
        updates.append(update)
        acknowledgement.write_text("received", encoding="utf-8")

    media = MediaTools.__new__(MediaTools)
    media._run_video_conversion(
        [sys.executable, "-u", "-c", script, str(acknowledgement)], on_progress,
    )
    assert updates == [VideoEncodingProgress(30, 15, 0.5), VideoEncodingProgress(60, 30, 1)]


def test_nonzero_exit_after_progress_preserves_error_details():
    media = MediaTools.__new__(MediaTools)
    updates = []
    with pytest.raises(MediaError, match="encoder failed.*exit code 7"):
        media._run_video_conversion([
            sys.executable, "-u", "-c",
            "import sys; print('frame=5\\nprogress=end', flush=True); "
            "sys.stderr.write('encoder failed'); sys.exit(7)",
        ], updates.append)
    assert updates[0].frames == 5


def test_callback_failure_reaps_the_child_process(monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    def fail(_update):
        raise ValueError("callback failed")

    monkeypatch.setattr(media_module.subprocess, "Popen", start)
    media = MediaTools.__new__(MediaTools)
    with pytest.raises(ValueError, match="callback failed"):
        media._run_video_conversion([
            sys.executable, "-u", "-c",
            "import time; print('frame=1\\nprogress=continue', flush=True); time.sleep(60)",
        ], fail)
    assert children[0].poll() is not None
    assert children[0].stdout.closed


def test_launch_failure_propagates():
    media = MediaTools.__new__(MediaTools)
    with pytest.raises(FileNotFoundError):
        media._run_video_conversion(["missing-export-ffmpeg-executable"], None)
