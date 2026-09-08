from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from choicer_voicer_pack_creator import recording_renderer as renderer
from choicer_voicer_pack_creator.export_resources import (
    ExportResourceBudget,
    ResourceSnapshot,
    current_ffmpeg_threads,
)
from choicer_voicer_pack_creator.media import AudioInfo, MediaError, MediaInfo, MediaTools
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    operation_scope,
)
from choicer_voicer_pack_creator.recording_renderer import prepare_recording, render_recording
from choicer_voicer_pack_creator.recordings import find_takes, read_pack


def _wav(path: Path, duration: float, value: float = 0.25) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(np.full((round(duration * 48_000), 2), value * 32767, "<i2").tobytes())


def _fixture(tmp_path: Path, *, starts=(0.2, 0.6), duration=0.2):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "_pack_info.ini").write_text('[data]\ntitle="Synthetic fixture"\n')
    (pack / "dub_video.mp4").write_bytes(b"synthetic video")
    (pack / "opaque-prompt.ini").write_text(
        f'[data]\ndub_timestamps={list(starts)}\ndub_characters=["Cat"]\n',
    )
    (pack / "missing.ini").write_text("[data]\ndub_timestamps=[0.1]\n")
    take = tmp_path / "recordings" / "pack" / "take"
    take.mkdir(parents=True)
    _wav(take / "_dubrecord_opaque-prompt.wav", duration)
    return pack, take


