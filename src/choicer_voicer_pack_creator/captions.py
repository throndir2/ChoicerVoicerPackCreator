from __future__ import annotations

import html
import math
from dataclasses import dataclass
from typing import Any

from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.models import SourceCaption


def parse_json3(
    value: Any, duration: float, *, automatic: bool, language: str
) -> list[SourceCaption]:
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise ValueError("YouTube captions did not contain JSON3 events")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Captions require a finite, positive video duration")
    source = f"YouTube {'automatic' if automatic else 'creator'} ({language})"
    cues: list[SourceCaption] = []
    windows: list[object] = []
    for event in value["events"]:
        if not isinstance(event, dict):
            raise ValueError("Invalid YouTube caption event")
        segments = event.get("segs", [])
        if not isinstance(segments, list) or not all(isinstance(s, dict) for s in segments):
            raise ValueError("Invalid YouTube caption text")
        text = html.unescape("".join(str(s.get("utf8", "")) for s in segments))
        text = " ".join(text.split())
        if not text:
            continue  # Window definitions and newline-only events are not spoken cues.
        start = float(event["tStartMs"]) / 1000
        length = float(event.get("dDurationMs", duration * 1000 - start * 1000)) / 1000
        if not math.isfinite(start) or not math.isfinite(length) or length <= 0:
            raise ValueError("Invalid YouTube caption timestamp")
        end = min(duration, start + length)
        start = max(0.0, start)
        if end - start < 0.05:
            continue
        window = event.get("wWinId")
        if event.get("aAppend") and cues and windows[-1] == window:
            previous = cues[-1]
            cues[-1] = SourceCaption(
                previous.start, max(previous.end, end), f"{previous.text} {text}", source
            )
        else:
            cues.append(SourceCaption(start, end, text, source))
            windows.append(window)
    cues.sort(key=lambda cue: (cue.start, cue.end))
    if automatic:
        # Auto-caption windows linger on screen; don't mistake that for overlapping speech.
        cues = [
            SourceCaption(cue.start, min(cue.end, following.start), cue.text, cue.source)
            if following and cue.start < following.start < cue.end
            else cue
            for cue, following in zip(cues, [*cues[1:], None], strict=True)
        ]
    return [cue for cue in cues if cue.end - cue.start >= 0.05]


@dataclass(frozen=True, slots=True)
class CaptionComparison:
    text: str
    status: str
    timing: str


def compare_caption(
    start: float, end: float, text: str, transcripts: list[AnalysisSuggestion]
) -> CaptionComparison:
    matches = sorted(
        (
            item for item in transcripts
            if item.caption.strip() and min(end, item.end) > max(start, item.start)
        ),
        key=lambda item: (item.start, item.end),
    )
    if not matches:
        return CaptionComparison("", "No Whisper match - review", "")
    draft = " ".join(item.caption.strip() for item in matches)
    def normalize(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    same_text = bool(normalize(text)) and normalize(text) == normalize(draft)
    aligned = abs(start - matches[0].start) <= 0.5 and abs(end - matches[-1].end) <= 0.5
    status = (
        "Text agrees"
        if same_text and aligned
        else "Timing differs - review"
        if same_text
        else "Text differs - review"
    )
    timing = "; ".join(
        f"{item.start:.3f}-{item.end:.3f}: {item.caption}" for item in matches
    )
    return CaptionComparison(draft, status, timing)
