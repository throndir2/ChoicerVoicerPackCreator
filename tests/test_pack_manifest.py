from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.pack_manifest import (
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    read_manifest,
    render_manifest,
    sha256,
)
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.validation import PackValidationError, PackValidator


class Media:
    def probe(self, _path):
        return SimpleNamespace(
            duration=20, height=480, fps=30, video_codec="theora", audio_codec="vorbis",
            pixel_format="yuv420p", audio_sample_rate=48000, audio_channels=2,
        )

    def probe_audio_duration(self, _path):
        return 2.5

    def probe_audio(self, _path):
        return SimpleNamespace(duration=2.5, codec="mp3", sample_rate=48000, channels=1)


@pytest.fixture
def manifest_pack(tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "_pack_info.ini").write_bytes(render_pack_info("Manifest", "icon.png", ["Tester"], ""))
    for name in ("dub_video.ogv", "_backing_track.mp3", "icon.png", "001_Hero.mp3", "001_Hero.png"):
        (root / name).write_bytes(name.encode())
    (root / "001_Hero.txt").write_bytes(render_clip_metadata("Hello", "001_Hero.png", 4, ["Hero"]))
    segment = Segment(4.1, 6.35, "Hello", ["Hero"])
    project = PackProject(
        video_path="C:\\Private\\source.mp4", segments=[segment], video_duration=20,
        head_padding=0.1, tail_padding=0.15,
        source_url="https://youtu.be/abcdefghijk?si=private-tracking&t=42",
        caption_language="en",
    )
    hashes = {path.name: sha256(path) for path in root.iterdir()}
    (root / MANIFEST_NAME).write_bytes(
        render_manifest(project, [(segment, "001_Hero")], root, 20, hashes),
    )
    return root, project


