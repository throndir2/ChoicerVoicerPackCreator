from __future__ import annotations

import getpass
import hashlib
import json
import math
import tempfile
import threading
import wave
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from choicer_voicer_pack_creator.analysis import (
    WHISPER_TRANSCRIPT_NAME,
    WhisperManager,
    analyze_video,
    detect_hardware,
)
from choicer_voicer_pack_creator.export_progress import ExportProgress
from choicer_voicer_pack_creator.exporter import (
    PackExporter,
    is_same_or_within,
    safe_name,
    sha256,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.segment_review import (
    compare_transcript,
    is_nonverbal,
    review_project,
)
from choicer_voicer_pack_creator.separation_types import BackingMode, validate_backing_mode
from choicer_voicer_pack_creator.timeline_audit import audit_timeline_overlaps
from choicer_voicer_pack_creator.validation import PackValidator

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
TRANSCRIBE_GAP_SECONDS = 1.5
TRANSCRIBE_MAX_SECONDS = 900.0

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Padding = Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def reject_null_fields(self):
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null; omit it to leave it unchanged")
        return self


class ProjectPatch(InputModel):
    title: str | None = None
    authors: list[str] | None = None
    readme: str | None = None
    video_path: str | None = None
    backing_track_path: str | None = None
    icon_path: str | None = None
    head_padding: Padding | None = None
    tail_padding: Padding | None = None
    video_height: Annotated[int, Field(ge=144, le=2160)] | None = None
    video_fps: Annotated[int, Field(ge=1, le=120)] | None = None
    preserve_source_video: bool | None = None
    auto_speaker_matching: bool | None = None


class SegmentPatch(InputModel):
    id: str | None = None
    start: Seconds | None = None
    end: Seconds | None = None
    caption: str | None = None
    characters: list[str] | None = None
    audio_mode: Literal["video", "file"] | None = None
    audio_path: str | None = None
    image_path: str | None = None
    speaker_assignment: Literal["manual", "excluded"] | None = None


class MergeGroup(InputModel):
    segment_ids: Annotated[list[str], Field(min_length=2, max_length=50)]
    caption: str | None = None
    characters: list[str] | None = None


@dataclass
class ProjectSnapshot:
    project: PackProject
    path: Path | None = None
    dirty: bool = False
    saved_hash: str | None = None
    project_id: str | None = None
    loading: bool = False

    @property
    def revision(self) -> str:
        project_data = self.project.to_dict()
        for name in ("video_duration", "head_padding", "tail_padding"):
            project_data[name] = float(project_data[name])
        for segment in project_data["segments"]:
            segment["start"] = float(segment["start"])
            segment["end"] = float(segment["end"])
        payload = {
            "project_id": self.project_id,
            "loading": self.loading,
            "project": project_data,
            "path": str(self.path) if self.path else None,
            "dirty": self.dirty,
            "saved_hash": self.saved_hash,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()

    def copy(self) -> ProjectSnapshot:
        return ProjectSnapshot(
            deepcopy(self.project), self.path, self.dirty, self.saved_hash, self.project_id, self.loading
        )


def require_ready(snapshot: ProjectSnapshot) -> None:
    if snapshot.loading:
        raise ValueError("Project is still loading. Wait and call get_project again before editing.")


def require_revision(snapshot: ProjectSnapshot, expected: str) -> None:
    require_ready(snapshot)
    if snapshot.revision != expected:
        raise ValueError("Project changed. Call get_project and retry using its current revision.")


def local_path(value: str, *, exists: bool = True, directory: bool = False) -> Path:
    path = Path(value)
    if not value or not path.is_absolute():
        raise ValueError("Use an absolute local filesystem path, not a URL or relative path.")
    path = path.resolve()
    if exists and not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"{'Directory' if directory else 'File'} does not exist: {path}")
    return path


def protected_assets(project: PackProject) -> list[Path]:
    values = [project.video_path, project.backing_track_path, project.icon_path]
    values.extend(value for item in project.segments for value in (item.audio_path, item.image_path))
    return [Path(value).resolve() for value in values if value]


def save_snapshot(snapshot: ProjectSnapshot, destination: Path, overwrite: bool) -> ProjectSnapshot:
    require_ready(snapshot)
    if not destination.name.casefold().endswith(".cvpack.json"):
        raise ValueError("Project destination must end in .cvpack.json.")
    assets = protected_assets(snapshot.project)
    write_paths = [
        destination,
        ProjectStore.previous_path(destination),
        destination.with_name(destination.name + ".partial"),
        destination.with_name(destination.name + ".previous.partial"),
    ]
    if any(path in assets for path in write_paths) or (
        snapshot.project.source_pack_path
        and is_same_or_within(destination, Path(snapshot.project.source_pack_path))
    ):
        raise ValueError("Refusing to save over source assets or inside an imported pack.")
    if destination.exists():
        current_hash = sha256(destination)
        if destination == snapshot.path:
            if current_hash != snapshot.saved_hash:
                raise ValueError("Saved project changed on disk. Reopen it or save to a new path.")
        elif not overwrite:
            raise ValueError("Destination exists. Use a new path or explicitly set overwrite=true.")
    elif destination == snapshot.path and snapshot.saved_hash is not None:
        raise ValueError("Saved project was removed on disk. Save to a new path or reopen it.")
    ProjectStore.save(snapshot.project, destination)
    return ProjectSnapshot(
        snapshot.project, destination, False, sha256(destination), snapshot.project_id
    )


class ProjectAccess(Protocol):
    live: bool

    def bind(self, project_id: str | None = None) -> ProjectAccess: ...

    def list_projects(self) -> dict[str, Any]: ...

    def activate(self, project_id: str) -> ProjectSnapshot: ...

    def open_existing(self, path: Path) -> ProjectSnapshot | None: ...

    def create(self, snapshot: ProjectSnapshot) -> ProjectSnapshot: ...

    def snapshot(self) -> ProjectSnapshot: ...

    def replace(self, snapshot: ProjectSnapshot, expected_revision: str) -> None: ...

    def save(self, destination: Path, expected_revision: str, overwrite: bool) -> ProjectSnapshot: ...

    def show(self, segment_id: str | None, timestamp: float | None) -> None: ...


class HeadlessProjectAccess:
    live = False

    def __init__(self, snapshot: ProjectSnapshot | None = None) -> None:
        self._root = self
        self._project_id: str | None = None
        self._lock = threading.RLock()
        self._active_id = (snapshot.project_id if snapshot else None) or uuid4().hex
        initial = snapshot.copy() if snapshot else ProjectSnapshot(PackProject())
        initial.project_id = self._active_id
        self._projects = {self._active_id: initial}

    @property
    def current(self) -> ProjectSnapshot:
        project_id = self._root._active_id if self._project_id is None else self._project_id
        if project_id not in self._root._projects:
            raise ValueError(f"Unknown project_id: {project_id}")
        return self._root._projects[project_id]

    @current.setter
    def current(self, snapshot: ProjectSnapshot) -> None:
        project_id = self._root._active_id if self._project_id is None else self._project_id
        snapshot.project_id = project_id
        self._root._projects[project_id] = snapshot

    def bind(self, project_id: str | None = None) -> HeadlessProjectAccess:
        with self._root._lock:
            bound = object.__new__(HeadlessProjectAccess)
            bound._root = self._root
            bound._project_id = self.current.project_id if project_id is None else project_id
            bound.snapshot()
            return bound

    def list_projects(self) -> dict[str, Any]:
        with self._root._lock:
            return {
                "active_project_id": self._root._active_id,
                "projects": [
                    {"project_id": item.project_id, "title": item.project.title,
                     "project_path": str(item.path) if item.path else None,
                     "dirty": item.dirty, "loading": item.loading, "revision": item.revision}
                    for item in self._root._projects.values()
                ],
            }

    def activate(self, project_id: str) -> ProjectSnapshot:
        with self._root._lock:
            bound = self.bind(project_id)
            self._root._active_id = project_id
            return bound.snapshot()

    def create(self, snapshot: ProjectSnapshot) -> ProjectSnapshot:
        with self._root._lock:
            if snapshot.path is not None:
                existing = self.open_existing(snapshot.path)
                if existing is not None:
                    return existing
            snapshot = snapshot.copy()
            snapshot.project_id = uuid4().hex
            self._root._projects[snapshot.project_id] = snapshot
            self._root._active_id = snapshot.project_id
            return snapshot.copy()

    def open_existing(self, path: Path) -> ProjectSnapshot | None:
        with self._root._lock:
            for current in self._root._projects.values():
                if current.path == path:
                    return self.activate(current.project_id)
            return None

    def snapshot(self) -> ProjectSnapshot:
        with self._root._lock:
            return self.current.copy()

    def replace(self, snapshot: ProjectSnapshot, expected_revision: str) -> None:
        with self._root._lock:
            require_revision(self.current, expected_revision)
            self.current = snapshot.copy()

    def save(self, destination: Path, expected_revision: str, overwrite: bool) -> ProjectSnapshot:
        with self._root._lock:
            require_revision(self.current, expected_revision)
            for item in self._root._projects.values():
                if item.project_id != self.current.project_id and item.path == destination:
                    raise ValueError("Destination belongs to another open project.")
            self.current = save_snapshot(self.current, destination, overwrite)
            return self.snapshot()

    def show(self, segment_id: str | None, timestamp: float | None) -> None:
        raise ValueError("No editor in headless mode. Restart the MCP server without --headless.")


class PackAutomation:
    def __init__(
        self, access: ProjectAccess, data_root: Path, media: MediaTools | None = None
    ) -> None:
        self.access = access
        self.data_root = data_root
        self._media = media

    def for_project(self, project_id: str | None = None) -> PackAutomation:
        return PackAutomation(self.access.bind(project_id), self.data_root, self._media)

    @property
    def media(self) -> MediaTools:
        if self._media is None:
            self._media = MediaTools()
        return self._media

    @staticmethod
    def describe(snapshot: ProjectSnapshot, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        data = snapshot.project.to_dict()
        segments = data.pop("segments")
        # Captions and analysis drafts can be very large; read them with get_source_captions.
        data["source_caption_count"] = len(data.pop("source_captions"))
        data["has_analysis_review"] = data.pop("analysis_review") is not None
        return {
            "project_id": snapshot.project_id,
            "loading": snapshot.loading,
            "project": data,
            "segments": segments[offset:offset + limit],
            "total_segments": len(segments),
            "next_offset": offset + limit if offset + limit < len(segments) else None,
            "project_path": str(snapshot.path) if snapshot.path else None,
            "dirty": snapshot.dirty,
            "revision": snapshot.revision,
        }

    def get_project(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("offset must be non-negative and limit must be between 1 and 500.")
        return self.describe(self.access.snapshot(), offset, limit)

    def _publish(self, updated: ProjectSnapshot, previous: ProjectSnapshot) -> dict[str, Any]:
        json.dumps(updated.project.to_dict(), allow_nan=False)
        updated.project_id = previous.project_id
        self.access.replace(updated, previous.revision)
        return self.describe(self.access.snapshot())

    def new_project(
        self, video_path: str, title: str, authors: list[str]
    ) -> dict[str, Any]:
        source = local_path(video_path)
        info = self.media.probe(source)
        if not info.has_audio or not math.isfinite(info.duration) or info.duration <= 0:
            raise ValueError("Source must have video, audio, and a finite positive duration.")
        if not title.strip() or not authors or not all(author.strip() for author in authors):
            raise ValueError("Provide a title and at least one non-empty author.")
        project = PackProject(
            title=title, authors=authors, video_path=str(source), video_duration=info.duration
        )
        return self.describe(self.access.create(ProjectSnapshot(project, dirty=True)))

    def open_project(self, path: str) -> dict[str, Any]:
        existing = self.access.open_existing(local_path(path, exists=False))
        if existing is not None:
            return self.describe(existing)
        source = local_path(path)
        before = sha256(source)
        project = ProjectStore.load(source)
        if before != sha256(source):
            raise ValueError("Project changed while opening it. Retry.")
        json.dumps(project.to_dict(), allow_nan=False)
        return self.describe(self.access.create(ProjectSnapshot(project, source, False, before)))

    def import_pack(self, path: str) -> dict[str, Any]:
        result = PackImporter(self.media).import_folder(local_path(path, directory=True))
        json.dumps(result.project.to_dict(), allow_nan=False)
        return self.describe(self.access.create(ProjectSnapshot(result.project, dirty=True)))

    def update_project(self, patch: ProjectPatch, expected_revision: str) -> dict[str, Any]:
        previous = self.access.snapshot()
        require_revision(previous, expected_revision)
        updated = previous.copy()
        fields = patch.model_dump(exclude_unset=True)
        if "video_path" in fields:
            source = local_path(fields["video_path"])
            info = self.media.probe(source)
            if not info.has_audio or not math.isfinite(info.duration) or info.duration <= 0:
                raise ValueError("Source must have video, audio, and a finite positive duration.")
            fields["video_path"] = str(source)
            if str(source) != updated.project.video_path:
                updated.project.source_url = ""
                updated.project.caption_language = ""
                updated.project.source_captions = []
                updated.project.analysis_review = None
            updated.project.video_duration = info.duration
            fields.setdefault("preserve_source_video", False)
        for name in ("backing_track_path", "icon_path"):
            if fields.get(name):
                fields[name] = str(local_path(fields[name]))
        for name, value in fields.items():
            setattr(updated.project, name, value)
        updated.dirty = True
        return self._publish(updated, previous)

    def edit_segments(
        self,
        upsert: list[SegmentPatch],
        delete_ids: list[str],
        expected_revision: str,
    ) -> dict[str, Any]:
        previous = self.access.snapshot()
        require_revision(previous, expected_revision)
        updated = previous.copy()
        project = updated.project
        if len(upsert) + len(delete_ids) > 500:
            raise ValueError("Edit at most 500 segments per call.")
        if not upsert and not delete_ids:
            raise ValueError("Provide at least one segment edit or deletion.")
        touched: set[str] = set()
        for segment_id in delete_ids:
            if segment_id in touched or not project.remove_segment(segment_id):
                raise ValueError(f"Unknown or repeated segment id: {segment_id}")
            touched.add(segment_id)
        changed_ids = []
        for patch in upsert:
            fields = patch.model_dump(exclude_unset=True)
            segment_id = fields.pop("id", None)
            if segment_id is not None:
                segment = project.segment_by_id(segment_id)
                if segment_id in touched or segment is None:
                    raise ValueError(f"Unknown or repeated segment id: {segment_id}")
                touched.add(segment_id)
            else:
                if "start" not in fields or "end" not in fields:
                    raise ValueError("New segments require start and end.")
                segment = Segment(start=fields["start"], end=fields["end"])
                project.segments.append(segment)
            # Imported trigger times are not the original spoken cut.
            if fields.get("audio_mode") == "video" and not segment.source_range_known:
                if "start" not in fields or "end" not in fields:
                    raise ValueError("Regenerating imported audio requires explicit start and end.")
                segment.source_range_known = True
            for name in ("audio_path", "image_path"):
                if fields.get(name):
                    fields[name] = str(local_path(fields[name]))
            if "audio_path" in fields and fields["audio_path"] != segment.audio_path:
                segment.recording_padding = None
            for name, value in fields.items():
                setattr(segment, name, value)
            if "characters" in fields and "speaker_assignment" not in fields:
                segment.speaker_assignment = (
                    "manual" if any(name.strip() for name in segment.characters) else "excluded"
                )
            if segment.speaker_assignment == "excluded" and any(
                name.strip() for name in segment.characters
            ):
                raise ValueError("Only an unassigned segment can be excluded from speaker matching.")
            if not 0 <= segment.start < segment.end <= project.video_duration + 0.05:
                raise ValueError("Segment must have 0 <= start < end <= source video duration.")
            if segment.audio_mode == "file" and not segment.audio_path:
                raise ValueError("File audio requires audio_path to an already-cut prompt recording.")
            if segment.audio_mode == "video":
                if fields.get("audio_path"):
                    raise ValueError("Set audio_mode=file when supplying an external audio_path.")
                segment.audio_path = ""
                segment.recording_padding = None
            changed_ids.append(segment.id)
        project.sort_segments()
        updated.dirty = True
        result = self._publish(updated, previous)
        result["changed_ids"] = changed_ids
        result["deleted_ids"] = delete_ids
        return result

    def save_project(
        self, expected_revision: str, path: str | None = None, overwrite: bool = False
    ) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        require_revision(snapshot, expected_revision)
        destination = local_path(path, exists=False) if path is not None else snapshot.path
        if destination is None:
            raise ValueError("Supply an absolute .cvpack.json path for the first save.")
        return self.describe(self.access.save(destination, expected_revision, overwrite))

    def validate_project(self) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        revision = snapshot.revision
        project = snapshot.project
        errors = project.validate()
        if project.video_path and Path(project.video_path).is_file():
            info = self.media.probe(Path(project.video_path))
            project.video_duration = info.duration
            errors = project.validate()
            if not info.has_audio:
                errors.append("Source video has no audio stream.")
        return {
            "project_id": snapshot.project_id,
            "valid": not errors,
            "errors": errors,
            "overlaps": [asdict(item) for item in audit_timeline_overlaps(project.segments)],
            "warnings": list(project.import_warnings),
            "revision": revision,
        }

    def export_pack(
        self,
        output_parent: str,
        expected_revision: str,
        overwrite: bool = False,
        progress: Callable[[ExportProgress], None] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        require_revision(snapshot, expected_revision)
        if snapshot.dirty or snapshot.path is None:
            raise ValueError("Save the project before exporting, so the edit decisions are retained.")
        if not snapshot.path.is_file() or sha256(snapshot.path) != snapshot.saved_hash:
            raise ValueError("Saved project changed on disk. Reopen it before exporting.")
        parent = local_path(output_parent, exists=False)
        folder = parent / safe_name(snapshot.project.title)
        archive = folder.with_name(folder.name + ".zip")
        if (folder.exists() or archive.exists()) and not overwrite:
            raise ValueError("Pack or ZIP exists. Choose another output or set overwrite=true.")
        protected = [snapshot.path, ProjectStore.previous_path(snapshot.path)]
        protected.extend(protected_assets(snapshot.project))
        if any(is_same_or_within(path, folder) or path == archive for path in protected):
            raise ValueError("Export would replace the saved project or source assets.")
        result = PackExporter(
            self.media, cache_root=self.data_root.parent / "export-cache",
        ).export(snapshot.project, parent, progress=progress)
        return {
            "project_id": snapshot.project_id,
            "pack_path": str(result.pack_path),
            "zip_path": str(result.zip_path),
            "validation": result.validation,
            "file_hashes": result.file_hashes,
            "warnings": result.warnings,
            "exported_revision": expected_revision,
        }

    def validate_pack(self, folder: str, zip_path: str | None = None) -> dict[str, Any]:
        source = local_path(folder, directory=True)
        validator = PackValidator(self.media)
        report = validator.validate_folder(source)
        if zip_path is not None:
            validator.validate_zip(
                local_path(zip_path), source.name,
                {path.name for path in source.iterdir() if path.is_file()},
            )
            report["zip_valid"] = True
        return report

    def analyze(
        self,
        use_whisper: bool,
        allow_download: bool,
        sensitivity: str,
        model: str,
        language: str,
        progress: Callable[[str, float | None], None],
        cancelled: Callable[[], bool],
        align_captions: bool = False,
    ) -> dict[str, Any]:
        if (use_whisper or align_captions) and not allow_download:
            raise ValueError(
                "Whisper or caption alignment may download or repair pinned components. "
                "Obtain permission, then set "
                "allow_download=true. Activity-only scanning never downloads anything."
            )
        snapshot = self.access.snapshot()
        if align_captions and not snapshot.project.source_captions:
            raise ValueError("Word alignment requires original imported YouTube captions.")
        source = local_path(snapshot.project.video_path)
        info = self.media.probe(source)
        if not info.has_audio:
            raise ValueError("The source video has no audio.")
        result = analyze_video(
            self.media, source, info.duration, self.data_root,
            sensitivity=sensitivity, use_whisper=use_whisper, model_key=model,
            language=language, progress=progress, cancelled=cancelled,
            source_captions=list(snapshot.project.source_captions) if align_captions else None,
            align_captions=align_captions, allow_alignment_download=allow_download,
        )
        return {
            **asdict(result),
            "project_id": snapshot.project_id,
            "revision": snapshot.revision,
            "warning": "Draft evidence only; review captions, timing, and speakers. Nothing was added.",
        }

    def _source_range(self, start: float, end: float) -> Path:
        snapshot = self.access.snapshot()
        if not (
            math.isfinite(start) and math.isfinite(end)
            and 0 <= start < end <= snapshot.project.video_duration
        ):
            raise ValueError("Preview range must be within the source video.")
        if end - start > 30:
            raise ValueError("Preview at most 30 seconds per call.")
        return local_path(snapshot.project.video_path)

    def get_frame(self, timestamp: float) -> bytes:
        snapshot = self.access.snapshot()
        if not math.isfinite(timestamp) or not 0 <= timestamp < snapshot.project.video_duration:
            raise ValueError("Frame timestamp must be within the source video.")
        with tempfile.TemporaryDirectory(prefix="cvpc-mcp-frame-") as temporary:
            frame = Path(temporary) / "frame.png"
            self.media.extract_frame(local_path(snapshot.project.video_path), timestamp, frame)
            # Keep inline MCP responses bounded even for 4K sources.
            scaled = Path(temporary) / "preview.png"
            self.media.run(
                [self.media.ffmpeg, "-v", "error", "-y", "-i", str(frame),
                 "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
                 "-frames:v", "1", str(scaled)],
                "Preparing preview image",
            )
            return scaled.read_bytes()

    def _wav_preview(self, source: Path, start: float, duration: float, output: Path) -> bytes:
        self.media.run(
            [self.media.ffmpeg, "-v", "error", "-y", "-ss", f"{start:.6f}",
             "-i", str(source), "-t", f"{duration:.6f}", "-map", "0:a:0",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)],
            "Preparing audio preview",
        )
        return output.read_bytes()

    def preview_audio(self, start: float, end: float) -> bytes:
        source = self._source_range(start, end)
        with tempfile.TemporaryDirectory(prefix="cvpc-mcp-audio-") as temporary:
            return self._wav_preview(source, start, end - start, Path(temporary) / "preview.wav")

    def preview_segment(self, segment_id: str) -> bytes:
        snapshot = self.access.snapshot()
        project = snapshot.project
        segment = project.segment_by_id(segment_id)
        if segment is None:
            raise ValueError(f"Unknown segment id: {segment_id}")
        if segment.duration + project.head_padding + project.tail_padding > 30:
            raise ValueError("Preview at most 30 seconds; use preview_audio for shorter source ranges.")
        if segment.audio_mode == "file":
            source = local_path(segment.audio_path)
            if self.media.probe_audio_duration(source) > 30:
                raise ValueError("The external prompt exceeds the 30-second preview limit.")
        elif not segment.source_range_known:
            raise ValueError("Set an explicit source cut before regenerating an imported prompt.")
        with tempfile.TemporaryDirectory(prefix="cvpc-mcp-prompt-") as temporary:
            prompt = Path(temporary) / "prompt.mp3"
            PackExporter(self.media)._write_audio(
                project, segment, local_path(project.video_path), prompt, project.video_duration
            )
            return self._wav_preview(prompt, 0, 30, Path(temporary) / "preview.wav")

    # ----- Headless end-to-end pipeline and assistant review helpers -----

    def _changed_during(self, expected_revision: str, what: str) -> ProjectSnapshot:
        latest = self.access.snapshot()
        if latest.revision != expected_revision:
            raise ValueError(
                f"Project changed while {what}. Call get_project, reconcile, and retry."
            )
        return latest

    def import_youtube(
        self,
        url: str,
        destination_folder: str,
        title: str | None,
        authors: list[str] | None,
        caption_language: str,
        if_exists: Literal["error", "reuse", "overwrite"],
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict[str, Any]:
        from choicer_voicer_pack_creator.youtube import (
            YouTubeDownload,
            YouTubeImportConflict,
            download_youtube,
        )

        if title is not None and not title.strip():
            raise ValueError("title cannot be blank; omit it to use the YouTube title.")
        if authors is not None and (not authors or not all(author.strip() for author in authors)):
            raise ValueError("authors must contain at least one non-empty name when supplied.")
        folder = local_path(destination_folder, exists=False)
        folder.mkdir(parents=True, exist_ok=True)
        result = download_youtube(
            self.media, url, folder, caption_language, progress=progress, cancelled=cancelled,
        )
        if isinstance(result, YouTubeImportConflict):
            folders = ", ".join(str(item.folder) for item in result.imports)
            if if_exists == "error":
                raise ValueError(
                    f"This video was already imported here: {folders}. Set if_exists='reuse' to "
                    "reuse the downloaded video (captions are refreshed) or 'overwrite' to download again."
                )
            choice = result.imports[0]
            if if_exists == "reuse":
                choice = next((item for item in result.imports if not item.reuse_problem), choice)
            result = download_youtube(
                self.media, url, folder, caption_language, progress=progress, cancelled=cancelled,
                existing=choice, overwrite=if_exists == "overwrite",
            )
        if not isinstance(result, YouTubeDownload):
            raise ValueError("The YouTube import did not produce a video.")
        project = PackProject(
            title=title.strip() if title else result.title,
            authors=[author.strip() for author in authors] if authors else [getpass.getuser()],
            video_path=str(result.video_path),
            video_duration=result.duration,
            source_url=result.url,
            caption_language=result.language,
            source_captions=list(result.captions),
            import_warnings=list(result.warnings),
        )
        described = self.describe(self.access.create(ProjectSnapshot(project, dirty=True)), 0, 0)
        described["import_notes"] = list(result.warnings)
        return described

    def get_source_captions(
        self, offset: int = 0, limit: int = 200, include_fragments: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        captions = snapshot.project.source_captions
        page = []
        for index, caption in enumerate(captions[offset:offset + limit], start=offset):
            row: dict[str, Any] = {
                "index": index, "start": round(caption.start, 3), "end": round(caption.end, 3),
                "text": caption.text,
            }
            if include_fragments:
                row["fragments"] = [fragment.to_dict() for fragment in caption.fragments]
            page.append(row)
        return {
            "project_id": snapshot.project_id,
            "source_url": snapshot.project.source_url,
            "caption_language": snapshot.project.caption_language,
            "caption_source": captions[0].source if captions else None,
            "total_captions": len(captions),
            "captions": page,
            "next_offset": offset + limit if offset + limit < len(captions) else None,
            "revision": snapshot.revision,
        }

    def draft_segments(
        self,
        expected_revision: str,
        source: Literal["captions", "aligned_captions", "whisper"],
        replace_existing: bool,
        allow_download: bool,
        model: str,
        language: str,
        sensitivity: str,
        skip_nonverbal: bool,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        require_revision(snapshot, expected_revision)
        project = snapshot.project
        if project.segments and not replace_existing:
            raise ValueError(
                "The project already has segments. Set replace_existing=true to replace them."
            )
        if source != "captions" and not allow_download:
            raise ValueError(
                "Whisper and caption alignment may download or repair pinned components. "
                "Obtain permission, then set allow_download=true."
            )
        if source != "whisper" and not project.source_captions:
            raise ValueError("This project has no imported captions. Use source='whisper'.")
        video = local_path(project.video_path)
        info = self.media.probe(video)
        if not info.has_audio:
            raise ValueError("The source video has no audio.")
        result = analyze_video(
            self.media, video, info.duration, self.data_root,
            sensitivity=sensitivity, use_whisper=source == "whisper", model_key=model,
            language=language, progress=progress, cancelled=cancelled,
            source_captions=None if source == "whisper" else list(project.source_captions),
            align_captions=source == "aligned_captions", allow_alignment_download=allow_download,
        )
        rows: list[tuple[float, float, str, str]]
        if source == "whisper":
            rows = [
                (item.start, item.end, item.caption, "")
                for item in result.suggestions if item.caption.strip()
            ]
        else:
            reasons = list(result.caption_timing.review_reasons) if result.caption_timing else []
            rows = [
                (item.start, item.end, item.text, reasons[index] if index < len(reasons) else "")
                for index, item in enumerate(result.refined_captions or [])
            ]
        latest = self._changed_during(expected_revision, "drafting segments")
        updated = latest.copy()
        if replace_existing:
            updated.project.segments = []
        created: list[str] = []
        review: dict[str, str] = {}
        skipped = 0
        for start, end, text, reason in rows:
            end = min(end, updated.project.video_duration)
            caption = " ".join(text.split())
            if end - start < 0.05 or not caption or (skip_nonverbal and is_nonverbal(caption)):
                skipped += 1
                continue
            segment = Segment(start=round(start, 3), end=round(end, 3), caption=caption)
            updated.project.segments.append(segment)
            created.append(segment.id)
            if reason:
                review[segment.id] = reason
        if not created:
            raise ValueError("No usable caption or transcript rows were found.")
        updated.project.sort_segments()
        updated.dirty = True
        described = self._publish(updated, latest)
        described.update(
            created_ids=created, skipped_rows=skipped, timing_review_reasons=review,
            detected_language=result.detected_language,
            next_steps=(
                "Call review_segments for merge candidates and timing flags, name speakers with "
                "edit_segments, then match_speakers. Draft text is evidence, not verified dialogue."
            ),
        )
        return described

    def review_segments(self, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        snapshot = self.access.snapshot()
        return {
            "project_id": snapshot.project_id,
            "revision": snapshot.revision,
            **review_project(snapshot.project, offset, limit),
        }

    def merge_segments(self, groups: list[MergeGroup], expected_revision: str) -> dict[str, Any]:
        previous = self.access.snapshot()
        require_revision(previous, expected_revision)
        if not groups or len(groups) > 200:
            raise ValueError("Provide between 1 and 200 merge groups.")
        seen: set[str] = set()
        for group in groups:
            for segment_id in group.segment_ids:
                if segment_id in seen:
                    raise ValueError(f"Segment {segment_id} appears in more than one merge group.")
                seen.add(segment_id)
        updated = previous.copy()
        project = updated.project
        merged: list[str] = []
        warnings: list[str] = []
        for group in groups:
            combined = project.combine_segments(group.segment_ids)
            fields = group.model_dump(exclude_unset=True)
            if "caption" in fields:
                combined.caption = " ".join(fields["caption"].split())
            if "characters" in fields:
                combined.characters = [name.strip() for name in fields["characters"] if name.strip()]
                combined.speaker_assignment = "manual" if combined.characters else "excluded"
            covered = [
                item.id for item in project.segments
                if item.id != combined.id and item.start < combined.end - 0.05
                and item.end > combined.start + 0.05
            ]
            if covered:
                warnings.append(f"Merged segment {combined.id} overlaps {', '.join(covered)}.")
            merged.append(combined.id)
        updated.dirty = True
        described = self._publish(updated, previous)
        described.update(merged_ids=merged, removed_ids=sorted(seen), warnings=warnings)
        return described

    def split_segment(
        self, segment_id: str, at: float, first_caption: str, second_caption: str,
        expected_revision: str,
    ) -> dict[str, Any]:
        previous = self.access.snapshot()
        require_revision(previous, expected_revision)
        updated = previous.copy()
        segment = updated.project.segment_by_id(segment_id)
        if segment is None:
            raise ValueError(f"Unknown segment id: {segment_id}")
        if segment.audio_mode != "video" or not segment.source_range_known:
            raise ValueError("Only source-video segments with a known source range can be split.")
        if not segment.start + 0.1 <= at <= segment.end - 0.1:
            raise ValueError("The split point must be at least 0.1 seconds inside the segment.")
        second = segment.clone()
        second.start = at
        second.caption = " ".join(second_caption.split())
        segment.end = at
        segment.caption = " ".join(first_caption.split())
        updated.project.add_segment(second)
        updated.dirty = True
        described = self._publish(updated, previous)
        described["changed_ids"] = [segment.id, second.id]
        return described

    def transcribe_segments(
        self,
        segment_ids: list[str] | None,
        allow_download: bool,
        model: str,
        language: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict[str, Any]:
        if not allow_download:
            raise ValueError(
                "Whisper may download or repair pinned components. Obtain permission, then set "
                "allow_download=true."
            )
        snapshot = self.access.snapshot()
        project = snapshot.project
        if segment_ids:
            chosen = []
            for segment_id in dict.fromkeys(segment_ids):
                segment = project.segment_by_id(segment_id)
                if segment is None:
                    raise ValueError(f"Unknown segment id: {segment_id}")
                chosen.append(segment)
        else:
            chosen = list(project.segments)
        if not chosen:
            raise ValueError("There are no segments to transcribe.")
        ranges: list[tuple[Segment, Path, float, float]] = []
        skipped: list[dict[str, str]] = []
        for segment in chosen:
            if segment.audio_mode == "file":
                source = local_path(segment.audio_path)
                ranges.append((segment, source, 0.0, self.media.probe_audio_duration(source)))
            elif segment.source_range_known:
                start = max(0.0, segment.start - project.head_padding)
                end = min(project.video_duration, segment.end + project.tail_padding)
                ranges.append((segment, local_path(project.video_path), start, end - start))
            else:
                skipped.append({"segment_id": segment.id, "reason": "unknown source range"})
        total = sum(item[3] + TRANSCRIBE_GAP_SECONDS for item in ranges)
        if total > TRANSCRIBE_MAX_SECONDS:
            raise ValueError(
                f"Selected audio is {total:.0f}s; transcribe at most {TRANSCRIBE_MAX_SECONDS:.0f}s "
                "per call. Pass fewer segment_ids."
            )
        rate = 16000
        with tempfile.TemporaryDirectory(prefix="cvpc-mcp-transcribe-") as temporary:
            folder = Path(temporary)
            combined = folder / "segments.wav"
            silence = b"\0\0" * int(rate * TRANSCRIBE_GAP_SECONDS)
            spans: list[tuple[float, float]] = []
            with wave.open(str(combined), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(silence)
                cursor = len(silence) // 2
                for index, (_segment, source, start, duration) in enumerate(ranges):
                    if cancelled():
                        raise ValueError("Transcription was canceled.")
                    progress(f"Preparing audio for segment {index + 1} of {len(ranges)}...", None)
                    piece = folder / f"piece-{index}.wav"
                    self._wav_preview(source, start, duration, piece)
                    with wave.open(str(piece), "rb") as reader:
                        frames = reader.readframes(reader.getnframes())
                    piece.unlink()
                    output.writeframes(frames)
                    spans.append((cursor / rate, (cursor + len(frames) // 2) / rate))
                    output.writeframes(silence)
                    cursor += len(frames) // 2 + len(silence) // 2
            manager = WhisperManager(self.data_root)
            manager.transcribe(
                combined, folder, model, language, detect_hardware(), progress, cancelled,
            )
            heard = heard_text(folder / f"{WHISPER_TRANSCRIPT_NAME}.json", spans)
        results = []
        for (segment, *_), text in zip(ranges, heard, strict=True):
            comparison = compare_transcript(segment.caption, text)
            results.append({
                "segment_id": segment.id, "start": round(segment.start, 3),
                "end": round(segment.end, 3), "caption": segment.caption, "heard": text,
                **comparison,
            })
        return {
            "project_id": snapshot.project_id,
            "revision": snapshot.revision,
            "model": model,
            "segments": results,
            "skipped": skipped,
            "flagged": sum(bool(item["flags"]) for item in results),
            "note": (
                "Heard text is local Whisper recognition of each exported prompt (with padding). "
                "Low similarity or clipped flags are evidence for review, not the correct line."
            ),
        }

    def match_speakers(
        self,
        expected_revision: str,
        allow_download: bool,
        apply: bool,
        include_all: bool,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict[str, Any]:
        from choicer_voicer_pack_creator.speaker_matching import (
            MIN_ACTIVE_SECONDS,
            MIN_COSINE,
            MIN_RUNNER_UP_MARGIN,
            SpeakerClip,
            SpeakerDownloadRequired,
            SpeakerMatchingManager,
        )

        snapshot = self.access.snapshot()
        require_revision(snapshot, expected_revision)
        project = snapshot.project
        clips: list[SpeakerClip] = []
        skipped: dict[str, int] = {}
        for segment in project.segments:
            names = tuple(name.strip() for name in segment.characters if name.strip())
            references = () if segment.speaker_assignment == "automatic" else names
            reason = ""
            if segment.speaker_assignment == "excluded":
                reason = "excluded"
            elif is_nonverbal(segment.caption):
                reason = "nonverbal"
            elif len(references) > 1:
                reason = "multiple-characters"
            elif segment.audio_mode == "video" and not segment.source_range_known:
                reason = "unknown-range"
            elif segment.audio_mode == "video" and segment.duration < MIN_ACTIVE_SECONDS:
                reason = "short"
            elif segment.audio_mode == "file" and not segment.audio_path:
                reason = "missing-audio"
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            if segment.audio_mode == "video":
                clips.append(SpeakerClip(
                    segment.id, project.video_path, segment.start, segment.end, references,
                ))
            else:
                clips.append(SpeakerClip(segment.id, segment.audio_path, 0.0, None, references))
        if not any(len(clip.characters) == 1 for clip in clips):
            raise ValueError(
                "Name at least one clear dialogue segment (about 2 seconds or more) for each "
                "character with edit_segments first. Matching compares voices with those references."
            )
        try:
            result = SpeakerMatchingManager(self.data_root).evidence(
                self.media, tuple(clips), allow_download=allow_download,
                progress=progress, cancelled=cancelled,
            )
        except SpeakerDownloadRequired as error:
            raise ValueError(f"{error} Obtain permission, then set allow_download=true.") from error
        matches = {match.segment_id: match for match in result.matches}
        rows = []
        for item in result.evidence:
            segment = project.segment_by_id(item.segment_id)
            assert segment is not None
            names = [name.strip() for name in segment.characters if name.strip()]
            current = names[0] if len(names) == 1 else None
            scores = dict(item.scores)
            flag = suggested = None
            if current is None:
                if item.segment_id in matches:
                    flag, suggested = "suggested_match", matches[item.segment_id].character
            elif item.scores:
                best, best_score = item.scores[0]
                own = scores.get(current)
                if best != current and best_score >= MIN_COSINE and (
                    own is None or best_score - own >= MIN_RUNNER_UP_MARGIN
                ):
                    flag, suggested = "possible_misattribution", best
            if flag is None and not include_all:
                continue
            rows.append({
                "segment_id": segment.id, "start": round(segment.start, 3),
                "end": round(segment.end, 3), "caption": segment.caption,
                "characters": names, "speaker_assignment": segment.speaker_assignment,
                "candidates": [
                    {"character": name, "similarity": score} for name, score in item.scores[:3]
                ],
                "flag": flag, "suggested": suggested,
            })
        applied: list[str] = []
        revision = snapshot.revision
        if apply and result.matches:
            latest = self._changed_during(expected_revision, "matching speakers")
            updated = latest.copy()
            for match in result.matches:
                segment = updated.project.segment_by_id(match.segment_id)
                if (
                    segment is not None and segment.speaker_assignment == "manual"
                    and not any(name.strip() for name in segment.characters)
                ):
                    segment.characters = [match.character]
                    segment.speaker_assignment = "automatic"
                    applied.append(segment.id)
            if applied:
                updated.dirty = True
                revision = self._publish(updated, latest)["revision"]
        reasons: dict[str, int] = dict(skipped)
        for _, reason in result.skip_reasons:
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "project_id": snapshot.project_id,
            "revision": revision,
            "examined": result.examined,
            "references": sorted({
                clip.characters[0] for clip in clips if len(clip.characters) == 1
            }),
            "applied_ids": applied,
            "segments": rows,
            "skipped": reasons,
            "note": (
                "Similarities are voice-likeness scores, not probabilities. Review flagged rows; "
                "correct names with edit_segments. Applied names are marked automatic."
            ),
        }

    def generate_backing_track(
        self,
        expected_revision: str,
        mode: BackingMode,
        allow_download: bool,
        allow_runtime_download: bool,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict[str, Any]:
        from choicer_voicer_pack_creator.separation import (
            SeparationDownloadRequired,
            SeparationManager,
        )

        mode = validate_backing_mode(mode)
        snapshot = self.access.snapshot()
        require_revision(snapshot, expected_revision)
        video = local_path(snapshot.project.video_path)
        manager = SeparationManager(self.data_root.parent / "backing", mode=mode)
        try:
            backing = manager.generate(
                self.media, video, allow_download=allow_download,
                allow_runtime_download=allow_runtime_download, offer_runtime_download=False,
                progress=progress, cancelled=cancelled,
            )
        except SeparationDownloadRequired as error:
            raise ValueError(f"{error} Obtain permission, then set allow_download=true.") from error
        latest = self.access.snapshot()
        if latest.revision != expected_revision:
            raise ValueError(
                f"Backing saved at {backing}, but the project changed meanwhile. Set it with "
                "update_project patch.backing_track_path after reconciling."
            )
        updated = latest.copy()
        updated.project.backing_track_path = str(backing)
        updated.project.backing_generation_mode = mode
        updated.dirty = True
        described = self._publish(updated, latest)
        described["backing_track_path"] = str(backing)
        return described


def heard_text(transcript: Path, spans: list[tuple[float, float]]) -> list[str]:
    """Assign Whisper tokens in a concatenated recording back to the source pieces."""
    import bisect
    import re

    value = json.loads(transcript.read_text(encoding="utf-8-sig"))
    pieces: list[list[str]] = [[] for _ in spans]
    starts = [start for start, _ in spans]
    for item in value.get("transcription", []) if isinstance(value, dict) else []:
        if not isinstance(item, dict):
            continue
        fallback = item.get("offsets") or {}
        tokens = [token for token in item.get("tokens", []) if isinstance(token, dict)]
        if not tokens:
            tokens = [{"text": item.get("text", ""), "offsets": fallback}]
        for token in tokens:
            text = str(token.get("text", ""))
            if not text.strip() or text.strip().startswith("[_"):
                continue
            offsets = token.get("offsets") or fallback
            try:
                middle = (float(offsets["from"]) + float(offsets["to"])) / 2000
            except (KeyError, TypeError, ValueError):
                continue
            index = max(0, bisect.bisect_right(starts, middle) - 1)
            if index + 1 < len(spans) and (
                middle - spans[index][1] > spans[index + 1][0] - middle
            ):
                index += 1
            if spans:
                pieces[index].append(text)
    return [
        " ".join(re.sub(r"\[[^\]]*\]|\([A-Z ]+\)", " ", "".join(piece)).split())
        for piece in pieces
    ]