from __future__ import annotations

from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.timeline_audit import audit_timeline_overlaps


def test_timeline_audit_ignores_touching_short_and_intentional_layers() -> None:
    touching = Segment(0, 1, "A", ["A"])
    short = Segment(0.875, 2, "B", ["B"])
    layer_one = Segment(3, 4, "Together", ["A"])
    layer_two = Segment(3, 4, "Together", ["B"])

    assert audit_timeline_overlaps([touching, short, layer_one, layer_two]) == []


def test_timeline_audit_reports_every_substantial_nested_overlap_without_mutation() -> None:
    long = Segment(1, 6, "Long", ["Narrator"])
    middle = Segment(2, 3, "Middle", ["A"])
    later = Segment(4, 5, "Later", ["B"])
    original = [later, long, middle]

    warnings = audit_timeline_overlaps(original)

    assert original == [later, long, middle]
    assert [(item.first_id, item.second_id, item.seconds) for item in warnings] == [
        (long.id, middle.id, 1.0),
        (long.id, later.id, 1.0),
    ]


def test_timeline_audit_threshold_is_non_blocking_and_precise() -> None:
    first = Segment(0, 1, "First", ["A"])
    boundary = Segment(0.875, 2, "Boundary", ["B"])
    warning = Segment(1.874, 3, "Warning", ["C"])

    results = audit_timeline_overlaps([first, boundary, warning])

    assert len(results) == 1
    assert results[0].first_id == boundary.id
    assert results[0].second_id == warning.id
    assert results[0].seconds == 0.126


def test_identical_range_requires_distinct_speakers_to_count_as_layer() -> None:
    original = Segment(1, 2, "Same line", ["A"])
    accidental_duplicate = Segment(1, 2, "Same line", ["A"])
    intentional_layer = Segment(1, 2, "Together", ["B"])

    results = audit_timeline_overlaps(
        [original, accidental_duplicate, intentional_layer]
    )

    assert len(results) == 1
    assert results[0].first_id == original.id
    assert results[0].second_id == accidental_duplicate.id


def test_identical_range_with_partially_shared_speakers_is_flagged() -> None:
    first = Segment(1, 2, "Together", ["Alice"])
    second = Segment(1, 2, "Together", ["Alice", "Bob"])

    results = audit_timeline_overlaps([first, second])

    assert len(results) == 1
    assert results[0].first_id == first.id
    assert results[0].second_id == second.id
