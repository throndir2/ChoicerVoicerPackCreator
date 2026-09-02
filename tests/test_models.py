from __future__ import annotations

from pathlib import Path

from choicer_voicer_pack_creator.models import PackProject, Segment


def test_project_sorts_segments_and_preserves_speaker_order(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    project = PackProject(
        title="Example",
        authors=["Creator"],
        video_path=str(video),
        video_duration=10,
        segments=[
            Segment(5, 6, "Later", ["Bob", "Alice"]),
            Segment(1, 2, "Earlier", ["Alice"]),
        ],
    )
    project.sort_segments()
    assert [item.caption for item in project.segments] == ["Earlier", "Later"]
    assert project.speakers == ["Alice", "Bob"]
    assert project.validate() == []


def test_project_validation_reports_actionable_segment_errors(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    project = PackProject(
        title="",
        authors=[],
        video_path=str(video),
        video_duration=2,
        segments=[Segment(1.5, 2.5)],
    )
    errors = project.validate()
    assert any("title" in item.lower() for item in errors)
    assert any("author" in item.lower() for item in errors)
    assert any("after the video" in item.lower() for item in errors)
    assert any("caption" in item.lower() for item in errors)
    assert any("speaker" in item.lower() for item in errors)


def test_clone_gets_independent_identity() -> None:
    original = Segment(1, 2, "Together", ["A"], audio_mode="file", audio_path="a.mp3")
    clone = original.clone()
    assert clone.id != original.id
    assert clone.to_dict() | {"id": original.id} == original.to_dict()


def test_imported_recording_requires_reviewed_source_range(tmp_path: Path) -> None:
    video = tmp_path / "source.ogv"
    audio = tmp_path / "prompt.mp3"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    project = PackProject(
        title="Imported",
        authors=["Creator"],
        video_path=str(video),
        video_duration=5,
        segments=[
            Segment(
                1,
                2,
                "Line",
                ["Speaker"],
                audio_mode="video",
                audio_path=str(audio),
                source_range_known=False,
            )
        ],
    )
    assert any("original source cut is unknown" in error for error in project.validate())
