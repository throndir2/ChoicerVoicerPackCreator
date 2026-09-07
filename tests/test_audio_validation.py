from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import threading
import tracemalloc
from array import array
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import media as media_module
from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.media import (
    DecodedAudioStats,
    MediaError,
    MediaTools,
    _read_audio_samples,
)
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope


def _pcm(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def _expected(values: list[int], rate: int = 48000, threshold: int = 33) -> DecodedAudioStats:
    active = [index for index, value in enumerate(values) if abs(value) >= threshold]
    duration = len(values) / rate
    return DecodedAudioStats(
        duration,
        active[0] / rate if active else duration,
        (len(values) - 1 - active[-1]) / rate if active else duration,
        bool(active),
    )


class ChunkedAudio(io.BytesIO):
    def __init__(self, data: bytes, chunk_size: int = 65536) -> None:
        super().__init__(data)
        self.chunk_size = chunk_size
        self.read_sizes: list[int] = []

    def read(self, size=-1):
        assert 0 < size <= 65536
        self.read_sizes.append(size)
        return super().read(min(size, self.chunk_size))


@pytest.mark.parametrize("chunk_size", [1, 3, 64, 65536])
@pytest.mark.parametrize(
    "values", [[0] * 31, [0, 32, -32, 33, -33, 0], [0] * 32768 + [32767, -32768] + [0] * 32770],
)
def test_streamed_sample_indices_match_full_pcm(values, chunk_size):
    stream = ChunkedAudio(_pcm(values), chunk_size)
    count, first, last = _read_audio_samples(stream, 33)
    active = [index for index, value in enumerate(values) if abs(value) >= 33]
    assert count == len(values)
    assert first == (active[0] if active else None)
    assert last == (active[-1] if active else None)
    assert all(size == 65536 for size in stream.read_sizes)


def test_pcm_reader_rejects_partial_final_sample():
    with pytest.raises(MediaError, match="incomplete PCM sample"):
        _read_audio_samples(ChunkedAudio(b"\0"), 33)


def test_pcm_reader_handles_big_endian_hosts(monkeypatch):
    def big_endian_array(code):
        class BigEndianArray(array):
            def frombytes(self, value):
                super().frombytes(value)
                self.byteswap()

        return BigEndianArray(code)

    monkeypatch.setattr(media_module, "array", big_endian_array)
    monkeypatch.setattr(media_module.sys, "byteorder", "big")
    assert _read_audio_samples(io.BytesIO(_pcm([0, 33, -32768, 0])), 33) == (4, 1, 2)


def test_pcm_reader_memory_does_not_grow_with_audio_duration():
    class RepeatedAudio:
        remaining = 512
        chunk = _pcm([33] * 32768)

        def read(self, size):
            assert size == 65536
            if not self.remaining:
                return b""
            self.remaining -= 1
            return self.chunk

    tracemalloc.start()
    try:
        result = _read_audio_samples(RepeatedAudio(), 33)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == (512 * 32768, 0, 512 * 32768 - 1)
    assert peak < 512 * 1024


@pytest.fixture
def fake_audio_process(monkeypatch):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "fixture-ffmpeg"
    state = SimpleNamespace(
        stream=ChunkedAudio(_pcm([0, 33, -33, 0])),
        returncode=0, stderr=b"", commands=[], closed=False, cleanup_error=None,
    )

    @contextmanager
    def owned(command, **kwargs):
        state.commands.append(command)
        kwargs["stderr"].write(state.stderr)
        process = SimpleNamespace(
            stdout=state.stream, returncode=state.returncode, poll=lambda: state.returncode,
        )
        try:
            yield process
        finally:
            state.closed = True
            state.stream.close()
            if state.cleanup_error:
                raise state.cleanup_error

    monkeypatch.setattr(media_module, "owned_subprocess", owned)
    return media, state


@pytest.mark.parametrize("rate,dbfs", [(48000, -60.0), (8000, -40.0), (44100, 0.0)])
def test_statistics_keep_duration_padding_and_threshold(fake_audio_process, rate, dbfs):
    media, state = fake_audio_process
    values = [0, 32, 33, -33, 400, -32768, 0, 0]
    state.stream = ChunkedAudio(_pcm(values), 3)
    stats = media.decoded_audio_stats(Path("prompt.mp3"), rate, dbfs)
    assert stats == _expected(values, rate, round(32767 * 10 ** (dbfs / 20)))
    assert state.closed


def test_silent_statistics_remain_full_duration(fake_audio_process):
    media, state = fake_audio_process
    state.stream = ChunkedAudio(_pcm([0] * 100))
    assert media.decoded_audio_stats(Path("silent.mp3")) == _expected([0] * 100)


@pytest.mark.parametrize("validated", [False, True])
def test_complete_audio_validation_uses_one_input_and_preserves_all_streams(
    fake_audio_process, monkeypatch, validated,
):
    media, state = fake_audio_process
    monkeypatch.setattr(media_module, "current_ffmpeg_threads", lambda: 1)
    method = media.validated_audio_stats if validated else media.decoded_audio_stats
    method(Path("prompt.mp3"))
    assert len(state.commands) == 1
    command = state.commands[0]
    assert command.count("-i") == 1
    assert command.count("-map") == (2 if validated else 1)
    assert command[command.index("-map") + 1] == "0:a:0"
    if validated:
        assert command[-7:] == ["-map", "0", "-threads", "1", "-f", "null", os.devnull]
    assert command.index("-threads") < command.index("-i")
    assert command[command.index("-filter_threads") + 1] == "1"
    assert command[command.index("-filter_complex_threads") + 1] == "1"
    assert command.count("-threads") == (3 if validated else 2)


def test_nonzero_exit_keeps_error_after_partial_pcm(fake_audio_process):
    media, state = fake_audio_process
    state.returncode = 7
    state.stderr = b"prefix " * 10000 + b"broken input"
    with pytest.raises(MediaError, match="Decoding prompt.mp3 failed: .*broken input"):
        media.validated_audio_stats(Path("prompt.mp3"))
    assert state.closed


def test_empty_decode_remains_an_error(fake_audio_process):
    media, state = fake_audio_process
    state.stream = ChunkedAudio(b"")
    with pytest.raises(MediaError, match="empty.mp3 decoded to no audio samples"):
        media.decoded_audio_stats(Path("empty.mp3"))


def test_reader_failure_is_transferred_and_process_is_closed(fake_audio_process, monkeypatch):
    media, state = fake_audio_process

    def failed_reader(*args):
        raise OSError("PCM reader failed")

    monkeypatch.setattr(media_module, "_read_audio_samples", failed_reader)
    with pytest.raises(OSError, match="PCM reader failed"):
        media.validated_audio_stats(Path("prompt.mp3"))
    assert state.closed


@pytest.mark.parametrize("failure", ["cancel", "callback", "reader-cleanup", "process-cleanup"])
def test_stalled_reader_cancellation_joins_and_preserves_cleanup_failures(monkeypatch, failure):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "fixture-ffmpeg"
    started, released = threading.Event(), threading.Event()
    closed = []

    class StalledAudio:
        def read(self, size):
            started.set()
            assert released.wait(3)
            if failure == "reader-cleanup":
                raise OSError("reader cleanup failed")
            return b""

    @contextmanager
    def owned(*args, **kwargs):
        try:
            yield SimpleNamespace(stdout=StalledAudio(), returncode=0, poll=lambda: None)
        finally:
            released.set()
            closed.append(True)
            if failure == "process-cleanup":
                raise OSError("process cleanup failed")

    def cancelled():
        if failure == "callback" and started.is_set():
            raise ValueError("cancellation callback failed")
        return started.is_set()

    monkeypatch.setattr(media_module, "owned_subprocess", owned)
    expected = {
        "cancel": OperationCancelled, "callback": ValueError,
        "reader-cleanup": OSError, "process-cleanup": OSError,
    }[failure]
    with operation_scope(cancelled), pytest.raises(expected):
        media.validated_audio_stats(Path("prompt.mp3"))
    assert closed == [True]
    assert not any(t.name == "ffmpeg-audio-statistics" for t in threading.enumerate())


def test_audio_launch_failure_remains_visible(monkeypatch):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "missing"

    @contextmanager
    def failed(*args, **kwargs):
        raise FileNotFoundError("FFmpeg missing")
        yield

    monkeypatch.setattr(media_module, "owned_subprocess", failed)
    with pytest.raises(FileNotFoundError, match="FFmpeg missing"):
        media.decoded_audio_stats(Path("prompt.mp3"))


def test_real_child_pcm_and_large_stderr_do_not_deadlock(monkeypatch):
    owned = media_module.owned_subprocess
    processes = []
    script = (
        "import sys; sys.stderr.buffer.write(b'noise'*100000); sys.stderr.flush(); "
        "sys.stdout.buffer.write(b'\\x21\\x00'*65537); sys.stdout.flush()"
    )

    @contextmanager
    def substitute(command, **kwargs):
        with owned([sys.executable, "-u", "-c", script], **kwargs) as process:
            processes.append(process)
            yield process

    monkeypatch.setattr(media_module, "owned_subprocess", substitute)
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "fixture"
    stats = media.validated_audio_stats(Path("prompt.mp3"))
    assert stats == DecodedAudioStats(65537 / 48000, 0, 0, True)
    assert processes[0].poll() is not None and processes[0].stdout.closed


def test_real_child_stalled_output_is_cancellable_and_reaped(monkeypatch):
    owned = media_module.owned_subprocess
    read_samples = media_module._read_audio_samples
    started = threading.Event()
    processes = []

    @contextmanager
    def substitute(command, **kwargs):
        with owned([sys.executable, "-u", "-c", "import time; time.sleep(60)"], **kwargs) as process:
            processes.append(process)
            yield process

    def read(stream, threshold):
        started.set()
        return read_samples(stream, threshold)

    monkeypatch.setattr(media_module, "owned_subprocess", substitute)
    monkeypatch.setattr(media_module, "_read_audio_samples", read)
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "fixture"
    with operation_scope(started.is_set), pytest.raises(OperationCancelled):
        media.validated_audio_stats(Path("prompt.mp3"))
    assert processes[0].poll() is not None and processes[0].stdout.closed

@pytest.mark.parametrize("threads", [None, 1, 2])
def test_prompt_commands_consume_only_explicit_thread_overrides(tmp_path, monkeypatch, threads):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "fixture"
    commands = []
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.png"

    def run(command, _description):
        commands.append(command)
        output.touch()

    def capture(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, _pcm([1000]), b"")

    monkeypatch.setattr(media_module, "current_ffmpeg_threads", lambda: threads)
    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(media, "_capture", capture)
    media.extract_prompt(source, 1, 2, 0.1, 0.2, output)
    media.extract_frame(source, 1, output, size=(640, 360))
    media.convert_image(source, output, 640, 360)
    media.convert_audio(source, output, mono=True)
    media.make_icon(source, output, is_video=True)
    media.create_silent_backing(output, 3)
    media.decode(source)
    assert len(commands) == 8
    for command in commands:
        if threads is None:
            assert "-threads" not in command and "-filter_threads" not in command
        else:
            assert command.count("-threads") == 2
            assert command.index("-threads") < command.index("-i")
            assert command[-3:-1] == ["-threads", str(threads)] or command[-3:] == [
                "-f", "null", os.devnull,
            ]
            for flag in ("-threads", "-filter_threads", "-filter_complex_threads"):
                assert command[command.index(flag) + 1] == str(threads)


def test_cold_prompt_emits_thread_limits_for_each_ffmpeg_step(
    tmp_path, monkeypatch, fake_audio_process,
):
    media, state = fake_audio_process
    state.stream = ChunkedAudio(_pcm([0] * 4800 + [1000] * 19200 + [0] * 9600))
    commands = state.commands

    def run(command, _description):
        commands.append(command)
        Path(command[-1]).touch()

    def capture(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, _pcm([1000]), b"")

    monkeypatch.setattr(media_module, "current_ffmpeg_threads", lambda: 1)
    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(media, "_capture", capture)
    segment = Segment(0.1, 0.5, "Synthetic prompt", ["Speaker"])
    project = PackProject(
        title="Fixture", authors=["Tester"], head_padding=0.1, tail_padding=0.2,
        segments=[segment],
    )
    exporter = PackExporter(media, prompt_workers=1)
    exporter._write_prompt(
        project, segment, 1, tmp_path / "source.mp4", tmp_path, 3, 640, 360, lambda _: None,
    )
    assert len(commands) == 4
    for command in commands:
        assert command.count("-threads") == 2
        assert command.index("-threads") < command.index("-i")
        assert command[command.index("-filter_threads") + 1] == "1"
        assert command[command.index("-filter_complex_threads") + 1] == "1"


@pytest.fixture(scope="module")
def synthetic_audio(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    root = tmp_path_factory.mktemp("audio-validation")
    media = MediaTools()
    valid = root / "valid.mp3"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.4",
        "-af", "adelay=100,apad=pad_dur=0.2", "-ac", "1",
        "-c:a", "libmp3lame", "-threads", "1", str(valid),
    ], "Creating synthetic validation audio")
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=mono", "-t", "0.2",
        "-c:a", "libmp3lame", "-threads", "1", str(root / "silent.mp3"),
    ], "Creating synthetic silent audio")
    content = valid.read_bytes()
    for name, payload in {
        "empty": b"", "header-only": content[:100], "tail-truncated": content[:-20],
        "half-truncated": content[:len(content) // 2],
        "middle-corrupt": content[:1000] + b"\xff" * 700 + content[1700:],
    }.items():
        (root / f"{name}.mp3").write_bytes(payload)
    cover = root / "cover.png"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=16x16",
        "-frames:v", "1", "-threads", "1", str(cover),
    ], "Creating synthetic attached picture")
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-i", str(valid), "-i", str(cover),
        "-map", "0:a", "-map", "1:v", "-c", "copy", "-id3v2_version", "3",
        str(root / "attached-picture.mp3"),
    ], "Attaching synthetic picture to audio")
    return media, root


