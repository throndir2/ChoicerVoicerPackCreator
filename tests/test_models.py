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


def test_new_projects_default_to_fast_video_profile() -> None:
    project = PackProject()
    assert (project.video_height, project.video_fps) == (480, 30)
    data = project.to_dict()
    del data["video_height"]
    del data["video_fps"]
    restored = PackProject.from_dict(data)
    assert (restored.video_height, restored.video_fps) == (480, 30)


def test_saved_higher_quality_profile_is_preserved() -> None:
    project = PackProject(video_height=1080, video_fps=60)
    restored = PackProject.from_dict(project.to_dict())
    assert (restored.video_height, restored.video_fps) == (1080, 60)


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


def test_combine_segments_joins_in_timeline_order_and_preserves_unselected() -> None:
    first = Segment(1, 9, " First\nline ", ["Alice", "Bob"])
    second = Segment(2, 3, " \n ", ["Bob"])
    last = Segment(6, 7, "Last line ", ["Carol", "Alice"], image_path="still.png")
    untouched = Segment(4, 5, "Do not combine", ["Dave"], audio_mode="file", audio_path="dub.mp3")
    project = PackProject(segments=[last, untouched, second, first])
    before = [segment.to_dict() for segment in project.segments]

    combined = project.combine_segments([last.id, first.id, second.id])

    assert (combined.start, combined.end) == (1, 9)
    assert combined.caption == "First\nline Last line"
    assert combined.characters == ["Alice", "Bob", "Carol"]
    assert combined.image_path == "still.png"
    assert combined.audio_mode == "video"
    assert combined.audio_path == ""
    assert combined.source_range_known
    assert combined.id not in {first.id, second.id, last.id, untouched.id}
    assert project.segments == [combined, untouched]
    assert [segment.to_dict() for segment in [last, untouched, second, first]] == before
    assert PackProject.from_dict(project.to_dict()).to_dict() == project.to_dict()


@pytest.mark.parametrize("second_start, second_end", [(2, 3), (4, 5), (1, 2)])
def test_combine_touching_gapped_and_identical_ranges_keeps_repeated_lines(
    second_start: float, second_end: float
) -> None:
    first = Segment(1, 2, "Again!", ["Alice"])
    second = Segment(second_start, second_end, "Again!", ["Alice"])
    project = PackProject(segments=[first, second])
    combined = project.combine_segments([second.id, first.id])
    assert (combined.start, combined.end) == (1, max(2, second_end))
    assert combined.caption == "Again! Again!"
    assert combined.characters == ["Alice"]


@pytest.mark.parametrize("selection", [[], [0], [0, 0], [0, 2]])
def test_combine_invalid_selection_is_atomic(selection: list[int]) -> None:
    project = PackProject(segments=[Segment(1, 2), Segment(3, 4)])
    before = project.to_dict()
    identifiers = [segment.id for segment in project.segments] + ["missing"]
    with pytest.raises(ValueError, match="Select at least two|no longer exists"):
        project.combine_segments([identifiers[index] for index in selection])
    assert project.to_dict() == before


@pytest.mark.parametrize("audio_mode, known_range", [("file", True), ("file", False), ("video", False)])
def test_combine_preserved_or_unknown_source_audio_is_atomic(audio_mode, known_range) -> None:
    first = Segment(1, 2, "Original", ["Alice"])
    second = Segment(
        3, 4, "Recording", ["Bob"], audio_mode=audio_mode, audio_path="recording.mp3",
        source_range_known=known_range,
    )
    project = PackProject(segments=[first, second])
    before = project.to_dict()
    with pytest.raises(ValueError, match="Update Segment Timing"):
        project.combine_segments([first.id, second.id])
    assert project.to_dict() == before


@pytest.mark.parametrize("start, end", [(-1, 2), (2, 2), (3, 2), (float("inf"), 4), (1, float("nan"))])
def test_combine_rejects_invalid_timing_without_removing_segments(start, end) -> None:
    first, second = Segment(0, 1), Segment(start, end)
    project = PackProject(segments=[first, second])
    with pytest.raises(ValueError, match="In/Out"):
        project.combine_segments([first.id, second.id])
    assert project.segments == [first, second]


def test_combine_different_images_requires_explicit_permission() -> None:
    first = Segment(1, 2, image_path="first.png")
    second = Segment(3, 4, image_path="second.png")
    project = PackProject(segments=[first, second])
    before = project.to_dict()
    with pytest.raises(ValueError, match="still images"):
        project.combine_segments([second.id, first.id])
    assert project.to_dict() == before
    combined = project.combine_segments([second.id, first.id], discard_other_images=True)
    assert combined.image_path == "first.png"


