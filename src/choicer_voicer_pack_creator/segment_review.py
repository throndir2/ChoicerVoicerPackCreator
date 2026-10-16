"""Deterministic review hints for assistant-driven cleanup. Hints are evidence, not decisions."""
from __future__ import annotations

import difflib
import re
from typing import Any

from choicer_voicer_pack_creator.models import PackProject, Segment

SENTENCE_END = (".", "!", "?", "\u2026", "\u3002", "\uff01", "\uff1f")
CLOSERS = "\"'\u201d\u2019)]}\u00bb"
MIN_SECONDS = 0.5
MAX_SECONDS = 15.0
MAX_MERGE_SECONDS = 12.0
MAX_MERGE_GAP = 0.75
FAST_LETTERS_PER_SECOND = 22.0
SLOW_LETTERS_PER_SECOND = 4.0
OVERLAP_TOLERANCE = 0.05
MISMATCH_RATIO = 0.6


def is_nonverbal(caption: str) -> bool:
    text = caption.strip()
    # A caption fully inside brackets or music notes describes a sound, not a spoken line.
    if re.fullmatch(r"(?:\[[^\[\]]*\]|\([^()]*\)|\u266a[^\u266a]*\u266a?)[\s.!?\u2026]*", text):
        return True
    return bool(re.fullmatch(
        r"(?:u+gh|u+h|h+m+|m+m|huh|a+h)[\s.!?\u2026]*", text, re.IGNORECASE,
    ))


def ends_sentence(text: str) -> bool:
    stripped = text.strip().rstrip(CLOSERS)
    # A trailing ellipsis in captions usually marks a line that continues.
    if stripped.endswith(("...", "\u2026")):
        return False
    return stripped.endswith(SENTENCE_END)


def starts_lowercase(text: str) -> bool:
    stripped = text.lstrip(" \"'\u201c\u2018([-\u2013\u2014\u2026.")
    return bool(stripped) and stripped[0].isalpha() and stripped[0].islower()


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.casefold())


