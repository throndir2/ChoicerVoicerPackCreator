"""One deterministic, bounded recording composition for preview and video export."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from choicer_voicer_pack_creator.export_resources import (
    current_ffmpeg_threads,
    estimate_media_work,
    export_resources,
)
from choicer_voicer_pack_creator.media import MediaError, MediaTools, VideoEncodingProgress
from choicer_voicer_pack_creator.operations import (
    SourceChangedError,
    SourceSnapshot,
    canonical_path,
    check_cancelled,
    critical_stage,
    operation_scope,
    path_leases,
    report,
)
from choicer_voicer_pack_creator.recordings import (
    PackInfo,
    TakeInfo,
    _inventory,
    _safe_path,
    find_takes,
    read_pack,
    recording_stem,
)

_RATE = 48_000
_CHANNELS = 2
_CHUNK_FRAMES = 8192
_PCM_FRAME_BYTES = _CHANNELS * 4
_MIX_FRAME_BYTES = _CHANNELS * 8
_PREVIEW_CODECS = frozenset({"theora", "h264", "hevc", "mpeg4", "vp8", "vp9", "av1"})


@dataclass(frozen=True, slots=True)
class RecordingClip:
    path: Path
    start: float
    duration: float
    characters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordingPlan:
    pack: PackInfo
    take: TakeInfo
    video_path: Path
    backing_path: Path | None
    clips: tuple[RecordingClip, ...]
    duration: float
    warnings: tuple[str, ...]
    voices_gain: float
    backing_gain: float
    snapshot: SourceSnapshot
    width: int
    height: int
    fps: float
    frame_rate: str
    encoder: str
    backing_duration: float = 0.0
    video_codec: str = ""
    video_start: float = 0.0

    def verify_sources(self) -> None:
        """Also call before publishing a cached preview instead of rendering again."""
        _verify_sources(self)


@dataclass(frozen=True, slots=True)
class RecordingRenderResult:
    path: Path
    warnings: tuple[str, ...]
    encoder: str = ""


def _number(value: float, label: str, *, positive: bool = False) -> None:
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or abs(value) > sys.float_info.max or not math.isfinite(value)
        or value < 0 or (positive and value == 0)
    ):
        raise ValueError(f"{label} must be a finite {'positive' if positive else 'nonnegative'} number.")


def _encoder(media: MediaTools) -> tuple[str, tuple[str, ...]]:
    output = media.run(
        [media.ffmpeg, "-hide_banner", "-encoders"], "Checking recording export encoders",
    ).stdout
    encoders = {
        parts[1] for line in output.splitlines()
        if len(parts := line.split()) >= 2 and len(parts[0]) == 6
    }
    if "aac" not in encoders:
        raise MediaError("This FFmpeg installation lacks the AAC encoder required for MP4 export.")
    if "libopenh264" in encoders:
        return "libopenh264", ()
    if "mpeg4" in encoders:
        return "mpeg4", (
            "Software H.264 (OpenH264) is unavailable. MP4 export will use MPEG-4 Part 2 video "
            "and AAC audio; some Windows players and sharing services may not support it. "
            "Hardware H.264 is not selected automatically.",
        )
    raise MediaError(
        "No supported software MP4 encoder is available. Use an LGPL FFmpeg build with "
        "libopenh264 or the native mpeg4 encoder and AAC."
    )


def _video_timing(media: MediaTools, path: Path) -> tuple[float, str, float]:
    output = media.run([
        media.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=duration,duration_ts,time_base,avg_frame_rate,r_frame_rate,start_time:stream_tags=DURATION",
        "-of", "json", str(path),
    ], f"Reading video timeline: {path.name}").stdout
    data = json.loads(output)
    if not data.get("streams"):
        raise MediaError(f"No video stream found in {path.name}.")
    stream = data["streams"][0]
    try:
        start = float(stream.get("start_time") or 0)
        if not math.isfinite(start):
            raise ValueError("Non-finite video start")
        if stream.get("duration") not in {None, "N/A"}:
            duration = float(stream["duration"])
        elif stream.get("duration_ts") not in {None, "N/A"}:
            duration = float(int(stream["duration_ts"]) * Fraction(stream["time_base"]))
        else:
            # Matroska records per-stream end times in DURATION tags, rather
            # than stream.duration. Do not substitute the container/audio end.
            hours, minutes, seconds = stream["tags"]["DURATION"].split(":")
            hours, minutes, seconds = int(hours), int(minutes), float(seconds)
            if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                raise ValueError("Invalid Matroska video duration")
            duration = hours * 3600 + minutes * 60 + seconds - start
        average = stream.get("avg_frame_rate")
        rate = Fraction(average if average not in {None, "", "N/A", "0/0"} else "0")
        if rate <= 0:
            rate = Fraction(stream.get("r_frame_rate") or "0")
        _number(duration, "Video stream duration", positive=True)
        _number(float(rate), "Video frame rate", positive=True)
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        raise MediaError(
            f"Cannot determine a finite video-stream duration and frame rate for {path.name}."
        ) from error
    return duration, str(rate), start


def _check_discovered(pack: PackInfo, take: TakeInfo) -> tuple[PackInfo, TakeInfo]:
    if pack.errors:
        raise ValueError("Cannot render an invalid pack: " + "; ".join(pack.errors))
    for item in (pack, take):
        _verify_discovered_item(item)
    fresh_pack = read_pack(pack.path)
    if (
        pack.prompts != fresh_pack.prompts or pack.video_path != fresh_pack.video_path
        or pack.backing_path != fresh_pack.backing_path
    ):
        raise SourceChangedError("Pack metadata changed; refresh the recording library.")
    fresh_take = next((item for item in find_takes(take.path) if item.path == take.path), None)
    if fresh_take is None or fresh_take.recordings != take.recordings:
        raise SourceChangedError("Take inventory changed; refresh the recording library.")
    return fresh_pack, fresh_take


def prepare_recording(
    media: MediaTools,
    pack: PackInfo,
    take: TakeInfo,
    *,
    backing_path: Path | None = None,
    voices_gain: float = 1.0,
    backing_gain: float = 1.0,
) -> RecordingPlan:
    _number(voices_gain, "Voices gain")
    _number(backing_gain, "Backing gain")
    with operation_scope(), path_leases(read_paths=[pack.path, take.path, *(
        [backing_path] if backing_path is not None else []
    )]):
        pack, take = _check_discovered(pack, take)
        if pack.video_path is None:
            raise ValueError("The selected pack has no source video.")
        video = _safe_path(pack.video_path)
        backing = _safe_path(backing_path or pack.backing_path) if (
            backing_path is not None or pack.backing_path is not None
        ) else None
        sources = [video, *take.recordings, *(prompt.metadata_path for prompt in pack.prompts),
                   pack.path / "_pack_info.ini"]
        if backing is not None:
            sources.append(backing)
        for path in sources:
            _safe_path(path)
            if not path.is_file():
                raise ValueError(f"Source file does not exist: {path}")
        snapshot = SourceSnapshot.capture(sources)
        info = media.probe(video)
        duration, frame_rate, video_start = _video_timing(media, video)
        if info.width <= 0 or info.height <= 0:
            raise MediaError("Source video must have positive dimensions.")
        encoder, codec_warnings = _encoder(media)
        warnings = [*pack.warnings, *take.warnings, *codec_warnings]
        if info.width % 2 or info.height % 2:
            warnings.append(
                f"Odd source dimensions {info.width}x{info.height} will be padded to "
                f"{info.width + info.width % 2}x{info.height + info.height % 2} for MP4."
            )
        prompts = {prompt.stem: prompt for prompt in pack.prompts}
        seen: set[str] = set()
        folded: set[str] = set()
        clips = []
        for index, path in enumerate(take.recordings, 1):
            check_cancelled()
            report(f"Preparing recording {index}/{len(take.recordings)}: {path.name}")
            stem = recording_stem(path)
            if stem.casefold() in folded:
                raise ValueError(f"Ambiguous duplicate recording identity: {stem}")
            seen.add(stem)
            folded.add(stem.casefold())
            if stem not in prompts:
                warnings.append(f"Unmatched recording, not mixed: {path.name}. Select its matching pack.")
                continue
            audio = media.probe_audio(path)
            _number(audio.duration, f"Recording duration ({path.name})", positive=True)
            if audio.channels <= 0 or audio.sample_rate <= 0:
                raise MediaError(f"Invalid audio stream in {path.name}.")
            prompt = prompts[stem]
            for start in prompt.timestamps:
                if start >= duration:
                    warnings.append(
                        f"{path.name}: timestamp {start:.6f}s is out of range for the "
                        f"{duration:.6f}s video; that occurrence will be silent."
                    )
                if start + audio.duration > duration:
                    warnings.append(
                        f"{path.name} at {start:.6f}s extends beyond the video by "
                        f"{start + audio.duration - duration:.6f}s; the recorded tail will be cut off."
                    )
                clips.append(RecordingClip(path, start, audio.duration, prompt.characters))
        for stem in sorted(prompts.keys() - seen):
            warnings.append(f"Missing recording for {stem}; its timestamps will remain silent.")
        if not clips or not any(clip.start < duration for clip in clips):
            raise ValueError("No matched recordings occur within the selected video.")
        backing_duration = 0.0
        if backing is None:
            warnings.append(
                "Voice-only mix: no separate backing track is selected. Embedded source-video "
                "audio and original prompt voices are never included."
            )
        else:
            audio = media.probe_audio(backing)
            _number(audio.duration, "Backing duration", positive=True)
            if audio.channels <= 0 or audio.sample_rate <= 0:
                raise MediaError("The selected backing track has no valid audio stream.")
            backing_duration = audio.duration
            if backing_duration < duration:
                warnings.append("The backing track ends before the video; the remaining gap is silent.")
        warnings.append(
            "Recordings are matched to exact prompt metadata names, not pack versions. "
            "Confirm that the selected source pack is the version used to record this take."
        )
        _verify_input_snapshot(snapshot)
        plan = RecordingPlan(
            pack, take, video, backing,
            tuple(sorted(clips, key=lambda clip: (clip.start, clip.path.name))),
            duration, tuple(warnings), voices_gain, backing_gain, snapshot,
            info.width, info.height, float(Fraction(frame_rate)), frame_rate, encoder, backing_duration,
            info.video_codec, video_start,
        )
        _verify_sources(plan)
        return plan


def _verify_input_snapshot(snapshot: SourceSnapshot) -> None:
    try:
        for root in snapshot.roots:
            path = _safe_path(Path(root))
            if not path.is_file():
                raise SourceChangedError(f"Input disappeared or is no longer a regular file: {path}")
        snapshot.verify()
    except (SourceChangedError, OSError, ValueError) as error:
        raise SourceChangedError(
            f"Recording sources changed or became unavailable. Refresh the recording library "
            f"and prepare the mix again. Details: {error}"
        ) from error


def _verify_discovered_item(item: PackInfo | TakeInfo) -> None:
    try:
        _safe_path(item.path, directory=True)
        inventory = _inventory(item.path)
    except (OSError, ValueError) as error:
        raise SourceChangedError(
            f"Recording source folder became unavailable or unsafe. Refresh the recording library "
            f"and prepare the mix again. Details: {error}"
        ) from error
    if item.inventory and inventory != item.inventory:
        raise SourceChangedError(
            "Source inventory changed. Refresh the recording library and prepare the mix again."
        )
    if item.snapshot is not None:
        _verify_input_snapshot(item.snapshot)


def _verify_sources(plan: RecordingPlan) -> None:
    inputs = {canonical_path(path) for path in (
        plan.video_path, *(clip.path for clip in plan.clips),
        *([plan.backing_path] if plan.backing_path is not None else []),
    )}
    if not inputs <= set(plan.snapshot.roots) or plan.video_path != plan.pack.video_path:
        raise ValueError("The recording plan refers to inputs outside its source snapshot.")
    _number(plan.duration, "Video duration", positive=True)
    _number(plan.fps, "Video frame rate", positive=True)
    if any(type(value) is not int or value <= 0 for value in (plan.width, plan.height)):
        raise ValueError("The recording plan must have positive integer video dimensions.")
    prompts = {prompt.stem: prompt for prompt in plan.pack.prompts}
    recording_paths = set(plan.take.recordings)
    occurrences = set()
    for clip in plan.clips:
        _number(clip.start, "Recording timestamp")
        _number(clip.duration, "Recording duration", positive=True)
        prompt = prompts.get(recording_stem(clip.path))
        occurrence = (clip.path, clip.start)
        if (
            clip.path not in recording_paths or prompt is None
            or clip.start not in prompt.timestamps or clip.characters != prompt.characters
            or occurrence in occurrences
        ):
            raise ValueError("A recording clip does not match its source prompt metadata.")
        occurrences.add(occurrence)
    expected = {
        (path, start) for path in plan.take.recordings
        if (prompt := prompts.get(recording_stem(path))) is not None
        for start in prompt.timestamps
    }
    if occurrences != expected:
        raise ValueError("The recording plan omits matched prompt occurrences.")
    for item in (plan.pack, plan.take):
        _verify_discovered_item(item)
    _verify_input_snapshot(plan.snapshot)


def _thread_options() -> tuple[list[str], list[str]]:
    threads = current_ffmpeg_threads()
    if threads is None:
        raise RuntimeError("Recording FFmpeg work must hold an explicit export resource admission.")
    return ["-filter_threads", str(threads), "-filter_complex_threads", str(threads)], [
        "-threads", str(threads),
    ]


def _frames(seconds: float) -> int:
    _number(seconds, "Audio timeline duration")
    if seconds > (2**63 - 1) / (_RATE * _MIX_FRAME_BYTES):
        raise MediaError("Audio duration exceeds the supported disk-backed timeline range.")
    return round(seconds * _RATE)


def _decode_pcm(
    media: MediaTools, source: Path, destination: Path, duration: float, *, recording: bool,
) -> int:
    with export_resources.acquire(estimate_media_work(extra_bytes=1024 * 1024)):
        filters, threads = _thread_options()
        media.run([
            media.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-y", *filters, *threads, "-err_detect", "explode", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(_CHANNELS), "-ar", str(_RATE),
            "-c:a", "pcm_f32le", *threads, "-f", "f32le",
            # A corrupt duration must not allow a decoder to fill the entire disk.
            "-fs", str((_frames(duration) + _RATE) * _PCM_FRAME_BYTES), str(destination),
        ], f"Decoding recording mix source: {source.name}")
    size = destination.stat().st_size
    if not size or size % _PCM_FRAME_BYTES:
        raise MediaError(f"Invalid decoded PCM length for {source.name}.")
    frames = size // _PCM_FRAME_BYTES
    if frames >= _frames(duration) + _RATE:
        raise MediaError(f"Decoded audio exceeds its declared duration: {source.name}.")
    if recording and abs(frames - _frames(duration)) > 2:
        raise MediaError(f"Decoded recording duration disagrees with its metadata: {source.name}.")
    return frames


def _mix_pcm(
    soundtrack: Path, decoded: Path, starts: tuple[float, ...], gain: float, total_frames: int,
) -> None:
    with decoded.open("rb") as source, soundtrack.open("r+b") as output:
        frame = 0
        while raw := source.read(_CHUNK_FRAMES * _PCM_FRAME_BYTES):
            check_cancelled()
            samples = np.frombuffer(raw, dtype="<f4").astype("<f8")
            if not np.isfinite(samples).all():
                raise MediaError("A recording mix source contains non-finite PCM samples.")
            try:
                with np.errstate(over="raise", invalid="raise"):
                    samples *= gain
                    for start in starts:
                        check_cancelled()
                        offset = _frames(start) + frame
                        count = min(len(samples) // _CHANNELS, total_frames - offset)
                        if count <= 0:
                            continue
                        output.seek(offset * _MIX_FRAME_BYTES)
                        existing = output.read(count * _MIX_FRAME_BYTES)
                        if len(existing) != count * _MIX_FRAME_BYTES:
                            raise MediaError("The staged recording soundtrack was truncated.")
                        mixed = np.frombuffer(existing, dtype="<f8").copy()
                        mixed += samples[:count * _CHANNELS]
                        output.seek(offset * _MIX_FRAME_BYTES)
                        output.write(mixed.tobytes())
            except FloatingPointError as error:
                raise MediaError("Audio gains or overlapping samples exceed the mixer's range.") from error
            frame += len(samples) // _CHANNELS


def _soundtrack(media: MediaTools, plan: RecordingPlan, stage: Path) -> Path:
    total_frames = _frames(plan.duration)
    path = stage / "soundtrack.f64"
    with path.open("xb") as output:
        output.truncate(total_frames * _MIX_FRAME_BYTES)
    sources: dict[Path, list[RecordingClip]] = {}
    for clip in plan.clips:
        sources.setdefault(clip.path, []).append(clip)
    work = [
        (
            source, clips[0].duration,
            tuple(clip.start for clip in clips if clip.start < plan.duration), plan.voices_gain, True,
        )
        for source, clips in sources.items()
    ]
    if plan.backing_path is not None:
        work.append((plan.backing_path, plan.backing_duration, (0.0,), plan.backing_gain, False))
    decoded = stage / "source.f32"
    for index, (source, duration, starts, gain, recording) in enumerate(work, 1):
        check_cancelled()
        report(f"Mixing source {index}/{len(work)}: {source.name}", 0.4 * (index - 1) / len(work))
        _decode_pcm(media, source, decoded, duration, recording=recording)
        with export_resources.acquire(estimate_media_work(extra_bytes=1024 * 1024)):
            _mix_pcm(path, decoded, starts, gain, total_frames)
        decoded.unlink()
    return path


def _encode(
    media: MediaTools, plan: RecordingPlan, soundtrack: Path, staged: Path, *, format: str,
) -> None:
    preview = format == "preview"
    width, height = plan.width + plan.width % 2, plan.height + plan.height % 2
    estimate = estimate_media_work(
        width, height, frame_buffers=2 if preview else 16,
        extra_bytes=4 * 1024 * 1024, cpu_threads=1 if preview else 2,
    )
    with export_resources.acquire(estimate):
        filters, threads = _thread_options()
        if preview:
            # Shift the video stream's own origin, not the container's possibly
            # different audio origin. This agrees with the MP4 setpts timeline.
            input_options = ["-copyts", "-itsoffset", f"{-plan.video_start:.9f}"]
            options = ["-c:v", "copy", "-copytb", "1", "-c:a", "pcm_s16le"]
            duration_options = []
            container = [
                "-avoid_negative_ts", "disabled",
                "-cluster_time_limit", "1000", "-cluster_size_limit", "1048576", "-f", "matroska",
            ]
        else:
            input_options = []
            duration_options = ["-t", f"{plan.duration:.9f}"]
            video_options = (
                ["-c:v", "libopenh264", "-b:v", str(max(
                    2_000_000, int(width * height * plan.fps / 5),
                ))]
                if plan.encoder == "libopenh264" else
                ["-c:v", "mpeg4", "-q:v", "3", "-vtag", "mp4v"]
            )
            options = [
                "-vf", f"setpts=PTS-STARTPTS,pad={width}:{height}:0:0,format=yuv420p",
                *video_options, "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                "-c:a", "aac", "-b:a", "192k",
            ]
            container = ["-movflags", "+faststart", "-f", "mp4"]
        command = [
            media.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-xerror",
            "-y", "-stats_period", "0.5", "-progress", "pipe:1", *filters,
            *input_options, *threads, "-err_detect", "explode", "-i", str(plan.video_path),
            "-f", "f64le", "-ar", str(_RATE), "-ac", str(_CHANNELS),
            *threads, "-i", str(soundtrack), "-map", "0:v:0", "-map", "1:a:0",
            "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
            "-af", "alimiter=limit=0.95:level=false:attack=5:release=50:latency=true",
            *options, "-ar", str(_RATE), "-ac", str(_CHANNELS),
            *duration_options, *threads, *container, str(staged),
        ]

        def progress(value: VideoEncodingProgress) -> None:
            fraction = min(1.0, (value.frames or 0) / max(1.0, plan.duration * plan.fps))
            report(
                "Muxing recording preview..." if preview else "Encoding recording video...",
                0.4 + 0.45 * fraction,
            )

        media._run_video_conversion(command, progress)


def _validate_output(
    media: MediaTools, plan: RecordingPlan, path: Path, *, format: str,
) -> None:
    check_cancelled()
    info = media.probe(path)
    audio = media.probe_audio(path)
    duration, rate, start = _video_timing(media, path)
    fps = float(Fraction(rate))
    preview = format == "preview"
    expected_codec = plan.video_codec if preview else (
        "h264" if plan.encoder == "libopenh264" else "mpeg4"
    )
    expected_audio = "pcm_s16le" if preview else "aac"
    width = plan.width if preview else plan.width + plan.width % 2
    height = plan.height if preview else plan.height + plan.height % 2
    tolerance = max(1 / plan.fps, 1 / _RATE) + 0.001
    # Ogg's empty Theora duplicate-frame packets need not survive remuxing:
    # Matroska holds the last picture while the full-length PCM clock continues.
    duration_mismatch = (
        duration > plan.duration + tolerance
        or not math.isfinite(info.duration) or abs(info.duration - plan.duration) > tolerance
        if preview else abs(duration - plan.duration) > tolerance
    )
    if (
        info.width != width or info.height != height or abs(start) > 0.001
        or not info.has_audio or info.video_codec != expected_codec
        or info.audio_codec != expected_audio
        or (not preview and info.pixel_format != "yuv420p") or info.audio_channels != _CHANNELS
        or info.audio_sample_rate != _RATE
        or not math.isfinite(audio.duration) or abs(audio.duration - plan.duration) > 0.05
        or abs(fps - plan.fps) > max(0.01, plan.fps * 0.001)
        or duration_mismatch
    ):
        raise MediaError("Rendered video does not match the recording plan's video/audio properties.")
    with export_resources.acquire(estimate_media_work(
        info.width, info.height, frame_buffers=8,
    )):
        filters, threads = _thread_options()
        media.run([
            media.ffmpeg, "-v", "error", "-nostdin", "-xerror", *filters, *threads,
            "-err_detect", "explode", "-i", str(path),
            "-map", "0", *threads, "-f", "null", os.devnull,
        ], f"Decoding recording output: {path.name}")


def _destination(plan: RecordingPlan, destination: Path, *, format: str) -> Path:
    destination = _safe_path(destination)
    suffix = ".mkv" if format == "preview" else ".mp4"
    if destination.suffix.casefold() != suffix:
        raise ValueError(f"The {format} destination must have the {suffix} extension.")
    for root in (plan.pack.path, plan.take.path):
        if destination.is_relative_to(root):
            raise ValueError("Save the rendered video outside the original pack and take folders.")
    for source in plan.snapshot.roots:
        path = Path(source)
        if not path.exists():
            raise SourceChangedError(
                f"Recording input disappeared: {path}. Refresh the recording library "
                "and prepare the mix again."
            )
        if destination == path or (destination.exists() and os.path.samefile(destination, path)):
            raise ValueError("The destination must not replace an original input file.")
    if destination.exists() and not destination.is_file():
        raise ValueError(f"The destination is not a regular file: {destination}")
    return destination


def render_recording(
    media: MediaTools,
    plan: RecordingPlan,
    destination: Path,
    *,
    format: str = "mp4",
    overwrite: bool = False,
) -> RecordingRenderResult:
    if format not in {"mp4", "preview"}:
        raise ValueError("Only MP4 export or Matroska preview is supported.")
    if format == "preview" and plan.video_codec not in _PREVIEW_CODECS:
        raise MediaError(
            f"Fast Matroska preview does not support source video codec {plan.video_codec!r}. "
            "Export MP4 instead; preview never silently transcodes the source video."
        )
    if not math.isfinite(plan.video_start):
        raise ValueError("Video start must be finite.")
    if not isinstance(overwrite, bool):
        raise ValueError("Overwrite permission must be explicitly true or false.")
    _number(plan.duration, "Video duration", positive=True)
    _number(plan.voices_gain, "Voices gain")
    _number(plan.backing_gain, "Backing gain")
    _frames(plan.duration)
    if plan.encoder not in {"libopenh264", "mpeg4"}:
        raise ValueError("The recording plan has an unsupported encoder.")
    destination = _destination(plan, destination, format=format)
    with operation_scope(), path_leases(
        read_paths=[plan.pack.path, plan.take.path, *plan.snapshot.roots],
        write_paths=[destination],
    ):
        _verify_sources(plan)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Destination exists; confirm overwrite first: {destination}")
        previous = SourceSnapshot.capture([destination]) if destination.exists() else None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".recording-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            longest = max([clip.duration for clip in plan.clips] + [plan.backing_duration])
            required = (
                _frames(plan.duration) * _MIX_FRAME_BYTES
                + (_frames(longest) + _RATE) * _PCM_FRAME_BYTES
                + plan.video_path.stat().st_size * 4 + 16 * 1024 * 1024
            )
            if shutil.disk_usage(stage).free < required:
                raise OSError(
                    f"Recording export needs approximately {required / 1024**3:.2f} GiB of "
                    "free space for its disk-backed mix and staged video."
                )
            soundtrack = _soundtrack(media, plan, stage)
            staged = stage / ("render.mkv" if format == "preview" else "render.mp4")
            _verify_sources(plan)
            _encode(media, plan, soundtrack, staged, format=format)
            report("Validating staged recording video...", 0.9)
            _validate_output(media, plan, staged, format=format)
            _verify_sources(plan)
            check_cancelled()
            with critical_stage("Publishing recording video; cancellation is deferred..."):
                _destination(plan, destination, format=format)
                _verify_sources(plan)
                if previous is not None:
                    previous.verify()
                elif destination.exists():
                    raise FileExistsError("The destination appeared during rendering; confirm overwrite.")
                # Keep rollback outside the temporary tree: a failed restore must
                # retain the previous output rather than delete its last copy.
                backup = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
                if previous is not None:
                    os.link(destination, backup)
                published = False
                try:
                    if previous is not None:
                        os.replace(staged, destination)
                    else:
                        # Atomic no-clobber publication, even if another writer races this export.
                        os.link(staged, destination)
                    published = True
                    report("Validating published recording video...", 0.95)
                    _validate_output(media, plan, destination, format=format)
                    _verify_sources(plan)
                except BaseException:
                    try:
                        if published:
                            if previous is not None:
                                os.replace(backup, destination)
                            else:
                                destination.unlink()
                    except OSError as error:
                        raise OSError(
                            f"Recording publication failed and could not be rolled back. "
                            f"The previous output is retained at {backup}."
                            if previous is not None else
                            f"Recording validation failed; could not remove {destination}."
                        ) from error
                    if backup.exists():
                        backup.unlink()
                    raise
                if backup.exists():
                    backup.unlink()
            report("Recording video ready.", 1.0)
            encoder = f"copy ({plan.video_codec})" if format == "preview" else plan.encoder
            return RecordingRenderResult(destination, plan.warnings, encoder)
