from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.models import PackProject

_PATH_FIELDS = ("video_path", "backing_track_path", "icon_path", "source_pack_path")
_SEGMENT_PATH_FIELDS = ("audio_path", "image_path")


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
    def save(project: PackProject, path: Path) -> None:
        destination = path.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = project.to_dict()
        for field in _PATH_FIELDS:
            data[field] = _portable_path(str(data.get(field, "")), destination.parent)
        for segment in data["segments"]:
            for field in _SEGMENT_PATH_FIELDS:
                segment[field] = _portable_path(str(segment.get(field, "")), destination.parent)
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        temporary = destination.with_name(destination.name + ".partial")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, destination)

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