def test_combine_same_image_needs_no_permission_and_keeps_empty_caption() -> None:
    first = Segment(1, 2, " \n ", image_path="same.png")
    second = Segment(3, 4, "", image_path="same.png")
    project = PackProject(segments=[first, second])
    combined = project.combine_segments([first.id, second.id])
    assert combined.image_path == "same.png"
    assert combined.caption == ""
    assert combined.characters == []


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


def test_current_project_without_optional_review_loads() -> None:
    project = PackProject.from_dict({"schema_version": 1, "segments": []})
    assert project.analysis_review is None


@pytest.mark.parametrize("version", [None, 0, 2, True, 1.0, "1", "invalid", []])
def test_project_requires_explicit_supported_schema(version) -> None:
    with pytest.raises(ValueError, match="Unsupported project schema"):
        PackProject.from_dict({"schema_version": version})


def test_unversioned_json_is_not_a_project() -> None:
    with pytest.raises(ValueError, match="schema_version 1"):
        PackProject.from_dict({})


def test_analysis_review_round_trip_preserves_unfinished_edits() -> None:
    review = AnalysisReview(
        local_rows=[AnalysisDraftRow("0.5", "3", "Edited Whisper", "Whisper", 0.876)],
        refined_rows=[
            AnalysisDraftRow("in progress", "2", "Edited refinement", "YouTube", checked=False),
        ],
        local_model_name="tiny", local_detected_language="en",
    )
    project = PackProject(analysis_review=review)
    assert PackProject.from_dict(project.to_dict()).analysis_review == review


@pytest.mark.parametrize("review", [
    [], {"refined_rows": [None]}, {"local_rows": {}},
    {"selected_source": "unknown"}, {"selected_source": "youtube"}, {"local_source": "unknown"},
    {"local_model_name": None}, {"local_detected_language": []},
    {"local_rows": [{"start": "1", "end": "2", "caption": "", "source": "Whisper",
                     "confidence": float("nan")}]},
])
def test_invalid_review_state_is_reported(review) -> None:
    with pytest.raises(ValueError):
        PackProject.from_dict({"schema_version": 1, "analysis_review": review})


def test_caption_fragment_and_two_independent_drafts_round_trip() -> None:
    cue = SourceCaption(1, 3, "Raw & text", "YouTube", (
        CaptionFragment(" Raw & ", 1.1), CaptionFragment("text", None),
    ))
    review = AnalysisReview(
        local_rows=[AnalysisDraftRow("1.5", "3", "Whisper edit", "Whisper", 0.8)],
        selected_source="refined",
        refined_rows=[
            AnalysisDraftRow("unfinished", "3.4", "Refined edit", "YouTube", checked=False),
        ],
        pause_threshold=0.8,
    )
    project = PackProject(source_captions=[cue], analysis_review=review)
    restored = PackProject.from_dict(project.to_dict())
    assert restored.source_captions == [cue]
    assert restored.analysis_review == review
    assert restored.source_captions[0].fragments[1].start is None


def test_optional_caption_and_review_fields_load_with_defaults() -> None:
    project = PackProject.from_dict({
        "schema_version": 1,
        "source_captions": [{"start": 1, "end": 2, "text": "Original", "source": "YouTube"}],
        "analysis_review": {"local_source": "Audio activity"},
    })
    assert project.source_captions == [SourceCaption(1, 2, "Original", "YouTube")]
    assert project.analysis_review == AnalysisReview(local_source="Audio activity")
    assert project.analysis_review.refined_rows == []
    assert project.analysis_review.pause_threshold == 0.4
    assert project.analysis_review.local_model_name == ""
    assert project.analysis_review.local_detected_language == ""


def test_current_project_loads_without_retaining_obsolete_review_storage() -> None:
    project = PackProject(
        segments=[Segment(1, 2, "Keep this segment", ["Speaker"])],
        source_captions=[SourceCaption(1, 2, "Original evidence", "YouTube")],
        analysis_review=AnalysisReview(
            local_rows=[AnalysisDraftRow("1", "2", "Whisper draft", "Whisper")],
            refined_rows=[AnalysisDraftRow("1", "2", "Refined draft", "YouTube")],
            selected_source="refined",
        ),
    )
    value = project.to_dict()
    value["analysis_review"]["youtube_rows"] = [
        AnalysisDraftRow("1", "2", "Unused original draft", "YouTube").to_dict(),
    ]
    restored = PackProject.from_dict(value)
    assert restored.to_dict() == project.to_dict()
    assert "youtube_rows" not in restored.analysis_review.to_dict()


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
