from __future__ import annotations

import html
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from choicer_voicer_pack_creator.models import CaptionFragment, SourceCaption


def _fragment_start(value: Any, event_start: float, event_end: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        offset = float(value) / 1000
    except (TypeError, ValueError, OverflowError):
        return None
    start = event_start + offset
    if not math.isfinite(start) or offset < 0 or not 0 <= start < event_end:
        return None
    return start


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
        fragments: list[CaptionFragment] = []
        last_start: float | None = None
        for index, segment in enumerate(segments):
            # Only the first segment has an implicit zero offset in JSON3.
            fragment_start = _fragment_start(
                segment.get("tOffsetMs", 0 if index == 0 else None), start, end
            )
            fragment_text = html.unescape(str(segment.get("utf8", "")))
            if fragment_text.strip() and fragment_start is not None:
                if last_start is not None and fragment_start <= last_start:
                    fragment_start = None
                else:
                    last_start = fragment_start
            fragments.append(CaptionFragment(fragment_text, fragment_start))
        start = max(0.0, start)
        if end - start < 0.05:
            continue
        window = event.get("wWinId")
        if event.get("aAppend") and cues and windows[-1] == window:
            previous = cues[-1]
            fragments[0] = CaptionFragment(" " + fragments[0].text, fragments[0].start)
            cues[-1] = SourceCaption(
                previous.start, max(previous.end, end), f"{previous.text} {text}", source,
                (*previous.fragments, *fragments),
            )
        else:
            cues.append(SourceCaption(start, end, text, source, tuple(fragments)))
            windows.append(window)
    cues.sort(key=lambda cue: (cue.start, cue.end))
    if automatic:
        # Auto-caption windows linger on screen; don't mistake that for overlapping speech.
        cues = [
            SourceCaption(
                cue.start, min(cue.end, following.start), cue.text, cue.source, cue.fragments
            )
            if following and cue.start < following.start < cue.end
            else cue
            for cue, following in zip(cues, [*cues[1:], None], strict=True)
        ]
    return [cue for cue in cues if cue.end - cue.start >= 0.05]


SOURCE_HEAD_PADDING = 0.15
SOURCE_TAIL_PADDING = 0.25
_ONSET_TOLERANCE = 0.12
_MAX_JOIN_SECONDS = 6.0
_MAX_JOIN_CHARACTERS = 120


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _label(source: str, reason: str) -> str:
    return (
        f"{source} - {reason} - source audio handles up to "
        f"{SOURCE_HEAD_PADDING:.2f}s before / {SOURCE_TAIL_PADDING:.2f}s after "
        "- volume-based; music/effects may mask pauses"
    )


def pad_source_ranges(
    ranges: Sequence[tuple[float, float]],
    duration: float,
    *,
    check_cancel: Callable[[], None] = lambda: None,
) -> list[tuple[float, float]]:
    """Include real source audio, sharing short gaps without adding overlaps."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source audio handles require a finite, positive duration")
    for start, end in ranges:
        check_cancel()
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
            raise ValueError("Source audio handles received an invalid time range")
    ordered = sorted(range(len(ranges)), key=lambda i: ranges[i])
    overlapping: set[int] = set()
    furthest_end = -1.0
    furthest_index = -1
    for index in ordered:
        check_cancel()
        start, end = ranges[index]
        if start < furthest_end:
            overlapping.update((index, furthest_index))
        if end > furthest_end:
            furthest_end, furthest_index = end, index

    result = list(ranges)
    previous_end = 0.0
    for position, index in enumerate(ordered):
        check_cancel()
        start, end = ranges[index]
        if index not in overlapping:
            lower = (previous_end + start) / 2 if position else 0.0
            upper = (
                (end + ranges[ordered[position + 1]][0]) / 2
                if position + 1 < len(ordered) else duration
            )
            result[index] = (
                max(lower, start - SOURCE_HEAD_PADDING),
                min(upper, end + SOURCE_TAIL_PADDING),
            )
        previous_end = max(previous_end, end)
    return result


@dataclass(frozen=True, slots=True)
class _AudioEvidence:
    spans: list[tuple[float, float]]
    starts: list[float]
    ends: list[float]

    def at_onset(self, timestamp: float) -> int | None:
        index = bisect_right(self.starts, timestamp) - 1
        if index >= 0 and timestamp < self.ends[index]:
            return index
        following = index + 1
        if following < len(self.spans) and self.starts[following] - timestamp <= _ONSET_TOLERANCE:
            return following
        return None


def _audio_evidence(
    spans: Sequence[tuple[float, float]], duration: float, check_cancel: Callable[[], None]
) -> _AudioEvidence:
    ordered: list[tuple[float, float]] = []
    for start, end in spans:
        check_cancel()
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Caption refinement received an invalid audio activity range")
        if start < duration:
            ordered.append((start, min(duration, end)))
    ordered.sort()
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        check_cancel()
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return _AudioEvidence(merged, [start for start, _ in merged], [end for _, end in merged])


def _spoken_fragments(
    cue: SourceCaption, check_cancel: Callable[[], None]
) -> list[tuple[int, CaptionFragment]]:
    spoken: list[tuple[int, CaptionFragment]] = []
    previous = -1.0
    for index, fragment in enumerate(cue.fragments):
        check_cancel()
        # Standalone punctuation stays in the text, but cannot mark a spoken onset.
        if not fragment.text.strip().strip(".,!?;:\u3002\uff01\uff1f\u2026"):
            continue
        start = fragment.start
        if (
            start is None or not math.isfinite(start)
            or not cue.start <= start < cue.end or start <= previous
        ):
            return []
        spoken.append((index, fragment))
        previous = start
    if _normalize_text("".join(fragment.text for fragment in cue.fragments)) != cue.text:
        return []
    return spoken


def _short_token(text: str) -> bool:
    return len(text.split()) == 1 and len(text.strip()) <= 16


def _join_separator(left: str, right: str) -> str:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    # Display rows in languages without word spaces must not acquire invented word breaks.
    def unspaced(character: str) -> bool:
        return (
            "\u3040" <= character <= "\u30ff"
            or "\u3000" <= character <= "\u303f"
            or "\u3400" <= character <= "\u9fff"
            or "\uf900" <= character <= "\ufaff"
            or "\U00020000" <= character <= "\U000323af"
            or "\u0e00" <= character <= "\u0eff"
            or "\u1000" <= character <= "\u109f"
            or "\u1780" <= character <= "\u17ff"
        )

    return "" if unspaced(left[-1]) or unspaced(right[0]) else " "


def refine_captions(
    captions: Sequence[SourceCaption],
    audio_spans: Sequence[tuple[float, float]] | None,
    duration: float,
    *,
    pause_threshold: float = 0.4,
    check_cancel: Callable[[], None] = lambda: None,
) -> list[SourceCaption]:
    """Make a separate, conservative draft; activity is volume evidence, not word alignment."""
    check_cancel()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Caption refinement requires a finite, positive video duration")
    if (
        isinstance(pause_threshold, bool) or not math.isfinite(pause_threshold)
        or not 0.2 <= pause_threshold <= 1.0
    ):
        raise ValueError("Caption pause threshold must be between 0.2 and 1.0 seconds")
    evidence = _audio_evidence(audio_spans or (), duration, check_cancel)
    bounded: list[SourceCaption] = []
    for cue in captions:
        check_cancel()
        if (
            not math.isfinite(cue.start) or not math.isfinite(cue.end)
            or not 0 <= cue.start < duration or cue.end <= cue.start
        ):
            raise ValueError("Caption refinement received an invalid caption time range")
        bounded.append(SourceCaption(
            cue.start, min(duration, cue.end), cue.text, cue.source, cue.fragments
        ))

    overlapping: set[int] = set()
    furthest_end = -1.0
    furthest_index = -1
    for index in sorted(range(len(bounded)), key=lambda i: bounded[i].start):
        check_cancel()
        cue = bounded[index]
        if cue.start < furthest_end:
            overlapping.update((index, furthest_index))
        if cue.end > furthest_end:
            furthest_end, furthest_index = cue.end, index

    spoken_rows = [_spoken_fragments(cue, check_cancel) for cue in bounded]
    # Join only adjacent, short, incomplete display rows with a shared activity span.
    groups: list[tuple[SourceCaption, list[tuple[int, CaptionFragment]], bool, str | None]] = []
    for index, (cue, spoken) in enumerate(zip(bounded, spoken_rows, strict=True)):
        check_cancel()
        reason = None
        if index in overlapping:
            reason = "unsplit: overlapping caption windows"
        elif not spoken or (len(spoken) == 1 and not _short_token(cue.text)):
            reason = "unsplit: limited fragment timing"
        elif not evidence.spans:
            reason = "unsplit: no reliable audio activity"
        else:
            for _, fragment in spoken:
                check_cancel()
                assert fragment.start is not None
                if evidence.at_onset(fragment.start) is None:
                    reason = "unsplit: fragment timing not supported by audio"
                    break
        if groups and reason is None and groups[-1][3] is None:
            previous, previous_spoken, _, _ = groups[-1]
            left_start = previous_spoken[-1][1].start
            right_start = spoken[0][1].start
            assert left_start is not None and right_start is not None
            left_audio = evidence.at_onset(left_start)
            right_audio = evidence.at_onset(right_start)
            sentence_end = previous.text.rstrip("\"'\u201d\u2019)]}").endswith(
                (".", "!", "?", "\u3002", "\uff01", "\uff1f", ";", ":", "\u2026")
            )
            if (
                not sentence_end and 0 <= cue.start - previous.end <= 0.20
                and cue.source == previous.source
                and 0 < right_start - left_start <= 1.25
                and cue.end - previous.start <= _MAX_JOIN_SECONDS
                and len(previous.text) + len(cue.text) + 1 <= _MAX_JOIN_CHARACTERS
                and left_audio is not None and left_audio == right_audio
            ):
                separator = _join_separator(
                    previous.fragments[-1].text, cue.fragments[0].text
                )
                fragments = (
                    *previous.fragments,
                    CaptionFragment(separator + cue.fragments[0].text, cue.fragments[0].start),
                    *cue.fragments[1:],
                )
                joined = SourceCaption(
                    previous.start, cue.end,
                    _normalize_text("".join(fragment.text for fragment in fragments)),
                    previous.source, fragments,
                )
                groups[-1] = (joined, _spoken_fragments(joined, check_cancel), True, None)
                continue
        groups.append((cue, spoken, False, reason))

    result: list[SourceCaption] = []
    for cue, spoken, joined, reason in groups:
        check_cancel()
        if reason:
            result.append(SourceCaption(
                cue.start, cue.end, cue.text, _label(cue.source, reason), cue.fragments
            ))
            continue
        boundaries: list[tuple[int, float, float]] = []
        for (_, left), (fragment_index, right) in pairwise(spoken):
            check_cancel()
            assert left.start is not None and right.start is not None
            right_audio = evidence.at_onset(right.start)
            if right_audio is None or right_audio == 0 or evidence.at_onset(left.start) is None:
                continue
            gap_start = evidence.ends[right_audio - 1]
            gap_end = evidence.starts[right_audio]
            # Word starts alone never prove a pause. Require the measured gap immediately
            # before the right fragment's onset, with activity on both sides.
            if (
                gap_end - gap_start >= pause_threshold - 1e-9
                and left.start < gap_start
                and abs(right.start - gap_end) <= _ONSET_TOLERANCE
                and cue.start < gap_start < gap_end < cue.end
            ):
                right_edge = min(right.start, gap_end)
                boundaries.append((
                    fragment_index, min(gap_start, right_edge), right_edge,
                ))

        # Only bound outer edges when all timed fragments are represented in the audio.
        # A final multi-word block has no final word start: retain its display tail.
        start, end = cue.start, cue.end
        if len(spoken) >= 2:
            first_start = spoken[0][1].start
            last_start = spoken[-1][1].start
            assert first_start is not None and last_start is not None
            first_audio = bisect_right(evidence.ends, cue.start)
            final_audio = bisect_left(evidence.starts, cue.end) - 1
            if first_audio <= final_audio:
                first_edge = evidence.starts[first_audio]
                final_edge = evidence.ends[final_audio]
                if first_edge <= first_start + _ONSET_TOLERANCE:
                    start = min(first_start, max(cue.start, first_edge))
                if _short_token(spoken[-1][1].text) and final_edge >= last_start:
                    end = min(cue.end, final_edge)

        fragment_start = 0
        row_start = start
        changed = joined or bool(boundaries) or start != cue.start or end != cue.end
        description = (
            "pause split" if boundaries else
            "display rows joined" if joined else
            "audio edges" if changed else "unsplit: no safe internal pause"
        )
        for fragment_end, row_end, following_start in [
            *boundaries, (len(cue.fragments), end, end)
        ]:
            check_cancel()
            fragments = cue.fragments[fragment_start:fragment_end]
            text = _normalize_text("".join(fragment.text for fragment in fragments))
            result.append(SourceCaption(
                row_start, row_end, text, _label(cue.source, description), tuple(fragments)
            ))
            fragment_start, row_start = fragment_end, following_start
    padded = pad_source_ranges(
        [(cue.start, cue.end) for cue in result], duration, check_cancel=check_cancel,
    )
    return [
        SourceCaption(start, end, cue.text, cue.source, cue.fragments)
        for cue, (start, end) in zip(result, padded, strict=True)
    ]
