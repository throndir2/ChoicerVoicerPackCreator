from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from choicer_voicer_pack_creator.analysis import analyze_video
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
from choicer_voicer_pack_creator.timeline_audit import audit_timeline_overlaps
from choicer_voicer_pack_creator.validation import PackValidator

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


class SegmentPatch(InputModel):
    id: str | None = None
    start: Seconds | None = None
    end: Seconds | None = None
    caption: str | None = None
    characters: list[str] | None = None
    audio_mode: Literal["video", "file"] | None = None
    audio_path: str | None = None
    image_path: str | None = None


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
            for name, value in fields.items():
                setattr(segment, name, value)
            if not 0 <= segment.start < segment.end <= project.video_duration + 0.05:
                raise ValueError("Segment must have 0 <= start < end <= source video duration.")
            if segment.audio_mode == "file" and not segment.audio_path:
                raise ValueError("File audio requires audio_path to an already-cut prompt recording.")
            if segment.audio_mode == "video":
                if fields.get("audio_path"):
                    raise ValueError("Set audio_mode=file when supplying an external audio_path.")
                segment.audio_path = ""
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
        result = PackExporter(self.media).export(snapshot.project, parent, progress=progress)
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
    ) -> dict[str, Any]:
        if use_whisper and not allow_download:
            raise ValueError(
                "Whisper may download or repair pinned components. Obtain permission, then set "
                "allow_download=true. Activity-only scanning never downloads anything."
            )
        snapshot = self.access.snapshot()
        source = local_path(snapshot.project.video_path)
        info = self.media.probe(source)
        if not info.has_audio:
            raise ValueError("The source video has no audio.")
        result = analyze_video(
            self.media, source, info.duration, self.data_root,
            sensitivity=sensitivity, use_whisper=use_whisper, model_key=model,
            language=language, progress=progress, cancelled=cancelled,
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
