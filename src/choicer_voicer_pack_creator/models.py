from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

AudioMode = Literal["video", "file"]


@dataclass(frozen=True, slots=True)
class CaptionFragment:
    text: str
    start: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CaptionFragment:
        text = value.get("text")
        start = value.get("start")
        if not isinstance(text, str):
            raise ValueError("Caption fragment text must be a string")
        if start is not None:
            if isinstance(start, bool) or not isinstance(start, (int, float)):
                raise ValueError("Caption fragment start must be a number or null")
            if not math.isfinite(start) or start < 0:
                raise ValueError("Caption fragment has an invalid start")
            start = float(start)
        return cls(text, start)


@dataclass(frozen=True, slots=True)
class SourceCaption:
    start: float
    end: float
    text: str
    source: str
    fragments: tuple[CaptionFragment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "source": self.source,
            "fragments": [fragment.to_dict() for fragment in self.fragments],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceCaption:
        start, end = float(value["start"]), float(value["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Source caption has an invalid time range")
        fragments = value.get("fragments", [])
        if not isinstance(fragments, list) or not all(
            isinstance(fragment, dict) for fragment in fragments
        ):
            raise ValueError("Caption fragments must be an array of JSON objects")
        return cls(
            start, end, str(value["text"]), str(value["source"]),
            tuple(CaptionFragment.from_dict(fragment) for fragment in fragments),
        )


@dataclass(frozen=True, slots=True)
class AnalysisDraftRow:
    # Keep time edits as text so even an unfinished/invalid edit survives closing the review.
    start: str
    end: str
    caption: str
    source: str
    confidence: float | None = None
    checked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "caption": self.caption,
            "source": self.source,
            "confidence": self.confidence,
            "checked": self.checked,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AnalysisDraftRow:
        confidence = value.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Analysis draft confidence must be between zero and one")
        return cls(
            str(value["start"]), str(value["end"]), str(value["caption"]),
            str(value["source"]), confidence, bool(value.get("checked", True)),
        )


@dataclass(frozen=True, slots=True)
class AnalysisReview:
    youtube_rows: list[AnalysisDraftRow] = field(default_factory=list)
    local_rows: list[AnalysisDraftRow] = field(default_factory=list)
    selected_source: str = "local"
    local_source: str = "Whisper"
    refined_rows: list[AnalysisDraftRow] = field(default_factory=list)
    pause_threshold: float = 0.4
    local_model_name: str = ""
    local_detected_language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "youtube_rows": [row.to_dict() for row in self.youtube_rows],
            "local_rows": [row.to_dict() for row in self.local_rows],
            "selected_source": self.selected_source,
            "local_source": self.local_source,
            "refined_rows": [row.to_dict() for row in self.refined_rows],
            "pause_threshold": self.pause_threshold,
            "local_model_name": self.local_model_name,
            "local_detected_language": self.local_detected_language,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AnalysisReview:
        rows: dict[str, list[AnalysisDraftRow]] = {}
        for key in ("youtube_rows", "local_rows", "refined_rows"):
            items = value.get(key, [])
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ValueError("Analysis draft rows must be an array of JSON objects")
            rows[key] = [AnalysisDraftRow.from_dict(item) for item in items]
        selected_source = str(value.get("selected_source", "local"))
        local_source = str(value.get("local_source", "Whisper"))
        if selected_source not in {"youtube", "local", "refined"}:
            raise ValueError("Unknown analysis transcript selection")
        if local_source not in {"Whisper", "Audio activity"}:
            raise ValueError("Unknown local analysis source")
        local_model_name = value.get("local_model_name", "")
        local_detected_language = value.get("local_detected_language", "")
        if not isinstance(local_model_name, str) or not isinstance(local_detected_language, str):
            raise ValueError("Analysis draft model and language must be text")
        pause_threshold = value.get("pause_threshold", 0.4)
        if (
            isinstance(pause_threshold, bool)
            or not isinstance(pause_threshold, (int, float))
            or not math.isfinite(pause_threshold)
            or not 0.2 <= pause_threshold <= 1.0
        ):
            raise ValueError("Caption pause threshold must be between 0.2 and 1.0 seconds")
        return cls(
            rows["youtube_rows"], rows["local_rows"], selected_source, local_source,
            rows["refined_rows"], float(pause_threshold), local_model_name, local_detected_language,
        )


@dataclass(slots=True)
class Segment:
    """One playable dub prompt on the video timeline."""

    start: float
    end: float
    caption: str = ""
    characters: list[str] = field(default_factory=list)
    audio_mode: AudioMode = "video"
    audio_path: str = ""
    image_path: str = ""
    source_range_known: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def primary_character(self) -> str:
        return self.characters[0] if self.characters else "Unassigned"

    def clone(self) -> Segment:
        return Segment(
            start=self.start,
            end=self.end,
            caption=self.caption,
            characters=list(self.characters),
            audio_mode=self.audio_mode,
            audio_path=self.audio_path,
            image_path=self.image_path,
            source_range_known=self.source_range_known,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 6),
            "end": round(self.end, 6),
            "caption": self.caption,
            "characters": list(self.characters),
            "audio_mode": self.audio_mode,
            "audio_path": self.audio_path,
            "image_path": self.image_path,
            "source_range_known": self.source_range_known,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Segment:
        mode = str(value.get("audio_mode", "video"))
        if mode not in {"video", "file"}:
            mode = "video"
        characters = value.get("characters", [])
        if not isinstance(characters, list):
            characters = [str(characters)] if characters else []
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex),
            start=float(value.get("start", 0.0)),
            end=float(value.get("end", 3.0)),
            caption=str(value.get("caption", "")),
            characters=[str(item).strip() for item in characters if str(item).strip()],
            audio_mode=mode,  # type: ignore[arg-type]
            audio_path=str(value.get("audio_path", "")),
            image_path=str(value.get("image_path", "")),
            source_range_known=bool(value.get("source_range_known", mode == "video")),
        )


