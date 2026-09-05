from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import choicer_voicer_pack_creator.project_io as project_io
from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore


def test_project_store_uses_relative_paths_when_possible(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    video = media / "source.mp4"
    audio = media / "line.mp3"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    project = PackProject(
        title="Portable",
        authors=["Creator"],
        video_path=str(video),
        video_duration=5,
        segments=[
            Segment(1, 2, "Hello", ["Hero"], audio_mode="file", audio_path=str(audio))
        ],
    )
    path = tmp_path / "portable.cvpack.json"
    ProjectStore.save(project, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["video_path"] == "media/source.mp4"
    assert raw["segments"][0]["audio_path"] == "media/line.mp3"

    loaded = ProjectStore.load(path)
    assert loaded.video_path == str(video.resolve())
    assert loaded.segments[0].audio_path == str(audio.resolve())
    assert loaded.segments[0].caption == "Hello"
    assert not path.with_name(path.name + ".partial").exists()


def test_youtube_caption_provenance_survives_save_and_recovery(tmp_path: Path) -> None:
    project = PackProject(
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        caption_language="en",
        source_captions=[SourceCaption(1, 2, "Original caption", "YouTube creator (en)")],
    )
    path = tmp_path / "captions.cvpack.json"
    ProjectStore.save(project, path)
    loaded = ProjectStore.load(path)
    assert loaded.source_url == project.source_url
    assert loaded.caption_language == "en"
    assert loaded.source_captions == project.source_captions
    recovery = RecoveryStore(tmp_path / "recovery.json")
    recovery.save(project, path)
    assert recovery.load().project.source_captions == project.source_captions


def test_save_retains_previous_version_and_save_as_preserves_original(tmp_path: Path) -> None:
    project = PackProject(
        title="Recoverable",
        authors=["Creator"],
        segments=[Segment(1, 2, "First", ["Hero"])],
    )
    original = tmp_path / "original.cvpack.json"
    ProjectStore.save(project, original)
    project.segments[0].caption = "Second"
    ProjectStore.save(project, original)

    assert ProjectStore.load(original).segments[0].caption == "Second"
    assert ProjectStore.load(ProjectStore.previous_path(original)).segments[0].caption == "First"

    project.segments[0].caption = "Third"
    copied = tmp_path / "copy.cvpack.json"
    ProjectStore.save(project, copied)
    assert ProjectStore.load(original).segments[0].caption == "Second"
    assert ProjectStore.load(copied).segments[0].caption == "Third"


def test_recovery_store_round_trips_and_falls_back_to_previous(tmp_path: Path) -> None:
    recovery = RecoveryStore(tmp_path / "recovery.json")
    project = PackProject(
        title="Unsaved",
        authors=["Creator"],
        segments=[Segment(1, 2, "First", ["Hero"])],
    )
    recovery.save(project, None)
    project.segments[0].caption = "Second"
    recovery.save(project, None)

    current = recovery.load()
    assert current is not None
    assert current.project.segments[0].caption == "Second"

    recovery.path.write_text(
        json.dumps(
            {
                "recovery_schema_version": 2,
                "created_at_utc": "2026-09-02T00:00:00+00:00",
                "project_path": "",
                "saved_project_sha256": None,
                "project": {"schema_version": 1, "segments": [None]},
            }
        ),
        encoding="utf-8",
    )
    fallback = recovery.load()
    assert fallback is not None
    assert fallback.project.segments[0].caption == "First"
    assert fallback.source_path == recovery.previous_path

    recovery.save(project, None)
    retained = RecoveryStore(recovery.previous_path).load()
    assert retained is not None
    assert retained.project.segments[0].caption == "First"

    recovery.clear()
    assert recovery.load() is None


def test_recovery_detects_saved_project_changed_after_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "saved.cvpack.json"
    project = PackProject(
        title="Saved",
        authors=["Creator"],
        segments=[Segment(1, 2, "First", ["Hero"])],
    )
    ProjectStore.save(project, path)
    recovery = RecoveryStore(tmp_path / "recovery.json")
    project.segments[0].caption = "Unsaved snapshot"
    recovery.save(project, path)
    record = recovery.load()
    assert record is not None
    assert not recovery.saved_project_changed(record)

    saved_copy = ProjectStore.load(path)
    saved_copy.segments[0].caption = "Newer saved content"
    ProjectStore.save(saved_copy, path)
    assert recovery.saved_project_changed(record)


def test_failed_project_save_preserves_main_and_older_backup(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "saved.cvpack.json"
    project = PackProject(title="Saved", authors=["Creator"])
    project.readme = "version one"
    ProjectStore.save(project, path)
    project.readme = "version two"
    ProjectStore.save(project, path)
    previous = ProjectStore.previous_path(path)
    real_replace = os.replace

    def fail_main_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination).resolve() == path.resolve():
            raise OSError("injected main replacement failure")
        real_replace(source, destination)

    project.readme = "version three"
    monkeypatch.setattr(project_io.os, "replace", fail_main_replace)
    with pytest.raises(OSError, match="injected main"):
        ProjectStore.save(project, path)

    assert ProjectStore.load(path).readme == "version two"
    assert ProjectStore.load(previous).readme == "version one"
    assert not path.with_name(path.name + ".partial").exists()


def test_first_save_quarantines_orphaned_previous_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "saved.cvpack.json"
    previous = ProjectStore.previous_path(path)
    orphan = PackProject(title="Unrelated old project", authors=["Old"])
    ProjectStore.save(orphan, previous)

    current = PackProject(title="New project", authors=["Creator"])
    ProjectStore.save(current, path)

    assert ProjectStore.load(path).title == "New project"
    assert not previous.exists()
    quarantined = list(tmp_path.glob("saved.cvpack.json.orphaned-previous-*"))
    assert len(quarantined) == 1
    assert ProjectStore.load(quarantined[0]).title == "Unrelated old project"


def test_saving_over_corrupt_current_keeps_valid_previous_and_forensic_copy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saved.cvpack.json"
    project = PackProject(title="Version one", authors=["Creator"])
    ProjectStore.save(project, path)
    project.title = "Version two"
    ProjectStore.save(project, path)
    previous = ProjectStore.previous_path(path)
    path.write_bytes(b"corrupt current bytes")

    project.title = "Version three"
    ProjectStore.save(project, path)

    assert ProjectStore.load(path).title == "Version three"
    assert ProjectStore.load(previous).title == "Version one"
    forensic = list(tmp_path.glob("saved.cvpack.json.corrupt-*"))
    assert len(forensic) == 1
    assert forensic[0].read_bytes() == b"corrupt current bytes"
