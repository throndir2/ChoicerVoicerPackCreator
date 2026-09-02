from __future__ import annotations

import json
from pathlib import Path

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore


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
