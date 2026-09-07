from __future__ import annotations

from dataclasses import dataclass

from choicer_voicer_pack_creator.models import Segment


@dataclass(frozen=True, slots=True)
class TimelineOverlap:
    """A non-blocking pair of ranges that may need human review."""

    first_id: str
    second_id: str
    seconds: float


def describe_timeline_overlaps(
    segments: list[Segment], warnings: list[TimelineOverlap],
) -> list[str]:
    indexed = {segment.id: (index, segment) for index, segment in enumerate(segments, 1)}
    details = []
    for warning in warnings:
        first, second = indexed.get(warning.first_id), indexed.get(warning.second_id)
        if first is not None and second is not None:
            details.append(
                f"Segments {first[0]:03d} ({first[1].primary_character}) and "
                f"{second[0]:03d} ({second[1].primary_character}) overlap by "
                f"{warning.seconds:.3f}s."
            )
    return details


def audit_timeline_overlaps(
    segments: list[Segment],
    *,
    minimum_seconds: float = 0.125,
    intentional_layer_tolerance: float = 0.001,
) -> list[TimelineOverlap]:
    """Find substantial non-identical overlaps without mutating segment order.

    Exact duplicate ranges are intentional layers used for simultaneous speakers.
    Short overlaps are tolerated because hand-edited boundaries and codec timing can
    differ slightly without representing two competing voice prompts.
    """

    ordered = sorted(enumerate(segments), key=lambda item: (item[1].start, item[1].end, item[0]))
    active: list[Segment] = []
    warnings: list[TimelineOverlap] = []
    for _, current in ordered:
        active = [
            previous
            for previous in active
            if previous.end - current.start > minimum_seconds
        ]
        for previous in active:
            overlap = min(previous.end, current.end) - max(previous.start, current.start)
            if overlap <= minimum_seconds:
                continue
            previous_speakers = {name.casefold() for name in previous.characters}
            current_speakers = {name.casefold() for name in current.characters}
            intentional_layer = (
                abs(previous.start - current.start) <= intentional_layer_tolerance
                and abs(previous.end - current.end) <= intentional_layer_tolerance
                and bool(previous_speakers)
                and bool(current_speakers)
                and previous_speakers.isdisjoint(current_speakers)
            )
            if intentional_layer:
                continue
            warnings.append(
                TimelineOverlap(
                    first_id=previous.id,
                    second_id=current.id,
                    seconds=round(overlap, 6),
                )
            )
        active.append(current)
    return warnings
