"""Minimal immutable inputs for advisory editor checks (not export validation)."""

from __future__ import annotations

from dataclasses import dataclass

from choicer_voicer_pack_creator.models import AudioMode, PackProject, Segment
from choicer_voicer_pack_creator.operations import check_cancelled
from choicer_voicer_pack_creator.timeline_audit import TimelineOverlap, audit_timeline_overlaps


@dataclass(frozen=True, slots=True)
class SegmentChecksInput:
    id: str
    start: float
    end: float
    caption: str
    characters: tuple[str, ...]
    audio_mode: AudioMode
    audio_path: str
    image_path: str
    source_range_known: bool

    @classmethod
    def capture(cls, segment: Segment) -> SegmentChecksInput:
        return cls(
            segment.id, segment.start, segment.end, segment.caption, tuple(segment.characters),
            segment.audio_mode, segment.audio_path, segment.image_path, segment.source_range_known,
        )

    def segment(self) -> Segment:
        return Segment(
            id=self.id, start=self.start, end=self.end, caption=self.caption,
            characters=list(self.characters), audio_mode=self.audio_mode,
            audio_path=self.audio_path, image_path=self.image_path,
            source_range_known=self.source_range_known,
        )


@dataclass(frozen=True, slots=True)
class ProjectChecksInput:
    title: str
    authors: tuple[str, ...]
    video_path: str
    video_duration: float
    backing_track_path: str
    icon_path: str
    head_padding: float
    tail_padding: float
    video_height: int
    video_fps: int

    @classmethod
    def capture(cls, project: PackProject) -> ProjectChecksInput:
        return cls(
            project.title, tuple(project.authors), project.video_path, project.video_duration,
            project.backing_track_path, project.icon_path, project.head_padding,
            project.tail_padding, project.video_height, project.video_fps,
        )


@dataclass(frozen=True, slots=True)
class ProjectChecksResult:
    errors: tuple[str, ...]
    overlaps: tuple[TimelineOverlap, ...]
    details: tuple[str, ...]
    segment_count: int
    speaker_count: int


def check_project(
    metadata: ProjectChecksInput, segments: tuple[SegmentChecksInput, ...],
) -> ProjectChecksResult:
    check_cancelled()
    project = PackProject(
        title=metadata.title, authors=list(metadata.authors), video_path=metadata.video_path,
        video_duration=metadata.video_duration, backing_track_path=metadata.backing_track_path,
        icon_path=metadata.icon_path, head_padding=metadata.head_padding,
        tail_padding=metadata.tail_padding, video_height=metadata.video_height,
        video_fps=metadata.video_fps, segments=[item.segment() for item in segments],
    )
    errors = project.validate()
    check_cancelled()
    overlaps = audit_timeline_overlaps(project.segments)
    indexes = {segment.id: (index, segment) for index, segment in enumerate(project.segments, 1)}
    details = []
    for warning in overlaps:
        check_cancelled()
        first_index, first = indexes[warning.first_id]
        second_index, second = indexes[warning.second_id]
        details.append(
            f"Segments {first_index:03d} ({first.primary_character}) and "
            f"{second_index:03d} ({second.primary_character}) overlap by "
            f"{warning.seconds:.3f}s."
        )
    return ProjectChecksResult(
        tuple(errors), tuple(overlaps), tuple(details), len(segments),
        len({name for segment in segments for name in segment.characters if name}),
    )
