from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from choicer_voicer_pack_creator.caption_timing_types import (
    CaptionTimingEvidence,
    CaptionTimingResult,
    TimingWord,
)
from choicer_voicer_pack_creator.captions import SOURCE_HEAD_PADDING, SOURCE_TAIL_PADDING
from choicer_voicer_pack_creator.models import SourceCaption

_PRECISION = 1e-7
_MAX_CORRECTION = 2.0
_MIN_WORD_SECONDS = 0.01
_MAX_WORD_SECONDS = 1.5
_MAX_WORD_GAP = 1.5
_MIN_PROBABILITY = 0.65
_MIN_EDGE_PROBABILITY = 0.75
_FORCED_REVIEW_PROBABILITY = 0.05
_EDGE_AGREEMENT = 0.18
_INTERIOR_AGREEMENT = 0.25
_MAX_ONSET_RECOVERY = 0.6
_MAX_LEADING_SILENCE = 1.5
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_UNSUPPORTED_MARKER = re.compile(
    r"[\[<(]\s*(?:unk|unknown|inaudible|unintelligible|music|applause|laughter|"
    r"silence|blank_audio)\s*[\]>)]",
    re.IGNORECASE,
)
_STUTTER = re.compile(
    r"""^\s*["“‘(\[{]*(?P<prefix>(?:[^\W\d_]{1,3}\s*[-–—]\s*)+)"""
    r"(?P<word>[^\W_]+(?:['’][^\W_]+)*)",
    re.UNICODE,
)


def _leading_stutter(text: str) -> tuple[str, tuple[str, ...]]:
    match = _STUTTER.match(text)
    if match:
        prefixes = tuple(_TOKEN.findall(match["prefix"].casefold()))
        word = match["word"].replace("\u2019", "'").casefold()
        if all(len(prefix) < len(word) and word.startswith(prefix) for prefix in prefixes):
            return text[:match.start("prefix")] + text[match.start("word"):], prefixes
    return text, ()


def alignment_text(text: str) -> str:
    """Normalize model input only; callers must retain the original visible caption."""
    if not isinstance(text, str):
        raise ValueError("Caption alignment text must be a string")
    text, _ = _leading_stutter(text)
    text = unicodedata.normalize("NFC", text.replace("\u2019", "'").replace("\u2018", "'"))
    words: list[str] = []
    current = ""
    for index, character in enumerate(text):
        if (
            character.isalnum() or unicodedata.category(character).startswith("M")
            or character == "'" and current and index + 1 < len(text) and text[index + 1].isalnum()
        ):
            current += character
        elif current:
            words.append(current)
            current = ""
    if current:
        words.append(current)
    return " ".join(words)


def _unspaced(character: str) -> bool:
    return (
        "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff" or "\U00020000" <= character <= "\U000323af"
        or "\u0e00" <= character <= "\u0eff" or "\u1000" <= character <= "\u109f"
        or "\u1780" <= character <= "\u17ff"
    )


