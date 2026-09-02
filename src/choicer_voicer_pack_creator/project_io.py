from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.models import PackProject

_PATH_FIELDS = ("video_path", "backing_track_path", "icon_path", "source_pack_path")
_SEGMENT_PATH_FIELDS = ("audio_path", "image_path")
_RECOVERY_SCHEMA_VERSION = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_bytes(destination: Path, payload: bytes) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write_bytes_atomic(destination: Path, payload: bytes) -> None:
    temporary = _stage_bytes(destination, payload)
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _portable_path(value: str, base: Path) -> str:
    if not value:
        return ""
    path = Path(value).resolve()
    try:
        return path.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolved_path(value: str, base: Path) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return str(path.resolve())
    return str((base / path).resolve())


class ProjectStore:
    @staticmethod
    def previous_path(path: Path) -> Path:
        destination = path.resolve()
        return destination.with_name(destination.name + ".previous")

    @staticmethod
    def _forensic_path(path: Path, label: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return path.with_name(f"{path.name}.{label}-{stamp}-{uuid.uuid4().hex[:8]}")

    @staticmethod
    def save(project: PackProject, path: Path) -> None:
        destination = path.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = project.to_dict()
        for field in _PATH_FIELDS:
            data[field] = _portable_path(str(data.get(field, "")), destination.parent)
        for segment in data["segments"]:
            for field in _SEGMENT_PATH_FIELDS:
                segment[field] = _portable_path(str(segment.get(field, "")), destination.parent)
        payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        previous = ProjectStore.previous_path(destination)
        prior_previous = previous.read_bytes() if previous.is_file() else None
        destination_payload = destination.read_bytes() if destination.is_file() else None
        destination_valid = False
        if destination_payload is not None:
            try:
                ProjectStore.load(destination)
                destination_valid = True
            except Exception:
                destination_valid = False
        temporary = _stage_bytes(destination, payload)
        try:
            if destination_payload is not None and destination_valid:
                _write_bytes_atomic(previous, destination_payload)
            elif destination_payload is not None:
                _write_bytes_atomic(
                    ProjectStore._forensic_path(destination, "corrupt"),
                    destination_payload,
                )
            try:
                os.replace(temporary, destination)
            except OSError:
                if destination_valid:
                    if prior_previous is None:
                        previous.unlink(missing_ok=True)
                    else:
                        _write_bytes_atomic(previous, prior_previous)
                raise
            if destination_payload is None and previous.is_file():
                orphaned = ProjectStore._forensic_path(destination, "orphaned-previous")
                with suppress(OSError):
                    os.replace(previous, orphaned)
                    # The newly saved main project is valid. Preserve an undeletable sidecar
                    # rather than reporting the completed save as failed.
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def load(path: Path) -> PackProject:
        source = path.resolve()
        value: Any = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Project file must contain a JSON object")
        for field in _PATH_FIELDS:
            value[field] = _resolved_path(str(value.get(field, "")), source.parent)
        segments = value.get("segments", [])
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                for field in _SEGMENT_PATH_FIELDS:
                    segment[field] = _resolved_path(str(segment.get(field, "")), source.parent)
        return PackProject.from_dict(value)


@dataclass(slots=True)
class RecoveryRecord:
    project: PackProject
    project_path: Path | None
    created_at_utc: str
    saved_project_sha256: str | None
    source_path: Path


class RecoveryStore:
    """Atomic per-user recovery snapshots that never replace a project file."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".previous")

    def save(self, project: PackProject, project_path: Path | None) -> None:
        data = project.to_dict()
        base = project_path.resolve().parent if project_path else Path.cwd()
        for field in _PATH_FIELDS:
            value = str(data.get(field, ""))
            data[field] = _resolved_path(value, base) if value else ""
        for segment in data["segments"]:
            for field in _SEGMENT_PATH_FIELDS:
                value = str(segment.get(field, ""))
                segment[field] = _resolved_path(value, base) if value else ""
        saved_project_sha256: str | None = None
        if project_path is not None:
            saved_project_sha256 = (
                _sha256(project_path.resolve()) if project_path.resolve().is_file() else ""
            )
        payload = (
            json.dumps(
                {
                    "recovery_schema_version": _RECOVERY_SCHEMA_VERSION,
                    "created_at_utc": datetime.now(UTC).isoformat(),
                    "project_path": str(project_path.resolve()) if project_path else "",
                    "saved_project_sha256": saved_project_sha256,
                    "project": data,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        prior_previous = self.previous_path.read_bytes() if self.previous_path.is_file() else None
        temporary = _stage_bytes(self.path, payload)
        try:
            if self.path.is_file():
                try:
                    self._load_candidate(self.path)
                except Exception:
                    pass
                else:
                    _write_bytes_atomic(self.previous_path, self.path.read_bytes())
            try:
                os.replace(temporary, self.path)
            except OSError:
                if prior_previous is None:
                    self.previous_path.unlink(missing_ok=True)
                else:
                    _write_bytes_atomic(self.previous_path, prior_previous)
                raise
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _load_candidate(candidate: Path) -> RecoveryRecord:
        value: Any = json.loads(candidate.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("snapshot must contain a JSON object")
        if int(value.get("recovery_schema_version", 0)) != _RECOVERY_SCHEMA_VERSION:
            raise ValueError("unsupported recovery schema")
        project_value = value.get("project")
        if not isinstance(project_value, dict):
            raise ValueError("snapshot project must be a JSON object")
        project_path_value = str(value.get("project_path", ""))
        project_path = Path(project_path_value).resolve() if project_path_value else None
        saved_project_sha256 = value.get("saved_project_sha256")
        if saved_project_sha256 is not None and not isinstance(saved_project_sha256, str):
            raise ValueError("snapshot saved-project hash must be a string or null")
        return RecoveryRecord(
            project=PackProject.from_dict(project_value),
            project_path=project_path,
            created_at_utc=str(value.get("created_at_utc", "")),
            saved_project_sha256=saved_project_sha256,
            source_path=candidate,
        )

    def load(self) -> RecoveryRecord | None:
        errors: list[str] = []
        for candidate in (self.path, self.previous_path):
            if not candidate.is_file():
                continue
            try:
                return self._load_candidate(candidate)
            except Exception as error:
                errors.append(f"{candidate}: {error}")
        if errors:
            raise ValueError("No valid recovery snapshot could be read:\n" + "\n".join(errors))
        return None

    def clear(self) -> None:
        for path in (
            self.path,
            self.previous_path,
            self.path.with_name(self.path.name + ".partial"),
            self.previous_path.with_name(self.previous_path.name + ".partial"),
        ):
            path.unlink(missing_ok=True)

    def is_redundant(self, record: RecoveryRecord) -> bool:
        if record.project_path is None or not record.project_path.is_file():
            return False
        try:
            return ProjectStore.load(record.project_path).to_dict() == record.project.to_dict()
        except Exception:
            return False

    def saved_project_changed(self, record: RecoveryRecord) -> bool:
        if record.project_path is None or record.saved_project_sha256 is None:
            return False
        try:
            current_hash = _sha256(record.project_path) if record.project_path.is_file() else ""
        except OSError:
            return True
        return current_hash != record.saved_project_sha256
