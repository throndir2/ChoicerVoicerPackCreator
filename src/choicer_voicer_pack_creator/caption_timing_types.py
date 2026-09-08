from __future__ import annotations

from dataclasses import dataclass

from choicer_voicer_pack_creator.models import SourceCaption


@dataclass(frozen=True, slots=True)
class TimingWord:
    text: str
    start: float
    end: float
    probability: float


@dataclass(frozen=True, slots=True)
class CaptionTimingEvidence:
    index: int
    words: tuple[TimingWord, ...] = ()
    recognized_words: tuple[TimingWord, ...] = ()
    problem: str = ""


@dataclass(frozen=True, slots=True)
class CaptionTimingResult:
    captions: tuple[SourceCaption, ...]
    confidences: tuple[float | None, ...]
    review_reasons: tuple[str, ...]