def test_manifest_contains_public_provenance_not_private_project_data(manifest_pack):
    root, project = manifest_pack
    payload = (root / MANIFEST_NAME).read_text(encoding="utf-8")
    data = json.loads(payload)
    assert data["exporter"]["version"] == __version__
    assert data["schema_version"] == 1
    assert data["pack_id"] == project.pack_id
    assert data["source_youtube_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert data["exported_at_utc"].endswith("Z")
    assert MANIFEST_NAME not in data["files"]
    assert all(secret not in payload for secret in ("Private", "private-tracking", "t=42", "video_path"))
    assert not {"title", "authors", "readme"} & data.keys()
    assert not {"caption", "characters"} & data["segments"][0].keys()
    assert read_manifest(root).segments[0].source_range == (4.1, 6.35, 0.1, 0.15)


def test_manifest_restores_precise_ranges_ids_url_and_preserves_prompt(manifest_pack, tmp_path):
    root, original = manifest_pack
    result = PackImporter(Media()).import_folder(root)
    assert not result.warnings
    project = result.project
    assert project.pack_id == original.pack_id
    assert project.source_url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert project.caption_language == "en"
    assert (project.head_padding, project.tail_padding) == (0.1, 0.15)
    segment = project.segments[0]
    assert segment.id == original.segments[0].id
    assert segment.audio_mode == "file" and segment.source_range_known
    assert (segment.start, segment.end) == (4.1, 6.35)
    assert segment.recording_padding == (0.1, 0.15)
    assert segment.clone().recording_padding == segment.recording_padding
    assert segment.clone().id != segment.id
    saved = tmp_path / "saved.cvpack.json"
    ProjectStore.save(project, saved)
    restored = ProjectStore.load(saved)
    assert restored.to_dict() == project.to_dict()
    output = tmp_path / "preserved.mp3"
    trigger = PackExporter(Media())._write_audio(restored, restored.segments[0], root / "dub_video.ogv", output, 20)
    assert trigger == pytest.approx(4)
    assert output.read_bytes() == (root / "001_Hero.mp3").read_bytes()


def test_manifest_app_version_is_provenance_not_a_schema_selector(manifest_pack):
    root, _project = manifest_pack
    data = json.loads((root / MANIFEST_NAME).read_bytes())
    data["exporter"]["version"] = "99.0.0"
    (root / MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")
    assert read_manifest(root).head_padding == 0.1


@pytest.mark.parametrize("change", [
    "schema", "format", "unknown", "unknown-exporter", "unknown-range", "duplicate-id",
    "unsafe-path", "nan", "bool", "negative", "huge-number", "wrong-padding", "wrong-trigger",
    "missing-hash", "wrong-hash", "changed-audio", "changed-caption", "extra-file",
    "wrong-duration", "non-youtube", "tracking-url", "bad-uuid", "bad-time",
    "malformed", "oversized", "duplicate-key",
])
def test_bad_manifest_warns_and_uses_game_metadata_without_applying_any_fields(
    manifest_pack, change,
):
    root, original = manifest_pack
    path = root / MANIFEST_NAME
    data = json.loads(path.read_bytes())
    clip = data["segments"][0]
    if change == "schema":
        data["schema_version"] = 99
    elif change == "format":
        data["format"] = "unrelated"
    elif change == "unknown":
        data["future_field"] = True
    elif change == "unknown-exporter":
        data["exporter"]["future_field"] = True
    elif change == "unknown-range":
        clip["source_range"]["future_field"] = True
    elif change == "duplicate-id":
        data["segments"].append(clip)
    elif change == "unsafe-path":
        clip["metadata"] = "../private.txt"
    elif change in {"nan", "bool", "negative"}:
        clip["source_range"]["start"] = {"nan": float("nan"), "bool": True, "negative": -1}[change]
    elif change == "huge-number":
        clip["source_range"]["start"] = 10**400
    elif change == "wrong-padding":
        clip["source_range"]["head_padding"] = 0.3
    elif change == "wrong-trigger":
        clip["trigger_timestamp"] = 4.01
    elif change == "missing-hash":
        data["files"].pop("icon.png")
    elif change == "wrong-hash":
        data["files"]["icon.png"] = "0" * 64
    elif change == "changed-audio":
        (root / "001_Hero.mp3").write_bytes(b"modified audio")
    elif change == "changed-caption":
        (root / "001_Hero.txt").write_bytes(render_clip_metadata("Edited", "001_Hero.png", 4, ["Hero"]))
    elif change == "extra-file":
        (root / "custom.json").write_bytes(b"{}")
    elif change == "wrong-duration":
        clip["source_range"]["end"] = 7
    elif change == "non-youtube":
        data["source_youtube_url"] = "file:///private"
    elif change == "tracking-url":
        data["source_youtube_url"] += "&si=tracking"
    elif change == "bad-uuid":
        data["pack_id"] = "not-a-uuid"
    elif change == "bad-time":
        data["exported_at_utc"] = "2026-01-01T00:00:00"
    payload = json.dumps(data)
    if change == "malformed":
        payload = "{"
    elif change == "oversized":
        payload = " " * (MAX_MANIFEST_BYTES + 1)
    elif change == "duplicate-key":
        payload = payload.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()
    imported = PackImporter(Media()).import_folder(root)
    assert any("was not applied" in warning for warning in imported.warnings)
    assert imported.project.pack_id != original.pack_id
    assert not imported.project.source_url
    segment = imported.project.segments[0]
    assert (segment.start, segment.end) == (4, 6.5)
    assert not segment.source_range_known and segment.recording_padding is None
    assert segment.id != original.segments[0].id
    assert path.read_bytes() == before
    if change != "wrong-duration":
        with pytest.raises(PackValidationError, match="Invalid _cvpc_metadata.json"):
            PackValidator(Media()).validate_folder(root)


def test_ordinary_packs_and_unknown_recordings_remain_supported(manifest_pack):
    root, _project = manifest_pack
    (root / MANIFEST_NAME).unlink()
    imported = PackImporter(Media()).import_folder(root)
    assert not imported.warnings
    segment = imported.project.segments[0]
    assert not segment.source_range_known
    hashes = {path.name: sha256(path) for path in root.iterdir()}
    data = json.loads(render_manifest(imported.project, [(segment, "001_Hero")], root, 20, hashes))
    assert data["segments"][0]["source_range"] is None
    assert "source_youtube_url" not in data


def test_manifest_read_observes_cancellation(manifest_pack):
    root, _project = manifest_pack
    stopped = False
    with operation_scope(cancelled=lambda: stopped), pytest.raises(OperationCancelled):
        stopped = True
        read_manifest(root)


def test_preserved_cut_changes_cannot_silently_retime_the_recording(manifest_pack, tmp_path):
    root, _project = manifest_pack
    project = PackImporter(Media()).import_folder(root).project
    project.segments[0].end += 1
    with pytest.raises(ValueError, match="no longer matches"):
        PackExporter(Media())._write_audio(
            project, project.segments[0], root / "dub_video.ogv", tmp_path / "prompt.mp3", 20,
        )


@pytest.mark.parametrize("head", [0.1, 1 / 30])
def test_recording_padding_survives_saved_and_remapped_boundary_precision(
    manifest_pack, tmp_path, head,
):
    root, _project = manifest_pack
    project = PackImporter(Media()).import_folder(root).project
    segment = project.segments[0]
    segment.start = head - 3e-16
    segment.end = segment.start + 2.5 - head - 0.15
    segment.recording_padding = (head, 0.15)
    project = PackProject.from_dict(project.to_dict())
    assert not project.validate()
    assert PackExporter(Media())._write_audio(
        project, project.segments[0], root / "dub_video.ogv", tmp_path / "prompt.mp3", 20,
    ) == 0
