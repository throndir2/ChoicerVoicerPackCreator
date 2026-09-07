"""Materialized, transactional scene edits; source media and recordings stay immutable."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from choicer_voicer_pack_creator.media import MediaError, MediaInfo, MediaTools
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    CaptionFragment,
    PackProject,
)
from choicer_voicer_pack_creator.operations import (
    SourceSnapshot,
    check_cancelled,
    critical_stage,
    operation_scope,
    path_leases,
    report,
)
from choicer_voicer_pack_creator.project_io import ProjectStore

SceneEditMode = Literal["cut", "extract"]
_MIN_DURATION = 0.05
_BOUNDARY_ROUNDING = 0.0005 + 1e-9
_THREADS = "2"


@dataclass(frozen=True, slots=True)
class SceneEditPlan:
    start: float
    end: float
    mode: SceneEditMode
    duration: float
    kept_ranges: tuple[tuple[float, float], ...]
    warnings: tuple[str, ...] = ()


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number.")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    return float(value)


def _range(start: object, end: object, label: str, duration: float | None = None) -> None:
    first, last = _number(start, f"{label} start"), _number(end, f"{label} end")
    if first < 0 or last <= first or (duration is not None and last > duration + 1e-6):
        raise ValueError(f"Correct {label}: timestamps must be ordered and inside the source video.")


def _map_time(value: float, plan: SceneEditPlan) -> float:
    offset = 0.0
    for start, end in plan.kept_ranges:
        if value <= end:
            return offset + max(0.0, value - start)
        offset += end - start
    return plan.duration


def _intersection(
    start: float, end: float, plan: SceneEditPlan,
) -> tuple[float, float] | None:
    pieces = [
        (max(start, first), min(end, last))
        for first, last in plan.kept_ranges if min(end, last) > max(start, first)
    ]
    if not pieces:
        return None
    return _map_time(pieces[0][0], plan), _map_time(pieces[-1][1], plan)


def _clipped(start: float, end: float, mapped: tuple[float, float]) -> bool:
    return end - start > mapped[1] - mapped[0] + 1e-9


def _remap_project(project: PackProject, plan: SceneEditPlan) -> tuple[PackProject, tuple[str, ...]]:
    result = copy.deepcopy(project)
    result.video_duration = plan.duration
    result.preserve_source_video = False
    result.segments = []
    removed_segments = removed_captions = removed_drafts = 0
    clipped_text = False
    for index, segment in enumerate(project.segments, 1):
        check_cancelled()
        _range(segment.start, segment.end, f"segment {index}", project.video_duration)
        if segment.audio_mode not in {"file", "video"}:
            raise ValueError(f"Correct segment {index}: unknown prompt audio mode.")
        mapped = _intersection(segment.start, segment.end, plan)
        if mapped is None:
            removed_segments += 1
            continue
        clipped = _clipped(segment.start, segment.end, mapped)
        if clipped and (segment.audio_mode == "file" or not segment.source_range_known):
            raise ValueError(
                f"Segment {index} partially intersects the edit and uses a preserved recording "
                "or an unknown source range. Move the edit boundaries to keep or exclude the "
                "whole prompt, or explicitly regenerate its audio from a known video range first."
            )
        if round(mapped[0], 6) >= round(mapped[1], 6):
            raise ValueError(
                f"Segment {index} would be too short to save its retained timestamps. "
                "Adjust the edit boundaries or the segment timing first."
            )
        clipped_text |= clipped
        result.segments.append(replace(
            copy.deepcopy(segment), start=mapped[0], end=mapped[1],
        ))
    result.sort_segments()

    result.source_captions = []
    for index, caption in enumerate(project.source_captions, 1):
        check_cancelled()
        _range(caption.start, caption.end, f"source caption {index}")
        for fragment in caption.fragments:
            if fragment.start is not None:
                _number(fragment.start, f"source caption {index} fragment timestamp")
        mapped = _intersection(caption.start, caption.end, plan)
        if mapped is None:
            removed_captions += 1
            continue
        clipped_text |= _clipped(caption.start, caption.end, mapped)
        fragments = tuple(
            CaptionFragment(
                fragment.text,
                min(
                    math.nextafter(mapped[1], mapped[0]),
                    max(mapped[0], _map_time(fragment.start, plan)),
                ) if fragment.start is not None else None,
            )
            for fragment in caption.fragments
        )
        result.source_captions.append(replace(
            caption, start=mapped[0], end=mapped[1], fragments=fragments,
        ))

    if project.analysis_review is not None:
        remapped_rows: dict[str, list[AnalysisDraftRow]] = {}
        for name in ("local_rows", "refined_rows"):
            rows = []
            for index, row in enumerate(getattr(project.analysis_review, name), 1):
                check_cancelled()
                label = f"{name.removesuffix('_rows')} analysis draft row {index}"
                try:
                    start, end = float(row.start), float(row.end)
                    _range(start, end, label, project.video_duration)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"Correct the unfinished or invalid timestamps in {label} in Analyze "
                        "before editing the video; its review text has not been discarded."
                    ) from error
                mapped = _intersection(start, end, plan)
                if mapped is None:
                    removed_drafts += 1
                    continue
                clipped_text |= _clipped(start, end, mapped)
                if mapped[1] <= mapped[0]:
                    raise ValueError(f"Correct {label}: the retained time range is too short.")
                rows.append(replace(row, start=repr(mapped[0]), end=repr(mapped[1])))
            remapped_rows[name] = rows
        result.analysis_review = replace(project.analysis_review, **remapped_rows)

    warnings = []
    if removed_segments:
        warnings.append(
            f"{removed_segments} segment(s) entirely outside the retained video will be removed."
        )
    if removed_captions or removed_drafts:
        warnings.append(
            f"{removed_captions} source caption(s) and {removed_drafts} analysis draft row(s) "
            "entirely outside the retained video will be removed."
        )
    if clipped_text:
        warnings.append(
            "Some dialogue is clipped. Full caption text, speakers, images, and retained analysis "
            "review text are preserved; review clipped dialogue text and fragment timing afterward."
        )
    return result, tuple(warnings)


def plan_scene_edit(
    project: PackProject, start: float, end: float, mode: SceneEditMode,
) -> SceneEditPlan:
    """Validate an edit without creating media or changing the project.

    Fully excluded prompts/drafts are removed. Partially retained video dialogue
    keeps its full text for review; partial preserved recordings are never guessed.
    """
    check_cancelled()
    if mode not in {"cut", "extract"}:
        raise ValueError("Scene edit mode must be 'cut' or 'extract'.")
    if not project.video_path or not Path(project.video_path).is_file():
        raise ValueError("A readable source video is required before editing a scene.")
    duration = _number(project.video_duration, "Source video duration")
    start, end = _number(start, "In"), _number(end, "Out")
    if (
        duration <= 0
        or start < -_BOUNDARY_ROUNDING or end > duration + _BOUNDARY_ROUNDING
    ):
        raise ValueError("Choose an ordered In/Out range inside the source video.")
    # The editor displays milliseconds, so the displayed maximum can round
    # just beyond the measured source duration.
    start, end = min(duration, max(0.0, start)), min(duration, max(0.0, end))
    if end <= start:
        raise ValueError("Choose an ordered In/Out range inside the source video.")
    kept = (
        ((start, end),) if mode == "extract" else
        tuple((a, b) for a, b in ((0.0, start), (end, duration)) if b > a)
    )
    remaining = math.fsum(b - a for a, b in kept)
    if remaining < _MIN_DURATION - 1e-9:
        raise ValueError(
            "The edit must retain at least 0.05 seconds of video; the entire video cannot be deleted."
        )
    plan = SceneEditPlan(start, end, mode, remaining, kept)
    _, warnings = _remap_project(project, plan)
    return replace(plan, warnings=warnings)


def _video_timing(
    media: MediaTools, path: Path, duration_hint: float, fps: float,
) -> tuple[float, float]:
    """Read the last video packets, not container duration (which can include an offset/audio tail)."""
    def probe(start: float | None) -> dict:
        return json.loads(media.run([
            media.ffprobe, "-v", "error", "-select_streams", "v:0",
            *(["-read_intervals", f"{start:.9f}%"] if start is not None else []),
            "-show_streams", "-show_packets",
            "-show_entries", "stream=start_time:packet=pts_time,duration_time",
            "-of", "json", str(path),
        ], f"Checking video timestamps in {path.name}").stdout)

    data = probe(max(0.0, duration_hint - 2.0))
    streams = data.get("streams", [])
    if not streams:
        raise MediaError(f"{path.name} does not contain a video stream.")
    origin = float(streams[0].get("start_time", 0.0))
    if not data.get("packets"):
        # Some valid Matroska files yield no packets after a seek, even to zero.
        # Read sequentially instead of repeating the same unsuccessful seek.
        data = probe(None)
    ends = []
    for packet in data.get("packets", []):
        check_cancelled()
        if "pts_time" in packet:
            timestamp = float(packet["pts_time"])
            duration = float(packet.get("duration_time", 0.0)) or 1.0 / fps
            ends.append(timestamp + duration)
    if not ends or not math.isfinite(origin) or not all(math.isfinite(end) for end in ends):
        raise MediaError(f"Could not determine the decoded video timeline for {path.name}.")
    return origin, max(ends) - origin


def _audio_filters(
    ranges: tuple[tuple[float, float], ...], duration: float, *, origin: float | None,
) -> list[str]:
    # Align source audio to the VIDEO origin, preserving real initial silence and
    # filling timestamp gaps. A separate backing file has its own zero origin.
    timestamps = "PTS-STARTPTS" if origin is None else f"PTS-({origin:.9f})/TB"
    filters = [
        f"[0:a:0]asetpts={timestamps},aresample=async=1:first_pts=0,"
        f"apad=whole_dur={duration:.9f},atrim=duration={duration:.9f},"
        f"asplit={len(ranges)}" + "".join(f"[a{i}]" for i in range(len(ranges))),
    ]
    for index, (start, end) in enumerate(ranges):
        filters.append(
            f"[a{index}]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={end - start:.9f},atrim=duration={end - start:.9f}[a{index}out]"
        )
    filters.append(
        "".join(f"[a{i}out]" for i in range(len(ranges)))
        + f"concat=n={len(ranges)}:v=0:a=1[aout]"
    )
    return filters


def _boundary_frame(media: MediaTools, path: str, position: float, origin: float) -> float:
    if position == 0:
        return 0.0
    timestamp = position + origin
    data = json.loads(media.run([
        media.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"{timestamp:.9f}%{timestamp + 1:.9f}",
        "-show_packets", "-show_entries", "packet=pts_time", "-of", "json", path,
    ], "Locating the video frame that overlaps the scene boundary").stdout)
    starts = [
        float(packet["pts_time"]) - origin
        for packet in data.get("packets", []) if "pts_time" in packet
        and math.isfinite(float(packet["pts_time"]))
        and float(packet["pts_time"]) <= timestamp + 1e-9
    ]
    if not starts:
        raise MediaError("Could not locate the source frame at the scene boundary.")
    return max(0.0, max(starts))


def _render_video(
    project: PackProject, plan: SceneEditPlan, media: MediaTools, info: MediaInfo,
    origin: float, destination: Path,
) -> None:
    first, last = plan.kept_ranges[0][0], plan.kept_ranges[-1][1]
    first_frame = _boundary_frame(media, project.video_path, first, origin)
    video = (
        f"[0:v:0]settb=AVTB,setpts=PTS-STARTPTS,trim=start={first_frame:.9f}:end={last:.9f}"
    )
    if len(plan.kept_ranges) == 2:
        right_frame = _boundary_frame(media, project.video_path, plan.end, origin)
        # Remap video PTS directly: A/V concat pads each part to whole frames,
        # which otherwise introduces silence and shifts later dialogue/backing.
        video += (
            f",select='lt(t,{plan.start:.9f})+gte(t,{right_frame:.9f})',"
            f"setpts='if(gte(T,{plan.start:.9f}),"
            f"max(PTS-{plan.end - plan.start:.9f}/TB,{plan.start:.9f}/TB),PTS)'"
        )
    # Keep the frame already on screen at In, even for a scene shorter than a
    # frame. Clamp that first frame to zero without shifting the later frames.
    video += (
        f",setpts='max(PTS-{first:.9f}/TB,0)',"
        f"tpad=stop_mode=clone:stop_duration={plan.duration:.9f},"
        f"trim=duration={plan.duration:.9f}[vout]"
    )
    filters = [video]
    if info.has_audio:
        filters.extend(_audio_filters(
            plan.kept_ranges, project.video_duration, origin=origin,
        ))
    media.run([
        media.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n",
        "-threads", _THREADS, "-filter_complex_threads", _THREADS,
        "-copyts", "-i", project.video_path,
        "-filter_complex", ";".join(filters), "-map", "[vout]",
        *(["-map", "[aout]", "-c:a", "pcm_s16le"] if info.has_audio else ["-an"]),
        "-c:v", "ffv1", "-level", "3", "-threads:v", _THREADS,
        "-fps_mode", "passthrough", "-enc_time_base:v", "1:1000000",
        "-t", f"{plan.duration:.9f}", str(destination),
    ], "Rendering the retained video and synchronized audio (lossless FFV1)")


def _render_backing(
    project: PackProject, plan: SceneEditPlan, media: MediaTools, destination: Path,
    video_origin: float,
) -> None:
    origin = (
        video_origin
        if Path(project.backing_track_path).resolve() == Path(project.video_path).resolve()
        else None
    )
    media.run([
        media.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n",
        "-threads", _THREADS, "-filter_complex_threads", _THREADS,
        "-copyts", "-i", project.backing_track_path,
        "-filter_complex", ";".join(_audio_filters(
            plan.kept_ranges, project.video_duration, origin=origin,
        )),
        "-map", "[aout]", "-c:a", "pcm_s16le", "-rf64", "auto", str(destination),
    ], "Rendering the same retained ranges in the backing track")


def _validate_audio(media: MediaTools, path: Path, duration: float) -> None:
    info = media.probe_audio(path)
    if info.sample_rate <= 0 or info.channels <= 0 or info.codec != "pcm_s16le":
        raise MediaError(f"The edited audio has an invalid format: {path.name}")
    # Container duration can describe the video rather than the shorter audio.
    # Decode to a null sink for bounded-memory, sample-accurate audio end timing.
    output = media.run([
        media.ffmpeg, "-v", "error", "-xerror", "-nostdin", "-threads", _THREADS,
        "-i", str(path), "-map", "0:a:0", "-vn", "-progress", "pipe:1",
        "-nostats", "-f", "null", os.devnull,
    ], f"Checking decoded audio duration in {path.name}").stdout
    times = [int(line.split("=", 1)[1]) / 1e6 for line in output.splitlines()
             if line.startswith("out_time_us=") and line.split("=", 1)[1] != "N/A"]
    if not times or abs(times[-1] - duration) > max(0.002, 2 / info.sample_rate):
        raise MediaError(f"The edited audio duration does not match the scene: {path.name}")


def execute_scene_edit(
    project: PackProject, start: float, end: float, mode: SceneEditMode,
    output_parent: Path, media: MediaTools, *, title: str | None = None,
) -> PackProject:
    """Publish new FFV1/PCM media and a saved snapshot; return the retimed project.

    The selected parent must already exist. Each success owns a new permanent
    directory containing project.cvpack.json. Cancellation/failure removes only
    this operation's staging/output.
    Video boundaries are accurate within one source frame, audio within samples.
    FFV1 preserves decoded pixels; PCM stores 16-bit audio without lossy encoding.
    """
    with operation_scope():
        project = copy.deepcopy(project)
        plan = plan_scene_edit(project, start, end, mode)
        result, _ = _remap_project(project, plan)
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ValueError("A scene title must not be empty.")
            result.title = title.strip()
        parent = Path(output_parent).resolve()
        if not parent.is_dir():
            raise ValueError("Choose an existing output parent directory for the edited media.")
        if project.source_pack_path:
            source_pack = Path(project.source_pack_path).resolve()
            if parent == source_pack or parent.is_relative_to(source_pack):
                raise ValueError("Choose an output directory outside the original source pack.")
        identifier = uuid.uuid4().hex
        target = parent / f"scene-{mode}-{identifier}"
        stage = parent / f".scene-{identifier}.staging"
        sources = [Path(project.video_path)]
        sources.extend(
            Path(value) for value in (
                project.backing_track_path, result.icon_path,
                *(value for segment in result.segments
                  for value in (segment.audio_path, segment.image_path)),
            ) if value
        )
        with path_leases(read_paths=sources, write_paths=(stage, target)):
            snapshot = SourceSnapshot.capture(sources)
            info = media.probe(Path(project.video_path))
            if not math.isfinite(info.fps) or info.fps <= 0 or info.width <= 0 or info.height <= 0:
                raise MediaError("The source video needs readable dimensions and frame timing.")
            origin, actual_duration = _video_timing(
                media, Path(project.video_path), project.video_duration, info.fps,
            )
            tolerance = 1 / info.fps + 0.002
            if abs(actual_duration - project.video_duration) > tolerance:
                raise ValueError(
                    "The source video's duration differs from this project. Reload the video and "
                    "correct the timeline before editing it."
                )
            check_cancelled()
            stage.mkdir()
            published = success = False
            try:
                video = stage / "video.mkv"
                backing = stage / "backing.wav"
                _render_video(project, plan, media, info, origin, video)
                if project.backing_track_path:
                    _render_backing(project, plan, media, backing, origin)
                report("Validating the complete edited media...", None)
                edited_info = media.probe(video)
                edited_origin, actual = _video_timing(media, video, plan.duration, info.fps)
                if (
                    edited_info.video_codec != "ffv1"
                    or edited_info.width != info.width or edited_info.height != info.height
                    or edited_info.has_audio != info.has_audio
                    or abs(edited_origin) > 0.002
                    or abs(actual - plan.duration) > tolerance
                    or not math.isfinite(edited_info.duration)
                    or abs(edited_info.duration - plan.duration) > tolerance
                ):
                    raise MediaError("The edited video has an unexpected format or duration.")
                media.decode(video)
                if info.has_audio:
                    _validate_audio(media, video, plan.duration)
                if project.backing_track_path:
                    media.decode(backing)
                    _validate_audio(media, backing, plan.duration)
                result.video_path = str(target / video.name)
                result.backing_track_path = (
                    str(target / backing.name) if project.backing_track_path else ""
                )
                for segment in result.segments:
                    if (
                        segment.audio_mode == "video" and segment.audio_path
                        and Path(segment.audio_path).resolve() == Path(project.video_path).resolve()
                    ):
                        segment.audio_path = result.video_path
                report("Saving the edited project snapshot...", None)
                check_cancelled()
                # Save sibling media as relative paths so the complete scene
                # folder remains portable after publication or a later move.
                staged_project = copy.deepcopy(result)
                staged_project.video_path = str(video)
                staged_project.backing_track_path = str(backing) if project.backing_track_path else ""
                for segment in staged_project.segments:
                    if segment.audio_mode == "video" and segment.audio_path == result.video_path:
                        segment.audio_path = str(video)
                project_snapshot = stage / "project.cvpack.json"
                ProjectStore.save(staged_project, project_snapshot)
                ProjectStore.load(project_snapshot)
                snapshot.verify()
                with critical_stage("Publishing the verified scene media..."):
                    snapshot.verify()
                    if target.exists():
                        raise FileExistsError(f"The scene output already exists: {target}")
                    stage.rename(target)
                    published = True
                    snapshot.verify()
                success = True
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
                if published and not success:
                    shutil.rmtree(target)
