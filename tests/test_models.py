from __future__ import annotations

from pathlib import Path

import pytest

from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    CaptionFragment,
    PackProject,
    Segment,
    SourceCaption,
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
        local_model_name="tiny", local_detected_language="en",
    )
    project = PackProject(analysis_review=review)
    assert PackProject.from_dict(project.to_dict()).analysis_review == review


@pytest.mark.parametrize("review", [
    [], {"youtube_rows": [None]}, {"local_rows": {}},
    {"selected_source": "unknown"}, {"local_source": "unknown"},
    {"local_model_name": None}, {"local_detected_language": []},
    {"local_rows": [{"start": "1", "end": "2", "caption": "", "source": "Whisper",
                     "confidence": float("nan")}]},
])
def test_invalid_review_state_is_reported(review) -> None:
    with pytest.raises(ValueError):
        PackProject.from_dict({"analysis_review": review})


def test_caption_fragment_and_three_independent_drafts_round_trip() -> None:
    cue = SourceCaption(1, 3, "Raw & text", "YouTube", (
        CaptionFragment(" Raw & ", 1.1), CaptionFragment("text", None),
    ))
    review = AnalysisReview(
        [AnalysisDraftRow("1", "3", "YouTube edit", "YouTube")],
        [AnalysisDraftRow("1.5", "3", "Whisper edit", "Whisper", 0.8)],
        "refined", "Whisper",
        [AnalysisDraftRow("unfinished", "3.4", "Refined edit", "Refined YouTube", checked=False)],
        0.8,
    )
    project = PackProject(source_captions=[cue], analysis_review=review)
    restored = PackProject.from_dict(project.to_dict())
    assert restored.source_captions == [cue]
    assert restored.analysis_review == review
    assert restored.source_captions[0].fragments[1].start is None


def test_previous_caption_and_review_format_load_with_refinement_defaults() -> None:
    project = PackProject.from_dict({
        "source_captions": [{"start": 1, "end": 2, "text": "Original", "source": "YouTube"}],
        "analysis_review": {"selected_source": "youtube", "local_source": "Audio activity"},
    })
    assert project.source_captions == [SourceCaption(1, 2, "Original", "YouTube")]
    assert project.analysis_review == AnalysisReview([], [], "youtube", "Audio activity")
    assert project.analysis_review.refined_rows == []
    assert project.analysis_review.pause_threshold == 0.4
    assert project.analysis_review.local_model_name == ""
    assert project.analysis_review.local_detected_language == ""


@pytest.mark.parametrize("fragments", [
    None, {}, "text", [None], [{"text": None}], [{"text": 1}],
    [{"text": "word", "start": "1"}], [{"text": "word", "start": True}],
    [{"text": "word", "start": []}], [{"text": "word", "start": -0.1}],
    [{"text": "word", "start": float("nan")}],
    [{"text": "word", "start": float("inf")}],
])
def test_invalid_persisted_fragments_are_rejected(fragments) -> None:
    with pytest.raises(ValueError, match="fragment"):
        SourceCaption.from_dict({
            "start": 0, "end": 3, "text": "word", "source": "YouTube",
            "fragments": fragments,
        })


@pytest.mark.parametrize("threshold", [
    None, {}, "0.4", True, -1, 0.199, 1.001, float("nan"), float("inf"),
])
def test_invalid_persisted_pause_threshold_is_rejected(threshold) -> None:
    with pytest.raises(ValueError, match="pause threshold"):
        AnalysisReview.from_dict({"pause_threshold": threshold})


@pytest.mark.parametrize("threshold", [0.2, 0.4, 1.0])
def test_persisted_pause_threshold_accepts_inclusive_bounds(threshold) -> None:
    assert AnalysisReview.from_dict({"pause_threshold": threshold}).pause_threshold == threshold


def test_invalid_refined_rows_are_validated_like_other_drafts() -> None:
    with pytest.raises(ValueError, match="draft rows"):
        AnalysisReview.from_dict({"refined_rows": [None]})