class FakeMedia:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"

    def __init__(self):
        self.commands = []
        self.encoded = []
        self.soundtracks = []
        self.duration = 1.0
        self.width = 16
        self.height = 16
        self.video_start = 0.0
        self.encoders = " V....D mpeg4 MPEG-4\n A..... aac AAC\n V....D h264_mf hardware\n"
        self.validated = []
        self.on_encode = None
        self.fail_published = False

    def probe(self, path):
        source_size = path.name == "dub_video.mp4" or path.suffix == ".mkv"
        width = self.width if source_size else self.width + self.width % 2
        height = self.height if source_size else self.height + self.height % 2
        audio = "pcm_s16le" if path.suffix == ".mkv" else "aac"
        return MediaInfo(self.duration, width, height, 10.0, True, "mpeg4", audio, "yuv420p", 48000, 2)

    def probe_audio(self, path):
        if path.suffix in {".mp4", ".mkv"}:
            codec = "pcm_s16le" if path.suffix == ".mkv" else "aac"
            return AudioInfo(self.duration, codec, 48_000, 2)
        with wave.open(str(path), "rb") as audio:
            return AudioInfo(
                audio.getnframes() / audio.getframerate(), "pcm_s16le",
                audio.getframerate(), audio.getnchannels(),
            )

    def run(self, command, description):
        self.commands.append(command)
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, self.encoders, "")
        if command[0] == self.ffprobe:
            start = self.video_start if Path(command[-1]).name == "dub_video.mp4" else 0
            value = {"streams": [{
                "duration": str(self.duration), "avg_frame_rate": "10/1", "start_time": str(start),
            }]}
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        assert current_ffmpeg_threads() is not None
        source = Path(command[command.index("-i") + 1])
        if "null" in command:
            assert "-xerror" in command
            self.decode(source)
            return subprocess.CompletedProcess(command, 0, "", "")
        with wave.open(str(source), "rb") as audio:
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2") / 32768
        Path(command[-1]).write_bytes(samples.astype("<f4").tobytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    def _run_video_conversion(self, command, progress):
        assert current_ffmpeg_threads() is not None
        self.encoded.append(command)
        inputs = [command[index + 1] for index, arg in enumerate(command) if arg == "-i"]
        with Path(inputs[1]).open("rb") as source:
            self.soundtracks.append(hashlib.file_digest(source, "sha256").hexdigest())
        Path(command[-1]).write_bytes(b"synthetic rendered mp4")
        if self.on_encode:
            self.on_encode()

    def decode(self, path):
        assert current_ffmpeg_threads() is not None
        self.validated.append(path)
        if self.fail_published and path.stem != "render":
            raise MediaError("Published validation failure")


@pytest.fixture(autouse=True)
def resource_budget(monkeypatch):
    monkeypatch.setattr(renderer, "export_resources", ExportResourceBudget(
        snapshot_provider=lambda: ResourceSnapshot(12 * 1024**3, 16 * 1024**3, 8, 8.0, None),
    ))


def test_prepare_reuses_actual_recordings_at_all_metadata_times_and_warns_small_tail(tmp_path):
    pack, take = _fixture(tmp_path, starts=(0.1, 0.847))
    plan = prepare_recording(FakeMedia(), read_pack(pack), find_takes(take)[0])
    assert [(clip.start, clip.duration) for clip in plan.clips] == [(0.1, 0.2), (0.847, 0.2)]
    assert plan.clips[0].characters == ("Cat",)
    assert all(clip.path.parent == take for clip in plan.clips)
    assert any("0.047000s" in warning for warning in plan.warnings)
    assert any("Missing recording for missing" in warning for warning in plan.warnings)
    assert any("Voice-only" in warning for warning in plan.warnings)
    assert plan.duration == 1.0


def test_unmatched_files_are_explicit_and_invalid_names_fail_closed(tmp_path):
    pack, take = _fixture(tmp_path)
    _wav(take / "_dubrecord_unknown.wav", 0.1)
    plan = prepare_recording(FakeMedia(), read_pack(pack), find_takes(take)[0])
    assert any("Unmatched recording" in warning for warning in plan.warnings)
    assert all(clip.path.name != "_dubrecord_unknown.wav" for clip in plan.clips)
    _wav(take / "wrong-name.wav", 0.1)
    with pytest.raises(ValueError, match="Unrecognized recording"):
        prepare_recording(FakeMedia(), read_pack(pack), find_takes(take)[0])


def test_no_matched_in_range_recordings_fail_closed(tmp_path):
    pack, take = _fixture(tmp_path, starts=(2.0,))
    with pytest.raises(ValueError, match="No matched recordings"):
        prepare_recording(FakeMedia(), read_pack(pack), find_takes(take)[0])


@pytest.mark.parametrize("gain", [float("nan"), float("inf"), -1, True])
def test_nonfinite_or_negative_gains_are_rejected(tmp_path, gain):
    pack, take = _fixture(tmp_path)
    with pytest.raises(ValueError, match="gain"):
        prepare_recording(FakeMedia(), read_pack(pack), find_takes(take)[0], voices_gain=gain)


def test_prepare_detects_metadata_or_inventory_changes_since_discovery(tmp_path):
    pack, take = _fixture(tmp_path)
    discovered = read_pack(pack)
    (pack / "opaque-prompt.ini").write_text("[data]\ndub_timestamps=[0.9]\n")
    with pytest.raises(SourceChangedError):
        prepare_recording(FakeMedia(), discovered, find_takes(take)[0])
    discovered_take = find_takes(take)[0]
    _wav(take / "_dubrecord_new.wav", 0.1)
    with pytest.raises(SourceChangedError):
        prepare_recording(FakeMedia(), read_pack(pack), discovered_take)


def test_software_encoder_selection_does_not_pick_gpl_or_hardware(tmp_path):
    media = FakeMedia()
    media.encoders += " V....D libx264 GPL H264\n"
    assert renderer._encoder(media)[0] == "mpeg4"
    media.encoders += " V....D libopenh264 CPU H264\n"
    assert renderer._encoder(media) == ("libopenh264", ())
    media.encoders = " V....D h264_mf hardware\n A..... aac audio\n"
    with pytest.raises(MediaError, match="No supported software"):
        renderer._encoder(media)


def test_disk_mix_uses_sample_offsets_overlaps_gaps_and_gain_without_normalization(tmp_path):
    mix = tmp_path / "mix.f64"
    with mix.open("wb") as output:
        output.truncate(8 * renderer._MIX_FRAME_BYTES)
    decoded = tmp_path / "source.f32"
    decoded.write_bytes(np.full((2, 2), 0.75, "<f4").tobytes())
    renderer._mix_pcm(mix, decoded, (2 / 48000, 3 / 48000), 2.0, 8)
    values = np.frombuffer(mix.read_bytes(), "<f8").reshape(-1, 2)
    assert np.all(values[0:2] == 0)
    assert np.all(values[2] == 1.5)
    assert np.all(values[3] == 3.0)
    assert np.all(values[4] == 1.5)
    assert np.all(values[5:] == 0)


def test_mix_rejects_nonfinite_pcm_and_is_cancellable(tmp_path):
    mix = tmp_path / "mix.f64"
    mix.write_bytes(b"\0" * 64)
    decoded = tmp_path / "source.f32"
    decoded.write_bytes(np.array([np.nan, 0], "<f4").tobytes())
    with pytest.raises(MediaError, match="non-finite"):
        renderer._mix_pcm(mix, decoded, (0.0,), 1.0, 4)
    with pytest.raises(OperationCancelled), operation_scope(cancelled=lambda: True):
        renderer._mix_pcm(mix, decoded, (0.0,), 1.0, 4)


def test_render_options_mapping_limiter_and_transactional_publication(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    media.width, media.height = 17, 19
    backing = tmp_path / "backing.wav"
    _wav(backing, 0.9)
    plan = prepare_recording(
        media, read_pack(pack), find_takes(take)[0], backing_path=backing,
        voices_gain=1.25, backing_gain=0.75,
    )
    assert any("18x20" in warning for warning in plan.warnings)
    destination = tmp_path / "result.mp4"
    result = render_recording(media, plan, destination)
    assert result.path == destination
    assert result.encoder == "mpeg4"
    assert result.warnings == plan.warnings
    command = media.encoded[0]
    assert command[command.index("-vf") + 1] == (
        "setpts=PTS-STARTPTS,pad=18:20:0:0,format=yuv420p"
    )
    assert command[command.index("-af") + 1].endswith("latency=true")
    assert "level=false" in command[command.index("-af") + 1]
    assert [command[index + 1] for index, arg in enumerate(command) if arg == "-map"] == [
        "0:v:0", "1:a:0",
    ]
    assert "-r" not in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[command.index("-t") + 1] == "1.000000000"
    assert len(media.validated) == 2
    assert not list(tmp_path.glob(".recording-*"))
    with pytest.raises(FileExistsError):
        render_recording(media, plan, destination)


@pytest.mark.parametrize("format,suffix", [("mp4", ".mp4"), ("preview", ".mkv")])
def test_cancellation_during_render_preserves_destination_and_cleans_only_owned_stage(
    tmp_path, format, suffix,
):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / f"result{suffix}"
    destination.write_bytes(b"previous output")
    unrelated = tmp_path / ".recording-unrelated"
    unrelated.mkdir()
    cancel = False

    def stop():
        nonlocal cancel
        cancel = True

    media.on_encode = stop
    with pytest.raises(OperationCancelled), operation_scope(cancelled=lambda: cancel):
        render_recording(media, plan, destination, format=format, overwrite=True)
    assert destination.read_bytes() == b"previous output"
    assert list(tmp_path.glob(".recording-*")) == [unrelated]


@pytest.mark.parametrize("changed", ["video", "metadata", "recording", "new-recording"])
def test_changed_inputs_prevent_render_or_publication(tmp_path, changed):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    destination.write_bytes(b"previous output")
    paths = {
        "video": plan.video_path,
        "metadata": pack / "opaque-prompt.ini",
        "recording": take / "_dubrecord_opaque-prompt.wav",
        "new-recording": take / "_dubrecord_new.wav",
    }
    media.on_encode = lambda: paths[changed].write_bytes(b"source changed")
    with pytest.raises(SourceChangedError, match="Refresh"):
        render_recording(media, plan, destination, overwrite=True)
    assert destination.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".recording-*"))


