"""Optional, versioned export provenance; never a replacement for game metadata."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.models import TIMING_EPSILON, PackProject, Segment
from choicer_voicer_pack_creator.operations import check_cancelled
from choicer_voicer_pack_creator.youtube_url import canonical_youtube_url

MANIFEST_NAME = "_cvpc_metadata.json"
MANIFEST_FORMAT = "choicer-voicer-pack-creator"
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_BASE_FILES = {"_pack_info.ini", "icon.png", "dub_video.ogv", "_backing_track.mp3"}


def sha256(path: Path) -> str:
    check_cancelled()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(chunk)
    check_cancelled()
    return digest.hexdigest()


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError("Manifest has missing or invalid fields")
    unknown = value.keys() - required - (optional or set())
    if unknown:
        raise ValueError(f"Unsupported manifest fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"Invalid manifest {label}")
    return value


def _uuid(value: Any) -> str:
    return uuid.UUID(_text(value, "UUID")).hex


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Manifest timing must be finite, non-negative seconds")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError("Manifest timing exceeds the supported range") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError("Manifest timing must be finite, non-negative seconds")
    return number


def _filename(value: Any) -> str:
    name = _text(value, "filename")
    if (
        name in {".", ".."} or name[-1] in ". "
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    ):
        raise ValueError("Manifest references must be plain pack filenames")
    return name


@dataclass(frozen=True, slots=True)
class ManifestSegment:
    id: str
    metadata: str
    audio: str
    image: str
    trigger_timestamp: float
    source_range: tuple[float, float, float, float] | None

    def validate_timing(self, video_duration: float, audio_duration: float) -> None:
        if self.source_range is None:
            return
        start, end, head, tail = self.source_range
        if (
            end + tail > video_duration + 0.05
            or abs(audio_duration - (end - start + head + tail)) > 0.05
        ):
            raise ValueError(f"Manifest source range does not match {self.audio}'s duration")


@dataclass(frozen=True, slots=True)
class PackManifest:
    pack_id: str
    parent_pack_id: str | None
    source_youtube_url: str
    caption_language: str
    head_padding: float
    tail_padding: float
    segments: tuple[ManifestSegment, ...]


def render_manifest(
    project: PackProject, clips: list[tuple[Segment, str]], root: Path,
    source_duration: float, file_hashes: dict[str, str],
) -> bytes:
    entries = []
    for segment, base in clips:
        check_cancelled()
        padding = (
            (min(segment.start, project.head_padding),
             min(project.tail_padding, max(0.0, source_duration - segment.end)))
            if segment.audio_mode == "video" else segment.recording_padding
        )
        source_range = None
        if segment.source_range_known and padding is not None:
            source_range = {
                "start": segment.start, "end": segment.end,
                "head_padding": padding[0], "tail_padding": padding[1],
            }
        entries.append({
            "id": segment.id, "metadata": f"{base}.txt",
            "audio": f"{base}.mp3", "image": f"{base}.png",
            "trigger_timestamp": read_config(root / f"{base}.txt")["data"]["dub_timestamps"][0],
            "source_range": source_range,
        })
    data = {
        "format": MANIFEST_FORMAT, "schema_version": MANIFEST_SCHEMA_VERSION,
        "exporter": {"name": "Choicer Voicer Pack Creator", "version": __version__},
        "pack_id": project.pack_id, "parent_pack_id": project.parent_pack_id,
        "export_id": uuid.uuid4().hex,
        "exported_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "export_settings": {
            "head_padding": project.head_padding, "tail_padding": project.tail_padding,
        },
        "segments": entries, "files": file_hashes,
    }
    if project.source_url:
        data["source_youtube_url"] = canonical_youtube_url(project.source_url)
    if project.caption_language:
        data["caption_language"] = project.caption_language
    payload = (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("Pack manifest exceeds the 4 MiB size limit")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest field: {key}")
        result[key] = value
    return result


def read_manifest(root: Path) -> PackManifest:
    check_cancelled()
    path = root / MANIFEST_NAME
    if path.is_symlink():
        raise ValueError("Manifest must not be a symbolic link")
    with path.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("Pack manifest exceeds the 4 MiB size limit")
    data = _object(json.loads(payload, object_pairs_hook=_unique_object), {
        "format", "schema_version", "exporter", "pack_id", "parent_pack_id",
        "export_id", "exported_at_utc", "export_settings", "segments", "files",
    }, {"source_youtube_url", "caption_language"})
    if (
        data["format"] != MANIFEST_FORMAT or type(data["schema_version"]) is not int
        or data["schema_version"] != MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("Unsupported manifest format or schema version")
    exporter = _object(data["exporter"], {"name", "version"})
    _text(exporter["name"], "exporter name")
    _text(exporter["version"], "app version")
    pack_id = _uuid(data["pack_id"])
    parent_id = _uuid(data["parent_pack_id"]) if data["parent_pack_id"] is not None else None
    _uuid(data["export_id"])
    timestamp = datetime.fromisoformat(_text(data["exported_at_utc"], "export time"))
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("Manifest export time must be UTC")
    settings = _object(data["export_settings"], {"head_padding", "tail_padding"})
    head, tail = _number(settings["head_padding"]), _number(settings["tail_padding"])
    if max(head, tail) > 2:
        raise ValueError("Manifest export padding exceeds 2 seconds")
    url = ""
    if "source_youtube_url" in data:
        url = canonical_youtube_url(_text(data["source_youtube_url"], "YouTube URL"))
        if url != data["source_youtube_url"]:
            raise ValueError("Manifest YouTube URL must be canonical, without tracking parameters")
    language = _text(data["caption_language"], "caption language") if "caption_language" in data else ""
    if not isinstance(data["segments"], list) or not data["segments"]:
        raise ValueError("Manifest segments must be a nonempty array")
    expected_files = set(_BASE_FILES)
    segments = []
    identifiers: set[str] = set()
    for raw in data["segments"]:
        check_cancelled()
        entry = _object(raw, {"id", "metadata", "audio", "image", "trigger_timestamp", "source_range"})
        identifier = _text(entry["id"], "segment ID")
        metadata, audio, image = (_filename(entry[key]) for key in ("metadata", "audio", "image"))
        if (
            not metadata.endswith(".txt")
            or audio != metadata[:-4] + ".mp3" or image != metadata[:-4] + ".png"
            or metadata in expected_files or identifier in identifiers
        ):
            raise ValueError("Manifest has duplicate IDs or noncanonical clip mappings")
        identifiers.add(identifier)
        expected_files.update((metadata, audio, image))
        trigger = _number(entry["trigger_timestamp"])
        source_range = None
        if entry["source_range"] is not None:
            cut = _object(entry["source_range"], {"start", "end", "head_padding", "tail_padding"})
            start, end, cut_head, cut_tail = (
                _number(cut[key]) for key in ("start", "end", "head_padding", "tail_padding")
            )
            if (
                start >= end or start + TIMING_EPSILON < cut_head or max(cut_head, cut_tail) > 2
                or abs(start - cut_head - trigger) > 0.000501
            ):
                raise ValueError(f"Manifest source range is inconsistent with {metadata}")
            source_range = (start, end, cut_head, cut_tail)
        segments.append(ManifestSegment(identifier, metadata, audio, image, trigger, source_range))
    hashes = data["files"]
    if not isinstance(hashes, dict) or set(hashes) != expected_files:
        raise ValueError("Manifest hash inventory does not match its clip mappings")
    actual = {entry.name for entry in root.iterdir()}
    if actual != expected_files | {MANIFEST_NAME}:
        raise ValueError("Manifest inventory does not match the pack")
    for name, digest in hashes.items():
        check_cancelled()
        asset = root / name
        if (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or asset.is_symlink() or not asset.is_file() or sha256(asset) != digest
        ):
            raise ValueError(f"Manifest checksum does not match {name}")
    for segment in segments:
        clip = read_config(root / segment.metadata).get("data", {})
        if (
            clip.get("image") != segment.image
            or clip.get("dub_timestamps") != [segment.trigger_timestamp]
        ):
            raise ValueError(f"Manifest disagrees with game metadata in {segment.metadata}")
    return PackManifest(pack_id, parent_id, url, language, head, tail, tuple(segments))