def letters(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _speakers(segment: Segment) -> tuple[str, ...]:
    return tuple(sorted(name.strip().casefold() for name in segment.characters if name.strip()))


def _mergeable(segment: Segment) -> bool:
    return segment.audio_mode == "video" and segment.source_range_known


def _pair_reasons(left: Segment, right: Segment) -> list[str]:
    gap = right.start - left.end
    if not -OVERLAP_TOLERANCE <= gap <= MAX_MERGE_GAP:
        return []
    if right.end - left.start > MAX_MERGE_SECONDS:
        return []
    left_speakers, right_speakers = _speakers(left), _speakers(right)
    if left_speakers and right_speakers and left_speakers != right_speakers:
        return []
    if not left.caption.strip() or not right.caption.strip():
        return []
    if is_nonverbal(left.caption) or is_nonverbal(right.caption):
        return []
    reasons = []
    if not ends_sentence(left.caption):
        reasons.append("previous caption has no sentence-ending punctuation")
    if starts_lowercase(right.caption):
        reasons.append("next caption starts in lowercase")
    if reasons:
        reasons.append(f"gap {max(0.0, gap):.2f}s")
    return reasons


def _overlapping_words(left: str, right: str) -> int:
    left_words, right_words = words(left), words(right)
    for size in range(min(len(left_words), len(right_words), 8), 1, -1):
        if left_words[-size:] == right_words[:size]:
            return size
    return 0


def segment_issues(segment: Segment, project: PackProject) -> list[str]:
    kinds = []
    caption = segment.caption.strip()
    names = [name for name in segment.characters if name.strip()]
    if not caption:
        kinds.append("missing_caption")
    elif is_nonverbal(caption):
        kinds.append("nonverbal_caption")
    if not names:
        kinds.append("missing_speaker")
    elif len(names) > 1:
        kinds.append("multiple_speakers")
    if segment.speaker_assignment == "automatic":
        kinds.append("automatic_speaker")
    if segment.duration < MIN_SECONDS:
        kinds.append("too_short")
    elif segment.duration > MAX_SECONDS:
        kinds.append("too_long")
    if caption and not is_nonverbal(caption) and segment.duration > 0:
        rate = letters(caption) / segment.duration
        if rate > FAST_LETTERS_PER_SECOND:
            kinds.append("range_may_cut_speech")
        elif rate < SLOW_LETTERS_PER_SECOND and segment.duration > 3:
            kinds.append("range_may_include_silence")
    if project.video_duration > 0 and segment.end > project.video_duration + 0.05:
        kinds.append("out_of_bounds")
    if segment.audio_mode == "video" and not segment.source_range_known:
        kinds.append("unknown_source_range")
    return kinds


def review_project(project: PackProject, offset: int = 0, limit: int = 200) -> dict[str, Any]:
    """Return per-segment issues and merge candidates for segments[offset:offset+limit]."""
    segments = project.segments
    window = range(offset, min(len(segments), offset + limit))
    issues: list[dict[str, Any]] = []
    for index in window:
        segment = segments[index]
        kinds = segment_issues(segment, project)
        detail: dict[str, Any] = {}
        following = segments[index + 1] if index + 1 < len(segments) else None
        if following is not None:
            if following.start < segment.end - OVERLAP_TOLERANCE:
                kinds.append("overlaps_next")
                detail["overlap_seconds"] = round(segment.end - following.start, 3)
            if segment.caption.strip() and " ".join(words(segment.caption)) == " ".join(
                words(following.caption)
            ):
                kinds.append("duplicate_of_next")
            elif (repeated := _overlapping_words(segment.caption, following.caption)):
                kinds.append("repeats_words_into_next")
                detail["repeated_words"] = repeated
        if kinds:
            issues.append({
                "index": index, "segment_id": segment.id, "kinds": kinds,
                "start": round(segment.start, 3), "end": round(segment.end, 3),
                "caption": segment.caption, "characters": list(segment.characters),
                "speaker_assignment": segment.speaker_assignment, **detail,
            })

    candidates: list[dict[str, Any]] = []
    group: list[int] = []
    reasons: list[str] = []

    def flush() -> None:
        if len(group) > 1:
            chosen = [segments[index] for index in group]
            candidates.append({
                "segment_ids": [item.id for item in chosen],
                "indexes": list(group),
                "start": round(chosen[0].start, 3),
                "end": round(max(item.end for item in chosen), 3),
                "captions": [item.caption for item in chosen],
                "joined_caption": " ".join(item.caption.strip() for item in chosen),
                "reasons": list(dict.fromkeys(reasons)),
            })

    for index in window:
        segment = segments[index]
        if group:
            first = segments[group[0]]
            pair = _pair_reasons(segments[group[-1]], segment)
            if (
                pair and _mergeable(segment)
                and segment.end - first.start <= MAX_MERGE_SECONDS
            ):
                group.append(index)
                reasons.extend(item for item in pair if not item.startswith("gap "))
                continue
            flush()
        group = [index] if _mergeable(segment) else []
        reasons = []
    flush()

    captioned = [item for item in segments if item.caption.strip()]
    punctuated = sum(any(mark in item.caption for mark in ".!?\u3002\uff01\uff1f") for item in captioned)
    notes = []
    if captioned and punctuated / len(captioned) < 0.2:
        notes.append(
            "Most captions have no punctuation (likely automatic captions). Merge candidates are "
            "weak; decide sentence boundaries from meaning, and add punctuation/casing."
        )
    return {
        "total_segments": len(segments),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(segments) else None,
        "issues": issues,
        "merge_candidates": candidates,
        "notes": notes,
    }


def compare_transcript(caption: str, heard: str) -> dict[str, Any]:
    """Compare a caption with independent recognition of the exact exported audio."""
    expected, actual = words(caption), words(heard)
    flags = []
    if not actual:
        flags.append("no_speech_recognized")
        return {"similarity": 0.0, "flags": flags}
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    ratio = round(matcher.ratio(), 3)
    if ratio < MISMATCH_RATIO:
        flags.append("caption_mismatch")
    opcodes = [item for item in matcher.get_opcodes() if item[0] != "equal"]
    if expected and opcodes:
        tag, i1, i2, j1, j2 = opcodes[0]
        if i1 == 0 and j1 == 0:
            if tag in {"delete", "replace"} and i2 - i1 <= 3:
                flags.append("start_may_be_clipped")
            if tag == "insert":
                flags.append("extra_speech_at_start")
        tag, i1, i2, j1, j2 = opcodes[-1]
        if i2 == len(expected) and j2 == len(actual):
            if tag in {"delete", "replace"} and i2 - i1 <= 3:
                flags.append("end_may_be_clipped")
            if tag == "insert":
                flags.append("extra_speech_at_end")
    return {"similarity": ratio, "flags": flags}