def test_failed_published_validation_rolls_back_and_late_cancellation_commits(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    destination.write_bytes(b"previous output")
    media.fail_published = True
    with pytest.raises(MediaError, match="Published"):
        render_recording(media, plan, destination, overwrite=True)
    assert destination.read_bytes() == b"previous output"
    media.fail_published = False
    cancelled = False
    committed = []

    def progress(message, fraction):
        nonlocal cancelled
        if message.startswith("Publishing"):
            cancelled = True

    with operation_scope(
        cancelled=lambda: cancelled, progress=progress, committed=lambda: committed.append(True),
    ):
        render_recording(media, plan, destination, overwrite=True)
    assert committed == [True]
    assert destination.read_bytes() == b"synthetic rendered mp4"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("format,suffix", [("mp4", ".mp4"), ("preview", ".mkv")])
def test_stale_prepared_plan_reports_refresh_guidance_before_any_render(
    tmp_path, existing, format, suffix,
):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / f"out{suffix}"
    if existing:
        destination.write_bytes(b"previous output")
    plan.clips[0].path.unlink()
    with pytest.raises(SourceChangedError, match="Refresh"):
        render_recording(media, plan, destination, format=format, overwrite=existing)
    with pytest.raises(SourceChangedError, match="Refresh"):
        plan.verify_sources()
    assert not media.encoded
    if existing:
        assert destination.read_bytes() == b"previous output"
    else:
        assert not destination.exists()


def test_destination_race_never_overwrites_unconfirmed_output(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    media.on_encode = lambda: destination.write_bytes(b"other writer")
    with pytest.raises(FileExistsError):
        render_recording(media, plan, destination)
    assert destination.read_bytes() == b"other writer"


@pytest.mark.parametrize("existing", [False, True])
def test_competing_output_during_published_validation_is_preserved(tmp_path, monkeypatch, existing):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    if existing:
        destination.write_bytes(b"previous output")
    original_validate = renderer._validate_output

    def validate(media, plan, path, *, format):
        if path == destination:
            competing = tmp_path / "competing.mp4"
            competing.write_bytes(b"other writer replacement")
            os.replace(competing, destination)
            raise MediaError("Published validation failure")
        original_validate(media, plan, path, format=format)

    monkeypatch.setattr(renderer, "_validate_output", validate)
    with pytest.raises(SourceChangedError, match="competing output is preserved"):
        render_recording(media, plan, destination, overwrite=existing)
    assert destination.read_bytes() == b"other writer replacement"
    backups = list(tmp_path.glob(".result.mp4.previous-*"))
    assert len(backups) == int(existing)
    if backups:
        assert backups[0].read_bytes() == b"previous output"


@pytest.mark.parametrize("existing", [False, True])
def test_competing_replacement_between_rollback_check_and_claim_is_preserved(
    tmp_path, monkeypatch, existing,
):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    media.fail_published = True
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    if existing:
        destination.write_bytes(b"previous output")
    original_rename = os.rename

    def rename(source, target):
        if ".rejected-" in Path(target).name:
            competing = tmp_path / "competing.mp4"
            competing.write_bytes(b"other writer during rollback")
            os.replace(competing, destination)
        return original_rename(source, target)

    monkeypatch.setattr(renderer.os, "rename", rename)
    with pytest.raises(SourceChangedError, match="competing output is preserved"):
        render_recording(media, plan, destination, overwrite=existing)
    assert destination.read_bytes() == b"other writer during rollback"
    assert len(list(tmp_path.glob(".result.mp4.previous-*"))) == int(existing)


def test_competing_output_after_approved_overwrite_claim_is_not_overwritten(tmp_path, monkeypatch):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    destination.write_bytes(b"previous output")
    original_link = os.link

    def link(source, target):
        if Path(source).name == "render.mp4":
            destination.write_bytes(b"other writer before publication")
        return original_link(source, target)

    monkeypatch.setattr(renderer.os, "link", link)
    with pytest.raises(OSError, match="previous output is retained"):
        render_recording(media, plan, destination, overwrite=True)
    assert destination.read_bytes() == b"other writer before publication"
    backup, = tmp_path.glob(".result.mp4.previous-*")
    assert backup.read_bytes() == b"previous output"


@pytest.mark.parametrize("alias", ["pack ", "pack.", "pack:stream", "pack?"])
def test_ambiguous_destination_components_never_create_source_subdirectories(tmp_path, alias):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    before = set(pack.iterdir())
    with pytest.raises(ValueError, match="Unsafe filesystem path"):
        render_recording(media, plan, tmp_path / alias / "new-output" / "result.mp4")
    assert set(pack.iterdir()) == before
    assert not media.encoded


def test_cannot_export_over_inputs_or_into_source_folders(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    for path in (plan.video_path, take / "result.mp4", pack / "new" / "result.mp4"):
        with pytest.raises(ValueError, match="original"):
            render_recording(media, plan, path, overwrite=True)
    with pytest.raises(ValueError, match="Only MP4"):
        render_recording(media, plan, tmp_path / "out.webm", format="webm")
    with pytest.raises(ValueError, match="gain"):
        render_recording(media, replace(plan, voices_gain=float("nan")), tmp_path / "out.mp4")
    alias = tmp_path / "input-alias.mp4"
    os.link(plan.video_path, alias)
    with pytest.raises(ValueError, match="original input"):
        render_recording(media, plan, alias, overwrite=True)
    with pytest.raises(ValueError, match="Overwrite permission"):
        render_recording(media, plan, tmp_path / "out.mp4", overwrite="true")


def test_invalid_plan_times_or_unsnapshotted_inputs_are_rejected(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    invalid = replace(plan.clips[0], start=float("nan"))
    with pytest.raises(ValueError, match="timestamp"):
        render_recording(media, replace(plan, clips=(invalid,)), tmp_path / "out.mp4")
    invalid = replace(plan.clips[0], path=tmp_path / "_dubrecord_outside.wav")
    with pytest.raises(ValueError, match="snapshot"):
        render_recording(media, replace(plan, clips=(invalid,)), tmp_path / "out.mp4")
    with pytest.raises(ValueError, match="omits"):
        render_recording(media, replace(plan, clips=()), tmp_path / "out.mp4")


def test_extremely_late_occurrences_warn_without_allocating_delayed_audio(tmp_path):
    pack, take = _fixture(tmp_path, starts=(0.1, 1e300))
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    assert any("out of range" in warning for warning in plan.warnings)
    render_recording(media, plan, tmp_path / "out.mp4")
    assert len(media.encoded[0]) < 120
    assert len([command for command in media.commands if "f32le" in command]) == 1


def test_many_recordings_never_create_a_windows_sized_input_command(tmp_path):
    pack, take = _fixture(tmp_path)
    for index in range(256):
        stem = f"{index:04d}-" + "a" * 48
        (pack / f"{stem}.ini").write_text("[data]\ndub_timestamps=[0.4]\n")
        _wav(take / f"_dubrecord_{stem}.wav", 4 / 48_000)
    discovered = find_takes(take)[0]
    assert sum(len(str(path)) for path in discovered.recordings) > 32_767
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), discovered)
    render_recording(media, plan, tmp_path / "out.mp4")
    assert len([command for command in media.commands if "f32le" in command]) == 257
    assert max(len(subprocess.list2cmdline(command)) for command in [
        *media.commands, *media.encoded,
    ]) < 8000


def test_rollback_failure_retains_previous_output_recovery_file(tmp_path, monkeypatch):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    media.fail_published = True
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "out.mp4"
    destination.write_bytes(b"previous output")
    original = os.link

    def restore_file(source, target):
        if ".previous-" in Path(source).name:
            raise PermissionError("Synthetic rollback failure")
        return original(source, target)

    monkeypatch.setattr(renderer.os, "link", restore_file)
    with pytest.raises(OSError, match="previous output is retained"):
        render_recording(media, plan, destination, overwrite=True)
    backup, = tmp_path.glob(".out.mp4.previous-*")
    assert backup.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".recording-*"))