def _tokens(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for word in alignment_text(text).split():
        current = ""
        for character in word.casefold().replace("'", ""):
            if unicodedata.category(character).startswith("M") and (current or result):
                if current:
                    current += character
                else:
                    result[-1] += character
            elif _unspaced(character):
                if current:
                    result.append(current)
                    current = ""
                result.append(character)
            else:
                current += character
        if current:
            result.append(current)
    return tuple(result)


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _shared_boundary(left_end: float, right_start: float) -> float:
    return left_end + (right_start - left_end) * (
        SOURCE_TAIL_PADDING / (SOURCE_HEAD_PADDING + SOURCE_TAIL_PADDING)
    )


@dataclass(frozen=True, slots=True)
class _Word:
    token: str
    original: str
    start: float
    end: float
    probability: float
    grouped: bool = False
    unit_start: bool = True
    unit_end: bool = True


@dataclass(frozen=True, slots=True)
class _Edge:
    time: float
    confidence: float
    limit: float
    trusted: bool = True


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: _Edge | None
    end: _Edge | None
    confidence: float = 1.0


def _words(
    words: Sequence[TimingWord], duration: float, check_cancel: Callable[[], None],
    *, allow_grouped: bool = False,
) -> tuple[list[_Word], str]:
    result: list[_Word] = []
    previous_end = -1.0
    for word in words:
        check_cancel()
        if not isinstance(word, TimingWord) or not isinstance(word.text, str):
            raise ValueError("Caption timing evidence must contain TimingWord values")
        if _UNSUPPORTED_MARKER.search(word.text):
            return [], "Unknown-word or non-speech marker is not lexical timing evidence"
        tokens = _tokens(word.text)
        if not tokens:
            continue  # Aligners can attach seconds of silence to standalone punctuation.
        unspaced_unit = (
            len(alignment_text(word.text).split()) == 1
            and any(_unspaced(character) for character in word.text)
        )
        if len(tokens) != 1 and not allow_grouped and not unspaced_unit:
            return [], "Evidence contains a multi-word span rather than individual word timing"
        if (
            not _number(word.start) or not _number(word.end)
            or not 0 <= word.start < word.end <= duration
            or not _number(word.probability) or not 0 <= word.probability <= 1
        ):
            return [], "Invalid word timestamps or probability in model evidence"
        if word.start < previous_end - _PRECISION:
            return [], "Overlapping or nonmonotonic word timing in model evidence"
        if word.end - word.start < _MIN_WORD_SECONDS - _PRECISION:
            return [], "Suspiciously short word span in model evidence"
        for index, token in enumerate(tokens):
            check_cancel()
            # A BPE piece can contain several unspaced characters. Keep its measured
            # envelope and boundary identity; never invent times inside that piece.
            result.append(_Word(
                token, word.text, word.start, word.end, word.probability,
                len(tokens) != 1 and not unspaced_unit,
                index == 0, index == len(tokens) - 1,
            ))
        previous_end = word.end
    return result, ""


def audit_caption_text(
    text: str,
    recognized_words: Sequence[TimingWord],
    *,
    check_cancel: Callable[[], None] = lambda: None,
) -> str:
    """Check independent recognition of the exact proposed cut without rewriting text.

    An empty reason means lexical agreement, not proof of intact phonemes or stutter
    onsets: ASR can reconstruct clipped speech. The caller must supply recognition of
    the cut itself and validate cut bounds; timestamps can be relative or absolute.
    """
    if not isinstance(text, str):
        raise ValueError("Caption audit text must be a string")
    if (
        not isinstance(recognized_words, Sequence)
        or isinstance(recognized_words, (str, bytes))
    ):
        raise ValueError("Caption audit requires a sequence of recognized TimingWord values")
    if not callable(check_cancel):
        raise ValueError("Caption audit cancellation check must be callable")
    check_cancel()
    if _UNSUPPORTED_MARKER.search(text):
        return "Caption contains an unknown-word or non-speech marker"
    expected = _tokens(text)
    if not expected:
        return "Caption has no supported lexical words to audit"
    words, problem = _words(recognized_words, math.inf, check_cancel)
    if problem:
        return problem
    if not words:
        return "No spoken words were recognized in the proposed cut"

    _, prefixes = _leading_stutter(text)
    first_lexical = 0
    if (
        prefixes and len(words) > len(prefixes)
        and tuple(word.token for word in words[:len(prefixes)]) == prefixes
        and words[len(prefixes)].token == expected[0]
    ):
        for index in range(len(prefixes)):
            check_cancel()
            if (
                words[index].end - words[index].start > 0.6
                or words[index + 1].start - words[index].end > 0.25
            ):
                return "Opening stutter words have uncertain timing in the proposed cut"
        first_lexical = len(prefixes)
    actual = tuple(word.token for word in words[first_lexical:])
    if actual != expected:
        offsets: list[int] = []
        for offset in range(len(actual) - len(expected) + 1):
            check_cancel()
            if actual[offset:offset + len(expected)] == expected:
                offsets.append(offset)
        if len(offsets) > 1:
            return "Repeated words or phrases make proposed-cut coverage ambiguous"
        if offsets:
            before = offsets[0] > 0
            after = offsets[0] + len(expected) < len(actual)
            edges = "before and after" if before and after else "before" if before else "after"
            return f"Extra words {edges} the caption suggest neighboring speech"
        expected_counts: Counter[str] = Counter()
        actual_counts: Counter[str] = Counter()
        for token in expected:
            check_cancel()
            expected_counts[token] += 1
        for token in actual:
            check_cancel()
            actual_counts[token] += 1
        for token in expected_counts.keys() | actual_counts.keys():
            check_cancel()
            if (
                expected_counts[token] != actual_counts[token]
                and max(expected_counts[token], actual_counts[token]) > 1
            ):
                return "Repeated-word count differs; proposed-cut coverage is ambiguous"
        if actual[0] != expected[0]:
            return "Opening caption word is missing or differs in the proposed cut"
        if actual[-1] != expected[-1]:
            return "Closing caption word is missing or differs in the proposed cut"
        if len(actual) < len(expected):
            return "Caption words are missing inside the proposed cut"
        if len(actual) > len(expected):
            return "Extra words inside the caption make proposed-cut coverage uncertain"
        return "Recognized caption words differ inside the proposed cut"

    for index, word in enumerate(words):
        check_cancel()
        edge = index <= first_lexical or index == len(words) - 1
        if word.probability < (_MIN_EDGE_PROBABILITY if edge else _MIN_PROBABILITY):
            return (
                "Low-confidence opening or closing word in the proposed cut" if edge
                else "Low-confidence caption word in the proposed cut"
            )
        if word.end - word.start > _MAX_WORD_SECONDS:
            return "Suspiciously long recognized word in the proposed cut"
        if index and word.start - words[index - 1].end > _MAX_WORD_GAP:
            return "Long internal pause makes proposed-cut word coverage uncertain"
    return ""


def _local_matches(
    forced: Sequence[_Word], recognized: Sequence[_Word], check_cancel: Callable[[], None],
    *, opening: bool = True,
) -> list[int]:
    matches: list[int] = []
    for offset in range(len(recognized) - len(forced) + 1):
        check_cancel()
        first = recognized[offset]
        if (
            first.token != forced[0].token
            or first.end < forced[0].start - _INTERIOR_AGREEMENT
            or first.start > forced[0].end + _INTERIOR_AGREEMENT
        ):
            continue
        for index, word in enumerate(forced):
            check_cancel()
            other = recognized[offset + index]
            # Exact, contiguous lexical coverage and local timing anchors, not a global
            # fuzzy search that can latch onto a repeated phrase elsewhere in the context.
            if (
                word.token != other.token
                or abs(word.end - other.end) > _INTERIOR_AGREEMENT
                or ((index or not opening)
                    and abs(word.start - other.start) > _INTERIOR_AGREEMENT)
                or (not index and opening and (
                    word.start - other.start > _MAX_ONSET_RECOVERY
                    or other.start - word.start > _MAX_LEADING_SILENCE
                ))
            ):
                break
        else:
            matches.append(offset)
    return matches


def _run_problem(
    forced: Sequence[_Word],
    recognized: Sequence[_Word],
    *,
    opening: bool,
    closing: bool,
    check_cancel: Callable[[], None],
) -> str:
    for index, (word, other) in enumerate(zip(forced, recognized, strict=True)):
        check_cancel()
        first = opening and index == 0
        last = closing and index == len(forced) - 1
        if word.grouped:
            return "Multi-word forced span does not establish individual word timing"
        if (
            first and (not word.unit_start or not other.unit_start)
            or last and (not word.unit_end or not other.unit_end)
        ):
            return "Caption boundary falls inside an indivisible model token piece"
        if other.probability < (
            _MIN_EDGE_PROBABILITY if first or last else _MIN_PROBABILITY
        ):
            return "Low-confidence opening/closing word" if first or last else "Low-confidence word"
        if other.end - other.start > _MAX_WORD_SECONDS:
            return "Suspiciously long recognized word span"
        tolerance = _EDGE_AGREEMENT if first or last else _INTERIOR_AGREEMENT
        if abs(word.end - other.end) > tolerance:
            return "Forced and independently recognized word endings disagree"
        onset_difference = other.start - word.start
        if first and abs(onset_difference) > tolerance:
            if len(forced) < 2 or abs(word.end - other.end) > 0.12:
                return "Opening word onset lacks a reliable neighboring anchor"
        elif abs(onset_difference) > tolerance:
            return "Forced and independently recognized word onsets disagree"
        if word.end - word.start > _MAX_WORD_SECONDS and not (
            first and onset_difference > tolerance
            and word.end - word.start <= _MAX_WORD_SECONDS + _MAX_LEADING_SILENCE
        ):
            return "Suspiciously long forced word span"
        if index and (
            word.start - forced[index - 1].end > _MAX_WORD_GAP
            or other.start - recognized[index - 1].end > _MAX_WORD_GAP
        ):
            return "Long internal pause makes the word sequence uncertain"
    return ""


def _edge_match(
    expected: Sequence[str],
    forced: Sequence[_Word],
    recognized: Sequence[_Word],
    *,
    opening: bool,
    check_cancel: Callable[[], None],
) -> tuple[int | None, str]:
    count = min(2, len(expected))
    run = forced[:count] if opening else forced[-count:]
    tokens = expected[:count] if opening else expected[-count:]
    if len(run) != count or tuple(word.token for word in run) != tuple(tokens):
        return None, "Caption edge is not covered by exact forced words"
    at_first = opening or len(expected) == count
    matches = _local_matches(run, recognized, check_cancel, opening=at_first)
    if not matches:
        return None, "Caption edge lacks locally corroborated neighboring words"
    if len(matches) != 1:
        return None, "Repeated words have ambiguous local edge timing"
    offset = matches[0]
    problem = _run_problem(
        run, recognized[offset:offset + count], opening=at_first,
        closing=not opening or len(expected) == count, check_cancel=check_cancel,
    )
    return (None, problem) if problem else (offset if opening else offset + count - 1, "")


def _candidate(
    cue: SourceCaption,
    evidence: CaptionTimingEvidence,
    duration: float,
    check_cancel: Callable[[], None],
) -> tuple[_Candidate | None, str]:
    if evidence.problem:
        return None, evidence.problem
    if _UNSUPPORTED_MARKER.search(cue.text):
        return None, "Caption contains an unknown-word or non-speech marker"
    expected = _tokens(cue.text)
    if not expected:
        return None, "Caption has no supported lexical words to align"
    forced, problem = _words(evidence.words, duration, check_cancel, allow_grouped=True)
    if problem:
        return None, problem
    recognized, problem = _words(evidence.recognized_words, duration, check_cancel)
    if problem:
        return None, problem
    if not recognized:
        return None, "No independent recognized words corroborate the caption"
    reasons: list[str] = []
    full_offset: int | None = None
    if tuple(word.token for word in forced) != expected:
        reasons.append("Forced word evidence does not cover the exact caption text")
    else:
        matches = _local_matches(forced, recognized, check_cancel)
        if not matches:
            reasons.append("Independent recognition does not corroborate every caption word locally")
        elif len(matches) != 1:
            reasons.append("Repeated words or phrases have ambiguous local timing")
        else:
            full_offset = matches[0]
            problem = _run_problem(
                forced, recognized[full_offset:full_offset + len(forced)],
                opening=True, closing=True, check_cancel=check_cancel,
            )
            if problem:
                reasons.append(problem)
    start_index, start_problem = _edge_match(
        expected, forced, recognized, opening=True, check_cancel=check_cancel,
    )
    end_index, end_problem = _edge_match(
        expected, forced, recognized, opening=False, check_cancel=check_cancel,
    )
    # A full, unique run can disambiguate repeated words that two-word edge anchors
    # alone cannot. Conditional forced-token scores are not independent ASR confidence.
    if full_offset is not None and not reasons:
        start_index, end_index = full_offset, full_offset + len(forced) - 1
        start_problem = end_problem = ""
    for word in forced:
        check_cancel()
        if word.probability <= _FORCED_REVIEW_PROBABILITY:
            reasons.append("Very low forced lexical likelihood; word coverage needs review")
            break

    start: _Edge | None = None
    end: _Edge | None = None
    if start_index is not None:
        word = recognized[start_index]
        start = _Edge(
            word.start, word.probability,
            recognized[start_index - 1].end if start_index else 0.0,
        )
    if end_index is not None:
        word = recognized[end_index]
        end = _Edge(
            word.end, word.probability,
            recognized[end_index + 1].start if end_index + 1 < len(recognized) else duration,
        )
    _, stutters = _leading_stutter(cue.text)
    if stutters and start is not None and start_index is not None:
        _, recognized_stutters = _leading_stutter(recognized[start_index].original)
        if recognized_stutters != stutters:
            before = start_index - 1
            onset = start.time
            confidence = start.confidence
            measured = True
            for prefix in reversed(stutters):
                check_cancel()
                if before < 0:
                    measured = False
                    break
                word = recognized[before]
                if (
                    word.token != prefix or word.probability < _MIN_EDGE_PROBABILITY
                    or word.end - word.start > 0.6
                    or not -_PRECISION <= onset - word.end <= 0.25
                ):
                    measured = False
                    break
                onset = word.start
                confidence = min(confidence, word.probability)
                before -= 1
            if measured:
                start = replace(
                    start, time=onset, confidence=confidence,
                    limit=recognized[before].end if before >= 0 else 0.0,
                )
            else:
                reasons.append("Stutter onset is not independently timed; earlier opening retained")
                start = replace(
                    start,
                    time=min(cue.start, max(start.limit, start.time - SOURCE_HEAD_PADDING)),
                    limit=min(cue.start, start.limit), trusted=False,
                )
    if start is None:
        reasons.append(f"Opening unchanged: {start_problem}")
    elif abs(start.time - cue.start) > _MAX_CORRECTION:
        reasons.append("Opening correction exceeds the 2-second safety limit")
        start = None
    if end is None:
        reasons.append(f"Closing unchanged: {end_problem}")
    elif abs(end.time - cue.end) > _MAX_CORRECTION:
        reasons.append("Closing correction exceeds the 2-second safety limit")
        end = None
    confidence = 1.0
    if full_offset is not None:
        for word in recognized[full_offset:full_offset + len(forced)]:
            check_cancel()
            confidence = min(confidence, word.probability)
    return (
        _Candidate(start, end, confidence) if start or end else None,
        "; ".join(dict.fromkeys(reasons)),
    )


def correct_caption_timings(
    captions: Sequence[SourceCaption],
    evidence: Sequence[CaptionTimingEvidence],
    duration: float,
    *,
    check_cancel: Callable[[], None] = lambda: None,
) -> CaptionTimingResult:
    """Create a caption-preserving draft from corroborated local word evidence.

    Malformed requests raise ValueError. Supported edges can move independently; an
    uncertain opening never trims the original start to omit an unmeasured stutter.
    Partial corrections stay flagged. Scores are ASR confidence, not cut correctness.
    """
    if not callable(check_cancel):
        raise ValueError("Caption timing cancellation check must be callable")
    check_cancel()
    if not _number(duration) or duration <= 0:
        raise ValueError("Caption timing requires a finite, positive source duration")
    if (
        not isinstance(captions, Sequence) or isinstance(captions, (str, bytes))
        or not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes))
    ):
        raise ValueError("Caption timing requires caption and evidence sequences")
    originals: list[tuple[float, float]] = []
    for cue in captions:
        check_cancel()
        if not isinstance(cue, SourceCaption):
            raise ValueError("Caption timing requires SourceCaption values")
        if (
            not _number(cue.start) or not _number(cue.end)
            or not 0 <= cue.start < cue.end <= duration
        ):
            raise ValueError("Caption timing received an invalid caption time range")
        if not isinstance(cue.text, str) or not isinstance(cue.source, str):
            raise ValueError("Caption text and source must be strings")
        originals.append((cue.start, cue.end))
    indexed: dict[int, CaptionTimingEvidence] = {}
    for item in evidence:
        check_cancel()
        if not isinstance(item, CaptionTimingEvidence):
            raise ValueError("Caption timing requires CaptionTimingEvidence values")
        if (
            isinstance(item.index, bool) or not isinstance(item.index, int)
            or not 0 <= item.index < len(captions) or item.index in indexed
        ):
            raise ValueError("Caption timing evidence has a duplicate or invalid caption index")
        if (
            not isinstance(item.problem, str)
            or not isinstance(item.words, Sequence)
            or not isinstance(item.recognized_words, Sequence)
        ):
            raise ValueError("Caption timing evidence has invalid fields")
        indexed[item.index] = item
    candidates: list[_Candidate | None] = []
    reasons: list[str] = []
    for index, cue in enumerate(captions):
        check_cancel()
        item = indexed.get(index)
        candidate, reason = (
            _candidate(cue, item, duration, check_cancel)
            if item is not None else (None, "Missing word timing evidence")
        )
        candidates.append(candidate)
        reasons.append(reason)

    ordered = sorted(range(len(captions)), key=lambda index: (*originals[index], index))
    ranges = [
        (
            candidate.start.time if candidate and candidate.start else original[0],
            candidate.end.time if candidate and candidate.end else original[1],
        )
        for candidate, original in zip(candidates, originals, strict=True)
    ]

    def retain_edge(index: int, edge: str, reason: str) -> bool:
        candidate = candidates[index]
        if candidate is None or getattr(candidate, edge) is None:
            return False
        candidate = replace(candidate, **{edge: None})
        candidates[index] = candidate if candidate.start or candidate.end else None
        ranges[index] = (
            candidate.start.time if candidate.start else originals[index][0],
            candidate.end.time if candidate.end else originals[index][1],
        )
        if reason not in reasons[index]:
            reasons[index] = f"{reasons[index]}; {reason}".strip("; ")
        return True

    # Resolve only conflicting facing edges. An uncertain opening must not freeze a
    # corroborated closing, or prevent the next caption from recovering its first word.
    changed = True
    while changed:
        check_cancel()
        changed = False
        active: list[int] = []
        for index in ordered:
            check_cancel()
            if ranges[index][0] >= ranges[index][1]:
                reason = "Supported word edge conflicts with the retained opposite edge"
                for edge in ("start", "end"):
                    check_cancel()
                    changed = retain_edge(index, edge, reason) or changed
            # Retain every potentially conflicting predecessor, not just the longest
            # envelope: nested captions can conflict with one another inside it.
            active = [
                earlier for earlier in active
                if ranges[earlier][1] > originals[index][0] - _MAX_CORRECTION - _PRECISION
            ]
            for earlier in active:
                check_cancel()
                if ranges[index][0] >= ranges[earlier][1] - _PRECISION:
                    continue
                left = candidates[earlier]
                right = candidates[index]
                conflict = (
                    "Neighboring word evidence overlaps; facing boundaries retained"
                    if left and left.end and right and right.start else
                    "An uncertain neighboring caption prevents a safe shared boundary"
                )
                for affected, edge in ((earlier, "end"), (index, "start")):
                    check_cancel()
                    changed = retain_edge(affected, edge, conflict) or changed
            active.append(index)

    padded = list(ranges)
    previous: int | None = None
    for position, index in enumerate(ordered):
        check_cancel()
        candidate = candidates[index]
        if candidate is None:
            if previous is None or ranges[index][1] > ranges[previous][1]:
                previous = index
            continue
        start, end = ranges[index]
        padded_start, padded_end = start, end
        if candidate.start:
            lower = candidate.start.limit
            if previous is not None:
                other = candidates[previous]
                lower = max(lower, (
                    _shared_boundary(ranges[previous][1], start)
                    if other and other.end and candidate.start.trusted else ranges[previous][1]
                ))
            padded_start = max(
                0.0, lower, start - SOURCE_HEAD_PADDING if candidate.start.trusted else start,
            )
        if candidate.end:
            upper = candidate.end.limit
            if position + 1 < len(ordered):
                following = ordered[position + 1]
                other = candidates[following]
                upper = min(upper, (
                    _shared_boundary(end, ranges[following][0])
                    if other and other.start and other.start.trusted else ranges[following][0]
                ))
            padded_end = min(duration, upper, end + SOURCE_TAIL_PADDING)
        padded[index] = (padded_start, padded_end)
        if (
            not 0 <= padded_start < padded_end <= duration
            or padded_start > start + _PRECISION or padded_end < end - _PRECISION
        ):
            raise ValueError("Caption source handles would truncate a supported word boundary")
        if previous is None or end > ranges[previous][1]:
            previous = index

    rows: list[SourceCaption] = []
    confidences: list[float | None] = []
    for index, (cue, candidate) in enumerate(zip(captions, candidates, strict=True)):
        check_cancel()
        label = (
            f"timing review needed: {reasons[index]}"
            + ("; partial model/audio edge correction" if candidate else "")
            if reasons[index] else
            "model/audio aligned: locally corroborated words; "
            "source handles up to 0.15s before / 0.25s after"
        )
        rows.append(SourceCaption(
            *padded[index], cue.text, f"{cue.source} - {label}", cue.fragments,
        ))
        confidence = candidate.confidence if candidate else None
        if candidate:
            for edge in (candidate.start, candidate.end):
                check_cancel()
                if edge:
                    confidence = min(confidence, edge.confidence)
        confidences.append(confidence)
    return CaptionTimingResult(tuple(rows), tuple(confidences), tuple(reasons))
