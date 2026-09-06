from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.media import MediaError, MediaTools
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope


@pytest.fixture
def recording_media(monkeypatch):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "ffmpeg"
    commands = []

    def run(command, description):
        commands.append((command, description))
        Path(command[-1]).write_bytes(b"image")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(media, "run", run)
    return media, commands


@pytest.mark.parametrize("timestamp,expected", [(12.3456789, "12.345679"), (-2, "0.000000")])
def test_extract_frame_uses_accurate_input_seeking(
    recording_media, tmp_path, timestamp, expected,
):
    media, commands = recording_media
    source = tmp_path / "source video.mp4"
    destination = tmp_path / "frames" / "prompt.png"

    media.extract_frame(source, timestamp, destination)

    assert destination.is_file()
    assert len(commands) == 1
    command, description = commands[0]
    assert command == [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", expected, "-i", str(source), "-frames:v", "1", str(destination),
    ]
    assert description == f"Extracting frame at {timestamp:.3f}s"


def test_extract_frame_scales_and_crops_in_one_command(recording_media, tmp_path):
    media, commands = recording_media
    destination = tmp_path / "prompt.png"

    media.extract_frame(tmp_path / "source.mp4", 1.25, destination, size=(854, 480))

    assert len(commands) == 1
    command, _description = commands[0]
    assert command[command.index("-vf") + 1] == (
        "scale=854:480:force_original_aspect_ratio=increase,crop=854:480"
    )
    assert command[-1] == str(destination)


def test_extract_frame_requires_a_destination_file(recording_media, monkeypatch, tmp_path):
    media, _commands = recording_media
    monkeypatch.setattr(media, "run", lambda *_args: None)

    with pytest.raises(MediaError, match="Extracting frame at 4.000s produced no image"):
        media.extract_frame(tmp_path / "source.mp4", 4, tmp_path / "prompt.png")


@pytest.mark.parametrize("error", [
    MediaError("decoding failed"),
    OperationCancelled("cancelled"),
    FileNotFoundError("FFmpeg is missing"),
])
def test_extract_frame_propagates_command_failures(
    recording_media, monkeypatch, tmp_path, error,
):
    media, _commands = recording_media

    def fail(*_args):
        raise error

    monkeypatch.setattr(media, "run", fail)
    with pytest.raises(type(error), match=str(error)):
        media.extract_frame(tmp_path / "source.mp4", 1, tmp_path / "prompt.png", size=(80, 60))


def test_extract_frame_honors_operation_cancellation(tmp_path, monkeypatch):
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "ffmpeg"
    cancelled = False

    def unexpected_process(*_args, **_kwargs):
        pytest.fail("A cancelled extraction must not start FFmpeg")

    monkeypatch.setattr(subprocess, "Popen", unexpected_process)
    with operation_scope(cancelled=lambda: cancelled):
        cancelled = True
        with pytest.raises(OperationCancelled):
            media.extract_frame(tmp_path / "source.mp4", 1, tmp_path / "prompt.png")


@pytest.fixture(scope="module")
def ffmpeg_media():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required")
    return MediaTools()


@pytest.fixture(scope="module", params=[0, 5])
def changing_video(ffmpeg_media, tmp_path_factory, request):
    source = tmp_path_factory.mktemp(f"changing-frames-{request.param}") / "source.mp4"
    ffmpeg_media.run([
        ffmpeg_media.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=24:duration=3",
        "-c:v", "mpeg4", "-q:v", "2", "-g", "48", "-bf", "2",
        "-output_ts_offset", str(request.param), str(source),
    ], "Creating changing-frame fixture")
    frames = json.loads(ffmpeg_media.run([
        ffmpeg_media.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=key_frame,pict_type,best_effort_timestamp_time",
        "-of", "json", str(source),
    ], "Checking changing-frame fixture").stdout)["frames"]
    assert any(frame["pict_type"] == "B" for frame in frames)
    assert float(frames[0]["best_effort_timestamp_time"]) == pytest.approx(request.param)
    return source


def slow_extract_frame(media, source, timestamp, destination):
    media.run([
        media.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-ss", f"{max(0.0, timestamp):.6f}",
        "-frames:v", "1", str(destination),
    ], "Extracting reference frame with output seeking")
    assert destination.is_file()


@pytest.mark.integration
@pytest.mark.parametrize("timestamp", [
    -0.5, 0, 0.001, 0.041666, 0.041667, 0.125, 1.013, 1.999999, 2.000001, 2.95,
])
def test_fast_seek_matches_slow_seek_with_b_frames(
    ffmpeg_media, changing_video, tmp_path, timestamp,
):
    expected = tmp_path / "expected.png"
    actual = tmp_path / "actual.png"
    slow_extract_frame(ffmpeg_media, changing_video, timestamp, expected)

    ffmpeg_media.extract_frame(changing_video, timestamp, actual)

    assert actual.read_bytes() == expected.read_bytes()


@pytest.mark.integration
def test_fixture_changes_between_adjacent_non_keyframes(ffmpeg_media, changing_video, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    slow_extract_frame(ffmpeg_media, changing_video, 1, first)
    slow_extract_frame(ffmpeg_media, changing_video, 1.05, second)

    assert first.read_bytes() != second.read_bytes()


@pytest.mark.integration
@pytest.mark.parametrize("size", [(80, 60), (84, 36), (213, 121)])
def test_scaled_frame_matches_existing_conversion_dimensions(
    ffmpeg_media, changing_video, tmp_path, size,
):
    original = tmp_path / "original.png"
    expected = tmp_path / "expected.png"
    actual = tmp_path / "actual.png"
    slow_extract_frame(ffmpeg_media, changing_video, 1.013, original)
    ffmpeg_media.convert_image(original, expected, *size)

    ffmpeg_media.extract_frame(changing_video, 1.013, actual, size=size)

    expected_info = ffmpeg_media.probe(expected)
    actual_info = ffmpeg_media.probe(actual)
    assert (expected_info.width, expected_info.height) == size
    assert (actual_info.width, actual_info.height) == size


@pytest.mark.integration
def test_extract_frame_past_end_fails(ffmpeg_media, changing_video, tmp_path):
    with pytest.raises(MediaError, match="produced no image"):
        ffmpeg_media.extract_frame(changing_video, 20, tmp_path / "missing.png")


@pytest.mark.integration
def test_extract_frame_invalid_source_fails(ffmpeg_media, tmp_path):
    source = tmp_path / "invalid.mp4"
    source.write_text("not a video", encoding="utf-8")

    with pytest.raises(MediaError, match="Extracting frame at 0.500s failed"):
        ffmpeg_media.extract_frame(source, 0.5, tmp_path / "missing.png")