def test_preview_copies_video_and_reuses_identical_limited_composition(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    media.width, media.height = 17, 19
    media.video_start = 2.25
    plan = prepare_recording(
        media, read_pack(pack), find_takes(take)[0], voices_gain=1.75, backing_gain=0.25,
    )
    result = render_recording(media, plan, tmp_path / "preview.mkv", format="preview")
    command = media.encoded[0]
    assert result.encoder == "copy (mpeg4)"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert command[command.index("-itsoffset") + 1] == "-2.250000000"
    assert "-copyts" in command
    assert "-vf" not in command
    assert "-pix_fmt" not in command
    assert "-movflags" not in command
    assert "-t" not in command
    assert command[-2] == "matroska"
    render_recording(media, plan, tmp_path / "export.mp4")
    assert media.soundtracks[0] == media.soundtracks[1]
    assert command[command.index("-af") + 1] == (
        media.encoded[1][media.encoded[1].index("-af") + 1]
    )
    assert plan.voices_gain == 1.75 and plan.backing_gain == 0.25


def test_preview_rejects_unsupported_codec_or_wrong_container_without_transcoding(tmp_path):
    pack, take = _fixture(tmp_path)
    media = FakeMedia()
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    with pytest.raises(MediaError, match="never silently transcodes"):
        render_recording(
            media, replace(plan, video_codec="unsupported-codec"),
            tmp_path / "preview.mkv", format="preview",
        )
    with pytest.raises(ValueError, match=".mkv extension"):
        render_recording(media, plan, tmp_path / "wrong.mp4", format="preview")
    with pytest.raises(ValueError, match=".mp4 extension"):
        render_recording(media, plan, tmp_path / "wrong.mkv")
    assert not media.encoded


def test_matroska_timing_uses_video_tag_not_longer_container_audio_duration():
    class TaggedMedia:
        ffprobe = "ffprobe"

        def run(self, command, description):
            value = {
                "streams": [{
                    "avg_frame_rate": "60000/1001", "start_time": "0.0",
                    "tags": {"DURATION": "00:00:17.017000000"},
                }],
                "format": {"duration": "45.0"},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    duration, rate, start = renderer._video_timing(TaggedMedia(), Path("preview.mkv"))
    assert duration == 17.017
    assert rate == "60000/1001"
    assert start == 0.0


@pytest.mark.integration
@pytest.mark.parametrize("codec,extension,start,pattern", [
    ("libtheora", ".ogv", 0, "testsrc2"), ("libtheora", ".ogv", 0, "color"),
    ("mpeg4", ".mp4", 2.0, "testsrc2"),
])
def test_actual_preview_stream_copy_preserves_packets_and_starts_at_zero(
    tmp_path, codec, extension, start, pattern,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required for the integration fixture.")
    media = MediaTools()
    pack, take = _fixture(tmp_path, starts=(0.2,), duration=0.2)
    (pack / "dub_video.mp4").unlink()
    video = pack / f"dub_video{extension}"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
        "-i", f"{pattern}=s=32x24:r=60000/1001:d=1.001",
        "-an", "-c:v", codec, "-q:v", "5",
        "-output_ts_offset", str(start), "-t", "1.001", str(video),
    ], "Creating stream-copy preview fixture")
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    assert plan.video_start == pytest.approx(start, abs=0.001)
    result = render_recording(media, plan, tmp_path / "preview.mkv", format="preview")
    info = media.probe(result.path)
    assert info.video_codec == ("theora" if codec == "libtheora" else codec)
    assert info.audio_codec == "pcm_s16le"
    assert (info.width, info.height) == (plan.width, plan.height)

    def packets(path):
        data = media.run([
            media.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_packets",
            "-show_entries", "packet=data_hash,pts_time,size", "-show_data_hash", "sha256",
            "-of", "json", str(path),
        ], "Reading fixture video packet hashes").stdout
        return [packet for packet in json.loads(data)["packets"] if int(packet["size"]) > 0]

    original = packets(video)
    copied = packets(result.path)
    assert [packet["data_hash"] for packet in copied] == [
        packet["data_hash"] for packet in original
    ]
    assert float(copied[0]["pts_time"]) == pytest.approx(0.0, abs=0.001)
    for left, right in zip(original, copied, strict=True):
        assert float(left["pts_time"]) - plan.video_start == pytest.approx(
            float(right["pts_time"]), abs=0.001,
        )
    decoded = media._capture([
        media.ffmpeg, "-v", "error", "-i", str(result.path), "-map", "0:a:0",
        "-f", "f32le", "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "pipe:1",
    ])
    assert decoded.returncode == 0
    samples = np.frombuffer(decoded.stdout, "<f4").reshape(-1, 2)
    assert np.max(np.abs(samples[2400:7200])) == 0
    assert np.mean(samples[12000:16800]) == pytest.approx(0.25, abs=0.001)


@pytest.mark.integration
@pytest.mark.parametrize("rate,duration,frames", [
    ("10", 1.0, 10), ("60000/1001", 1.001, 60),
])
def test_static_theora_mp4_export_reconstructs_duplicates_and_trailing_hold(
    tmp_path, rate, duration, frames,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required for the integration fixture.")
    media = MediaTools()
    pack, take = _fixture(tmp_path, starts=(0.2,), duration=0.2)
    (pack / "dub_video.mp4").unlink()
    source = pack / "dub_video.ogv"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
        "-i", f"color=s=32x24:r={rate}:d={duration}",
        "-an", "-c:v", "libtheora", "-q:v", "5", str(source),
    ], "Creating static Theora fixture")
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    target = tmp_path / "static-recording.mp4"
    render_recording(media, plan, target)
    stream = json.loads(media.run([
        media.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames,duration,avg_frame_rate",
        "-of", "json", str(target),
    ], "Reading reconstructed Theora frame count").stdout)["streams"][0]
    assert int(stream["nb_read_frames"]) == frames
    assert float(stream["duration"]) == pytest.approx(duration, abs=0.001)
    assert media.probe(target).fps == pytest.approx(plan.fps, abs=0.001)


