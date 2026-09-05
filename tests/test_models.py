from __future__ import annotations

from pathlib import Path

import pytest

from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    Segment,
)


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


def test_old_project_without_review_still_loads() -> None:
    project = PackProject.from_dict({"schema_version": 1, "segments": []})
    assert project.analysis_review is None


def test_analysis_review_round_trip_preserves_unfinished_edits() -> None:
    review = AnalysisReview(
        [AnalysisDraftRow("in progress", "2", "Edited YouTube", "YouTube", checked=False)],
        [AnalysisDraftRow("0.5", "3", "Edited Whisper", "Whisper", 0.876)],
        "local",
    )
    project = PackProject(analysis_review=review)
    assert PackProject.from_dict(project.to_dict()).analysis_review == review


@pytest.mark.parametrize("review", [
    [], {"youtube_rows": [None]}, {"local_rows": {}},
    {"selected_source": "unknown"}, {"local_source": "unknown"},
    {"local_rows": [{"start": "1", "end": "2", "caption": "", "source": "Whisper",
                     "confidence": float("nan")}]},
])
def test_invalid_review_state_is_reported(review) -> None:
    with pytest.raises(ValueError):
        PackProject.from_dict({"analysis_review": review})