@pytest.mark.integration
@pytest.mark.parametrize(
    "name", [
        "valid", "silent", "empty", "header-only", "tail-truncated", "half-truncated",
        "middle-corrupt", "attached-picture",
    ],
)
def test_one_pass_matches_previous_two_command_outcomes(synthetic_audio, name):
    media, root = synthetic_audio
    path = root / f"{name}.mp3"
    pcm_command = [
        media.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", "48000", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
    ]
    previous = media._capture(pcm_command)
    previous_decode = media._capture([
        media.ffmpeg, "-v", "error", "-i", str(path), "-map", "0", "-f", "null", os.devnull,
    ])
    if previous.returncode != 0 or previous_decode.returncode != 0 or not previous.stdout:
        with pytest.raises(MediaError):
            media.validated_audio_stats(path)
    else:
        values = list(struct.unpack(f"<{len(previous.stdout) // 2}h", previous.stdout))
        assert media.validated_audio_stats(path) == _expected(values)


@pytest.mark.integration
def test_extra_stream_failure_cannot_be_hidden_by_first_audio_statistics(synthetic_audio):
    media, root = synthetic_audio
    captions = root / "captions.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:00,500\nSynthetic fixture\n", encoding="utf-8")
    path = root / "extra-stream.mkv"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-i", str(root / "valid.mp3"),
        "-i", str(captions), "-map", "0:a", "-map", "1:s",
        "-c:a", "copy", "-c:s", "srt", str(path),
    ], "Creating audio with an additional unsupported null-output stream")
    assert media.decoded_audio_stats(path).has_activity
    with pytest.raises(MediaError):
        media.decode(path)
    with pytest.raises(MediaError):
        media.validated_audio_stats(path)