@pytest.mark.integration
@pytest.mark.parametrize("rate,video_duration", [("10", 1.0), ("60000/1001", 1.001)])
def test_actual_ffmpeg_mix_keeps_silent_gaps_overlap_peaks_and_source_duration(
    tmp_path, rate, video_duration,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required for the integration fixture.")
    media = MediaTools()
    pack, take = _fixture(tmp_path, starts=(0.2, 0.5, 0.95), duration=0.1)
    # A loud original-video soundtrack must never leak into the recording mix.
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=s=32x24:r={rate}:d={video_duration}",
        "-f", "lavfi", "-i", f"sine=frequency=1000:duration={video_duration}:sample_rate=48000",
        "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac",
        "-t", str(video_duration), str(pack / "dub_video.mp4"),
    ], "Creating synthetic video fixture")
    _wav(take / "_dubrecord_opaque-prompt.wav", 0.1, 0.7)
    (pack / "overlap.ini").write_text("[data]\ndub_timestamps=[0.2]\n")
    _wav(take / "_dubrecord_overlap.wav", 0.1, 0.7)
    plan = prepare_recording(media, read_pack(pack), find_takes(take)[0])
    destination = tmp_path / "result.mp4"
    render_recording(media, plan, destination)
    assert media.probe(destination).duration == pytest.approx(video_duration, abs=0.03)
    assert media.probe(destination).fps == pytest.approx(plan.fps, abs=0.01)
    decoded = media._capture([
        media.ffmpeg, "-v", "error", "-i", str(destination), "-map", "0:a:0",
        "-f", "f32le", "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "pipe:1",
    ])
    assert decoded.returncode == 0
    samples = np.frombuffer(decoded.stdout, "<f4").reshape(-1, 2)
    assert np.max(np.abs(samples[round(0.05 * 48000):round(0.15 * 48000)])) < 0.005
    assert np.max(np.abs(samples)) <= 1.01
    # Independent nonoverlapped voice retains its requested gain, not amix's 1/N.
    assert np.mean(samples[round(0.53 * 48000):round(0.57 * 48000)]) == pytest.approx(0.7, abs=0.04)
    assert np.mean(samples[round(0.23 * 48000):round(0.27 * 48000)]) > 0.85
    assert any(f"{1.05 - video_duration:.6f}s" in warning for warning in plan.warnings)
    backing = tmp_path / "separate-backing.wav"
    _wav(backing, 1.0, 0.2)
    backed = prepare_recording(
        media, read_pack(pack), find_takes(take)[0],
        backing_path=backing, voices_gain=0.5, backing_gain=0.5,
    )
    render_recording(media, backed, tmp_path / "with-backing.mp4")
    decoded = media._capture([
        media.ffmpeg, "-v", "error", "-i", str(tmp_path / "with-backing.mp4"), "-map", "0:a:0",
        "-f", "f32le", "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "pipe:1",
    ])
    assert decoded.returncode == 0
    samples = np.frombuffer(decoded.stdout, "<f4").reshape(-1, 2)
    assert np.mean(samples[round(0.07 * 48000):round(0.12 * 48000)]) == pytest.approx(0.1, abs=0.02)
    assert np.mean(samples[round(0.53 * 48000):round(0.57 * 48000)]) == pytest.approx(0.45, abs=0.04)
