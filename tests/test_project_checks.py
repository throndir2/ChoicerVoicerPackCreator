from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope
from choicer_voicer_pack_creator.project_checks import (
    ProjectChecksInput,
    SegmentChecksInput,
    check_project,
)
from choicer_voicer_pack_creator.timeline_audit import audit_timeline_overlaps


def test_checks_use_minimal_frozen_inputs_and_existing_rules(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")
    project = PackProject(
        title="Checks", authors=["Author"], video_path=str(video), video_duration=10,
        segments=[
            Segment(1, 3, "One", ["Alice"]),
            Segment(2, 4, "Two", ["Bob"]),
            Segment(4, 5, "", [], audio_mode="file", audio_path=str(tmp_path / "missing.mp3")),
        ],
    )
    metadata = ProjectChecksInput.capture(project)
    segments = tuple(SegmentChecksInput.capture(segment) for segment in project.segments)
    expected_errors = project.validate()
    expected_overlaps = audit_timeline_overlaps(project.segments)
    project.title = ""
    project.segments[0].characters.append("Carol")
    project.segments[1].start = 5
    result = check_project(metadata, segments)
    assert list(result.errors) == expected_errors
    assert list(result.overlaps) == expected_overlaps
    assert result.speaker_count == 2
    assert result.segment_count == 3
    assert result.details == ("Segments 001 (Alice) and 002 (Bob) overlap by 1.000s.",)
    assert not hasattr(metadata, "analysis_review")
    with pytest.raises(FrozenInstanceError):
        segments[0].start = 9


def test_checks_remain_cooperatively_cancellable():
    project = PackProject(segments=[Segment(0, 2)])
    cancelled = False
    with pytest.raises(OperationCancelled), operation_scope(cancelled=lambda: cancelled):
        cancelled = True
        check_project(
            ProjectChecksInput.capture(project),
            tuple(SegmentChecksInput.capture(segment) for segment in project.segments),
        )