@dataclass(slots=True)
class PackProject:
    """Editable source of truth for one Choicer Voicer pack."""

    title: str = "Untitled Dub Pack"
    authors: list[str] = field(default_factory=list)
    readme: str = ""
    video_path: str = ""
    video_duration: float = 0.0
    backing_track_path: str = ""
    icon_path: str = ""
    segments: list[Segment] = field(default_factory=list)
    head_padding: float = 0.15
    tail_padding: float = 0.25
    video_height: int = 720
    video_fps: int = 30
    source_pack_path: str = ""
    preserve_source_video: bool = False
    import_warnings: list[str] = field(default_factory=list)
    source_url: str = ""
    caption_language: str = ""
    source_captions: list[SourceCaption] = field(default_factory=list)
    analysis_review: AnalysisReview | None = None

    @property
    def speakers(self) -> list[str]:
        result: list[str] = []
        for segment in self.segments:
            for character in segment.characters:
                if character and character not in result:
                    result.append(character)
        return result

    def sort_segments(self) -> None:
        self.segments.sort(key=lambda item: (item.start, item.end))

    def segment_by_id(self, segment_id: str) -> Segment | None:
        return next((item for item in self.segments if item.id == segment_id), None)

    def add_segment(self, segment: Segment) -> None:
        self.segments.append(segment)
        self.sort_segments()

    def remove_segment(self, segment_id: str) -> bool:
        previous = len(self.segments)
        self.segments = [item for item in self.segments if item.id != segment_id]
        return len(self.segments) != previous

    def combine_segments(
        self, segment_ids: list[str], *, discard_other_images: bool = False
    ) -> Segment:
        identifiers = set(segment_ids)
        if len(identifiers) < 2:
            raise ValueError("Select at least two segments to combine.")
        selected = sorted(
            (segment for segment in self.segments if segment.id in identifiers),
            key=lambda segment: (segment.start, segment.end),
        )
        if len(selected) != len(identifiers):
            raise ValueError("A selected segment no longer exists. Select the segments again.")
        if any(segment.audio_mode != "video" or not segment.source_range_known for segment in selected):
            raise ValueError(
                "Preserved recordings cannot be combined safely. For each recording, mark the "
                "exact source-video In/Out range, click Apply Range, and choose Yes to regenerate "
                "its prompt audio. Then select the segments and combine them."
            )
        if any(
            not math.isfinite(segment.start) or not math.isfinite(segment.end)
            or segment.start < 0 or segment.end <= segment.start
            for segment in selected
        ):
            raise ValueError("Correct the selected segments' In/Out ranges before combining them.")
        images = list(dict.fromkeys(segment.image_path for segment in selected if segment.image_path))
        if len(images) > 1 and not discard_other_images:
            raise ValueError("Combining different still images requires choosing which image to keep.")
        combined = Segment(
            start=selected[0].start,
            end=max(segment.end for segment in selected),
            caption=" ".join(segment.caption.strip() for segment in selected if segment.caption.strip()),
            characters=list(dict.fromkeys(
                character for segment in selected for character in segment.characters
            )),
            image_path=images[0] if images else "",
        )
        self.segments = [
            segment for segment in self.segments if segment.id not in identifiers
        ]
        self.add_segment(combined)
        return combined

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.title.strip():
            errors.append("Pack title is required.")
        if not self.authors or not any(author.strip() for author in self.authors):
            errors.append("At least one author is required.")
        if not self.video_path:
            errors.append("A source video is required.")
        elif not Path(self.video_path).is_file():
            errors.append(f"Source video does not exist: {self.video_path}")
        if self.backing_track_path and not Path(self.backing_track_path).is_file():
            errors.append(f"Backing track does not exist: {self.backing_track_path}")
        if self.icon_path and not Path(self.icon_path).is_file():
            errors.append(f"Icon does not exist: {self.icon_path}")
        if not self.segments:
            errors.append("At least one segment is required.")
        if not 0.0 <= self.head_padding <= 2.0:
            errors.append("Head padding must be between 0 and 2 seconds.")
        if not 0.0 <= self.tail_padding <= 2.0:
            errors.append("Tail padding must be between 0 and 2 seconds.")
        if self.video_height < 144 or self.video_height > 2160:
            errors.append("Video height must be between 144 and 2160 pixels.")
        if self.video_fps < 1 or self.video_fps > 120:
            errors.append("Video frame rate must be between 1 and 120 fps.")

        seen_ids: set[str] = set()
        for index, segment in enumerate(self.segments, start=1):
            prefix = f"Segment {index}"
            if segment.id in seen_ids:
                errors.append(f"{prefix} has a duplicate identifier.")
            seen_ids.add(segment.id)
            if not math.isfinite(segment.start) or not math.isfinite(segment.end):
                errors.append(f"{prefix} has a non-finite timestamp.")
            elif segment.start < 0 or segment.end <= segment.start:
                errors.append(f"{prefix} must end after a non-negative start time.")
            elif self.video_duration > 0 and segment.end > self.video_duration + 0.05:
                errors.append(
                    f"{prefix} ends at {segment.end:.3f}s, after the video ends at "
                    f"{self.video_duration:.3f}s."
                )
            if not segment.caption.strip():
                errors.append(f"{prefix} needs a caption.")
            if not segment.characters or not all(name.strip() for name in segment.characters):
                errors.append(f"{prefix} needs at least one speaker.")
            if segment.audio_mode == "file":
                if not segment.audio_path:
                    errors.append(f"{prefix} is set to file audio but no audio file is selected.")
                elif not Path(segment.audio_path).is_file():
                    errors.append(f"{prefix} audio file does not exist: {segment.audio_path}")
            elif not segment.source_range_known:
                errors.append(
                    f"{prefix} came from an imported recording whose original source cut is "
                    "unknown. Set a precise In/Out range before regenerating it from video."
                )
            if segment.image_path and not Path(segment.image_path).is_file():
                errors.append(f"{prefix} image does not exist: {segment.image_path}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "title": self.title,
            "authors": list(self.authors),
            "readme": self.readme,
            "video_path": self.video_path,
            "video_duration": round(self.video_duration, 6),
            "backing_track_path": self.backing_track_path,
            "icon_path": self.icon_path,
            "head_padding": self.head_padding,
            "tail_padding": self.tail_padding,
            "video_height": self.video_height,
            "video_fps": self.video_fps,
            "source_pack_path": self.source_pack_path,
            "preserve_source_video": self.preserve_source_video,
            "import_warnings": list(self.import_warnings),
            "source_url": self.source_url,
            "caption_language": self.caption_language,
            "source_captions": [caption.to_dict() for caption in self.source_captions],
            "analysis_review": self.analysis_review.to_dict() if self.analysis_review else None,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PackProject:
        if int(value.get("schema_version", 1)) != 1:
            raise ValueError(f"Unsupported project schema: {value.get('schema_version')}")
        authors = value.get("authors", [])
        if not isinstance(authors, list):
            authors = [str(authors)] if authors else []
        segments_value = value.get("segments", [])
        if not isinstance(segments_value, list):
            raise ValueError("Project segments must be an array")
        if not all(isinstance(item, dict) for item in segments_value):
            raise ValueError("Every project segment must be a JSON object")
        captions_value = value.get("source_captions", [])
        if not isinstance(captions_value, list) or not all(
            isinstance(item, dict) for item in captions_value
        ):
            raise ValueError("Project source captions must be an array of JSON objects")
        review_value = value.get("analysis_review")
        if review_value is not None and not isinstance(review_value, dict):
            raise ValueError("Project analysis review must be a JSON object")
        import_warnings = value.get("import_warnings", [])
        if not isinstance(import_warnings, list):
            import_warnings = [str(import_warnings)] if import_warnings else []
        project = cls(
            title=str(value.get("title", "Untitled Dub Pack")),
            authors=[str(item).strip() for item in authors if str(item).strip()],
            readme=str(value.get("readme", "")),
            video_path=str(value.get("video_path", "")),
            video_duration=float(value.get("video_duration", 0.0)),
            backing_track_path=str(value.get("backing_track_path", "")),
            icon_path=str(value.get("icon_path", "")),
            head_padding=float(value.get("head_padding", 0.15)),
            tail_padding=float(value.get("tail_padding", 0.25)),
            video_height=int(value.get("video_height", 720)),
            video_fps=int(value.get("video_fps", 30)),
            source_pack_path=str(value.get("source_pack_path", "")),
            preserve_source_video=bool(value.get("preserve_source_video", False)),
            import_warnings=[str(item) for item in import_warnings if str(item).strip()],
            source_url=str(value.get("source_url", "")),
            caption_language=str(value.get("caption_language", "")),
            source_captions=[SourceCaption.from_dict(item) for item in captions_value],
            analysis_review=AnalysisReview.from_dict(review_value) if review_value is not None else None,
            segments=[Segment.from_dict(item) for item in segments_value],
        )
        project.sort_segments()
        return project
