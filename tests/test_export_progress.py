from __future__ import annotations

import pytest

from choicer_voicer_pack_creator.export_progress import (
    ExportEstimator,
    ExportProgress,
    ExportStep,
    format_remaining,
)
from choicer_voicer_pack_creator.exporter import prompt_description, video_prompt_context
from choicer_voicer_pack_creator.models import Segment


def test_video_throughput_revises_current_and_whole_export_estimates():
    estimator = ExportEstimator()
    plan = (
        ExportStep("video", "Video", "video", 100),
        ExportStep("prompts", "Prompts", "prompts", 20),
        ExportStep("validation", "Validation", "validation", 10),
    )
    estimator.observe(ExportProgress("Start", "video", plan=plan), 0)
    assert estimator.estimate(0).total_remaining == 130
    estimator.observe(ExportProgress("25%", "video", fraction=0.25), 10)
    estimate = estimator.estimate(10)
    assert estimate.step_remaining == 30
    assert estimate.total_remaining == 60
    assert estimate.total_fraction == pytest.approx(10 / 70)
    estimator.observe(ExportProgress("50%", "video", fraction=0.5), 30)
    assert estimator.estimate(30).step_remaining == 30
    # No new frames must not run down the ETA to zero or advance measured work.
    assert estimator.estimate(60).step_remaining == 60
    assert estimator.estimate(60).total_remaining == 90


def test_completed_steps_calibrate_similar_future_work_without_losing_other_stages():
    estimator = ExportEstimator()
    plan = (
        ExportStep("audio-1", "First prompt audio", "audio", 2),
        ExportStep("image", "Image", "image", 1),
        ExportStep("audio-2", "Second prompt audio", "audio", 4),
        ExportStep("validation", "Validation", "validation", 10),
    )
    estimator.observe(ExportProgress("First audio", "audio-1", plan=plan), 0)
    estimator.observe(ExportProgress("Image", "image"), 6)
    estimate = estimator.estimate(6)
    assert estimate.step_remaining == 1
    assert estimate.total_remaining == 1 + 12 + 10
    estimator.observe(ExportProgress("Second audio", "audio-2"), 7)
    assert estimator.estimate(7).step_remaining == 12
    estimator.observe(ExportProgress("Validation", "validation"), 19)
    assert estimator.estimate(19).step_remaining == 10
    assert estimator.estimate(19).total_remaining == 10


def test_unknown_and_overdue_operations_do_not_claim_completion():
    estimator = ExportEstimator()
    assert estimator.estimate(0).total_fraction is None
    estimator.observe(ExportProgress(
        "Work", "work", plan=(ExportStep("work", "Work", "work", 1),),
    ), 0)
    assert estimator.estimate(2).step_remaining is None
    assert estimator.estimate(2).total_remaining is None
    estimator.observe(ExportProgress("Still working", "work", fraction=0.999), 10)
    assert estimator.estimate(10).total_fraction == 0.99
    estimator.observe(ExportProgress("Rolling back", "rollback"), 11)
    assert estimator.estimate(11).total_fraction is None
    assert "re-estimating" in format_remaining(None)
    assert format_remaining(0.001) == "about 1s remaining"
    assert format_remaining(3601) == "about 1h 0m remaining"


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (0, "Before prompt 1/2"),
        (1, 'Prompt 1/2 - Alice (00:01.000 - 00:02.000): "First line"'),
        (1.999, "Prompt 1/2 - Alice"),
        (2, "Between prompts 1 and 2/2"),
        (3, 'Prompt 2/2 - Bob (00:03.000 - 00:04.000): "Second line"'),
        (4, "After final prompt (2/2)"),
    ],
)
def test_video_context_identifies_exact_prompt_and_gaps(position, expected):
    segments = [
        Segment(1, 2, "First line", ["Alice"]),
        Segment(3, 4, "Second line", ["Bob"]),
    ]
    assert expected in video_prompt_context(segments, position)


def test_prompt_context_keeps_history_single_line_and_bounds_caption_preview():
    description = prompt_description(Segment(
        1.25, 2.5, "A\nvery " + "long caption " * 30, ["Alice\nSmith", "Bob"],
    ), 3, 8)
    assert 'Prompt 3/8 - Alice Smith / Bob (00:01.250 - 00:02.500): "A very ' in description
    assert description.endswith('..."')
    assert "\n" not in description
    assert len(description.split(': "', 1)[1][:-1]) == 96
