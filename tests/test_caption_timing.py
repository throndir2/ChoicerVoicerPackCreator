from __future__ import annotations

import math
import random
from dataclasses import replace

import pytest

from choicer_voicer_pack_creator.caption_timing import (
    alignment_text,
    audit_caption_text,
    correct_caption_timings,
)
from choicer_voicer_pack_creator.caption_timing_types import CaptionTimingEvidence, TimingWord
from choicer_voicer_pack_creator.models import CaptionFragment, SourceCaption


def _cue(text: str = "Bright little lantern", start: float = 1, end: float = 3) -> SourceCaption:
    return SourceCaption(
        start, end, text, "YouTube creator (en)", (CaptionFragment(text, start),),
    )


def _words(
    text: str = "Bright little lantern", start: float = 1.2, step: float = 0.3,
) -> tuple[TimingWord, ...]:
    return tuple(
        TimingWord(word, start + index * step, start + (index + 1) * step, 0.96)
        for index, word in enumerate(text.split())
    )


def _evidence(
    words: tuple[TimingWord, ...] | None = None,
    recognized: tuple[TimingWord, ...] | None = None,
    index: int = 0,
) -> CaptionTimingEvidence:
    words = _words() if words is None else words
    return CaptionTimingEvidence(index, words, words if recognized is None else recognized)


def _ranges(result):
    return [(cue.start, cue.end) for cue in result.captions]


def _assert_review(cue, evidence, reason: str = "", *, bounds=None):
    original = cue.to_dict()
    result = correct_caption_timings([cue], [evidence], 10)
    assert _ranges(result) == [
        pytest.approx(bounds) if bounds is not None else (cue.start, cue.end),
    ]
    assert result.captions[0].text == cue.text
    assert result.captions[0].fragments is cue.fragments
    assert result.review_reasons[0]
    assert reason.lower() in result.review_reasons[0].lower()
    assert "timing review needed" in result.captions[0].source
    assert "model/audio aligned" not in result.captions[0].source
    if bounds is None:
        assert result.confidences == (None,)
    else:
        confidence = result.confidences[0]
        assert confidence is not None and math.isfinite(confidence) and 0 <= confidence <= 1
    assert cue.to_dict() == original


@pytest.mark.parametrize(("text", "expected"), [
    ("  Bright,\n little lantern! ", "Bright little lantern"),
    ("L\u2014Lady", "Lady"),
    ("I\u2014I'm ready.", "I'm ready"),
    ("Y\u2014Yes", "Yes"),
    ("L-L-Lady", "Lady"),
    ("Th-the light", "the light"),
    ("“I\u2014I’m ready.”", "I'm ready"),
    ("No\u2014no", "No no"),
    ("I - like it", "I like it"),
    ("red-hot", "red hot"),
    ("sun-lit", "sun lit"),
    ("... ! ? \u2014", ""),
    ("你好，世界！", "你好 世界"),
])
def test_alignment_normalization_is_only_for_model_input(text, expected):
    assert alignment_text(text) == expected


def test_normalization_rejects_nontext():
    with pytest.raises(ValueError, match="string"):
        alignment_text(None)


@pytest.mark.parametrize("third_start", [2.0, 2.1, 2.5])
def test_nested_caption_conflicts_never_truncate_supported_words_or_make_empty_rows(third_start):
    captions = [
        _cue("Outer uncertain passage", 0, 8),
        _cue("Bright lantern", 2, 4),
        _cue("Other uncertain passage", third_start, 5),
    ]
    words = _words("Bright lantern", start=2.2)
    result = correct_caption_timings(captions, [_evidence(words, index=1)], 10)
    assert _ranges(result)[1] == (2, 4)
    assert result.review_reasons[1]
    assert all(0 <= row.start < row.end <= 10 for row in result.captions)


@pytest.mark.parametrize("pieces", [
    ("\u4f60", "\u597d", "\u4e16", "\u754c"),
    ("\u4f60\u597d", "\u4e16\u754c"),
    ("\u3053", "\u3093", "\u306b", "\u3061", "\u306f"),
    ("\u0e2a", "\u0e27\u0e31", "\u0e2a", "\u0e14\u0e35"),
])
def test_unspaced_text_matches_timed_unicode_pieces_without_invented_offsets(pieces):
    text = "".join(pieces)
    words = _words(" ".join(pieces), start=1.2, step=0.2)
    cue = _cue(text)
    result = correct_caption_timings([cue], [_evidence(words)], 10)
    assert result.review_reasons == ("",)
    assert _ranges(result) == [pytest.approx((1.05, words[-1].end + 0.25))]
    assert result.captions[0].text == text
    assert result.captions[0].fragments is cue.fragments
    assert audit_caption_text(text, words) == ""
    assert alignment_text(text) == text


def test_unspaced_caption_edge_cannot_cut_inside_an_indivisible_recognized_piece():
    cue = _cue("\u597d\u4e16", 1.1, 1.8)
    forced = (
        TimingWord("\u597d", 1.1, 1.4, 0.99),
        TimingWord("\u4e16", 1.4, 1.8, 0.99),
    )
    recognized = (
        TimingWord("\u4f60\u597d", 1.1, 1.4, 0.99),
        TimingWord("\u4e16\u754c", 1.4, 1.8, 0.99),
    )
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert result.review_reasons[0]
    assert "indivisible" in result.review_reasons[0]
    assert _ranges(result) == [(1.1, 1.8)]
    assert audit_caption_text(cue.text, recognized)


def test_alignment_preserves_combining_marks_and_normalizes_canonical_equivalents():
    assert alignment_text("Cafe\u0301") == "Caf\u00e9"
    assert audit_caption_text("Cafe\u0301", _words("Caf\u00e9")) == ""


@pytest.mark.parametrize(("text", "recognized"), [
    (" Bright,\n little lantern! ", "bright little lantern"),
    ("I’m ready.", "I'm ready"),
    ("L\u2014Lady waits", "Lady waits"),
    ("I\u2014I'm ready", "I-I'm ready"),
    ("Y\u2014Yes", "Yes"),
    ("Go go home", "Go go home"),
    ("Go now go", "Go now go"),
])
def test_cut_audit_accepts_exact_normalized_lexical_coverage_only(text, recognized):
    words = _words(recognized)
    original = tuple(words)
    assert audit_caption_text(text, words) == ""
    assert words == original


def test_cut_audit_ignores_artificial_punctuation_spans_not_extra_lexical_words():
    words = (TimingWord("...", 0, 1.2, 0.01), *_words(), TimingWord("!", 2.1, 9, 0.01))
    assert audit_caption_text("Bright little lantern!", words) == ""


@pytest.mark.parametrize("text", ["", "... !", "[unk]", "[Music]"])
def test_cut_audit_cannot_certify_nonlexical_captions(text):
    assert audit_caption_text(text, ())


@pytest.mark.parametrize("words", [(), (TimingWord("...", 0, 1, 0.99),)])
def test_cut_audit_reports_empty_recognition(words):
    assert "No spoken words" in audit_caption_text("Bright little lantern", words)


@pytest.mark.parametrize(("recognized", "reason"), [
    ("little lantern", "Opening"),
    ("Bright little", "Closing"),
    ("Bright lantern", "missing inside"),
    ("Bright tiny lantern", "differ inside"),
    ("Bright very little lantern", "Extra words inside"),
    ("Other Bright little lantern", "before"),
    ("Bright little lantern Next", "after"),
    ("Other Bright little lantern Next", "before and after"),
])
def test_cut_audit_reports_partial_different_and_spilled_lexical_content(recognized, reason):
    assert reason in audit_caption_text("Bright little lantern", _words(recognized))


def test_cut_audit_never_hides_a_missing_opening_word_in_high_overall_coverage():
    text = "We watched each tiny lantern float above the quiet harbor as dawn approached"
    recognized = _words(" ".join(text.split()[1:]), step=0.2)
    assert "Opening" in audit_caption_text(text, recognized)


@pytest.mark.parametrize(("text", "recognized"), [
    ("Go", "Go go"),
    ("Go go home", "Go home"),
    ("Go now go", "Go now"),
    ("Light shines", "Light shines Light shines"),
])
def test_cut_audit_does_not_pick_a_convenient_repeated_word_occurrence(text, recognized):
    assert "ambiguous" in audit_caption_text(text, _words(recognized))


@pytest.mark.parametrize("index", [0, 1, 2])
def test_cut_audit_low_confidence_names_are_reviewed_never_replaced(index):
    text = "Arden greets Rowan"
    words = tuple(replace(word, probability=0.64) if i == index else word
                  for i, word in enumerate(_words(text)))
    assert "Low-confidence" in audit_caption_text(text, words)
    assert [word.text for word in words] == text.split()
    assert text == "Arden greets Rowan"


def test_cut_audit_uses_reliable_edge_words_even_when_interior_threshold_passes():
    words = tuple(replace(word, probability=0.65) if index == 1 else word
                  for index, word in enumerate(_words()))
    assert audit_caption_text("Bright little lantern", words) == ""
    assert "Low-confidence opening" in audit_caption_text(
        "Bright little lantern", (replace(words[0], probability=0.74), *words[1:]),
    )


def test_cut_audit_handles_separately_recognized_stutters_without_ignoring_other_words():
    words = _words("L Lady waits", step=0.15)
    assert audit_caption_text("L\u2014Lady waits", words) == ""
    assert "Extra words before" in audit_caption_text("Lady waits", words)
    assert "Low-confidence opening" in audit_caption_text(
        "L\u2014Lady waits", (replace(words[0], probability=0.2), *words[1:]),
    )
    assert "uncertain timing" in audit_caption_text(
        "L\u2014Lady waits",
        (replace(words[0], start=0.2), *words[1:]),
    )


@pytest.mark.parametrize(("field", "value"), [
    ("start", -1), ("start", float("nan")), ("start", float("inf")),
    ("end", 1.2), ("end", float("inf")), ("probability", float("nan")),
    ("probability", 1.01),
])
def test_cut_audit_rejects_invalid_word_evidence_with_review_reason(field, value):
    words = (replace(_words()[0], **{field: value}), *_words()[1:])
    assert "Invalid word" in audit_caption_text("Bright little lantern", words)


def test_cut_audit_reviews_overlapping_long_and_multitoken_spans():
    words = _words()
    assert "Overlapping" in audit_caption_text(
        "Bright little lantern", (words[0], replace(words[1], start=1.49), words[2]),
    )
    assert "long recognized word" in audit_caption_text(
        "Bright little lantern", (*words[:-1], replace(words[-1], end=4)),
    )
    assert "multi-word" in audit_caption_text(
        "Bright little lantern", (TimingWord("Bright little lantern", 1, 2, 0.99),),
    )


def test_cut_audit_rejects_structurally_invalid_arguments():
    with pytest.raises(ValueError, match="string"):
        audit_caption_text(None, ())
    with pytest.raises(ValueError, match="sequence"):
        audit_caption_text("Bright", None)
    with pytest.raises(ValueError, match="TimingWord"):
        audit_caption_text("Bright", (None,))
    with pytest.raises(ValueError, match="callable"):
        audit_caption_text("Bright", (), check_cancel=None)


@pytest.mark.parametrize("cancel_at", [1, 100, 700])
def test_cut_audit_checks_cancellation_while_reading_and_comparing_words(cancel_at):
    calls = 0

    def check_cancel():
        nonlocal calls
        calls += 1
        if calls == cancel_at:
            raise RuntimeError("Canceled")

    text = " ".join(f"word{index}" for index in range(500))
    recognized = _words(text + " extra", step=0.1)
    with pytest.raises(RuntimeError, match="Canceled"):
        audit_caption_text(text.replace("word100", "different"), recognized,
                           check_cancel=check_cancel)
    assert calls == cancel_at


def test_corroborated_words_bound_display_window_and_preserve_every_original_field():
    cue = _cue(" Bright,\n little lantern! ")
    original = cue.to_dict()
    result = correct_caption_timings([cue], [_evidence()], 10)
    assert _ranges(result) == [pytest.approx((1.05, 2.35))]
    assert result.confidences == (0.96,)
    assert result.review_reasons == ("",)
    assert result.captions[0].source.startswith(cue.source + " - model/audio aligned")
    assert "0.15s before / 0.25s after" in result.captions[0].source
    assert result.captions[0].text == cue.text
    assert result.captions[0].fragments is cue.fragments
    assert cue.to_dict() == original


def test_punctuation_cannot_attach_silence_to_lexical_edges():
    punctuation = (
        TimingWord("...", 0, 1.2, 0.01),
        *_words(),
        TimingWord("!", 2.1, 9, 0.01),
    )
    result = correct_caption_timings([_cue()], [_evidence(punctuation)], 10)
    assert _ranges(result) == [pytest.approx((1.05, 2.35))]
    assert result.review_reasons == ("",)


def test_first_word_attached_to_silence_uses_independent_onset_and_next_anchor():
    forced = (
        TimingWord("Bright", 0.2, 1.5, 0.98),
        *_words()[1:],
    )
    result = correct_caption_timings([_cue()], [_evidence(forced, _words())], 10)
    assert _ranges(result) == [pytest.approx((1.05, 2.35))]
    assert result.review_reasons == ("",)


def test_overlong_forced_first_word_can_only_be_rescued_by_independent_onset():
    forced = (
        TimingWord("Bright", 0, 1.8, 0.98),
        TimingWord("little", 1.8, 2.1, 0.98),
        TimingWord("lantern", 2.1, 2.4, 0.98),
    )
    recognized = (replace(forced[0], start=1.5), *forced[1:])
    result = correct_caption_timings([_cue()], [_evidence(forced, recognized)], 10)
    assert _ranges(result) == [pytest.approx((1.35, 2.65))]
    assert result.review_reasons == ("",)
    _assert_review(_cue(), _evidence(forced), "long", bounds=(1, 2.65))


def test_short_opening_word_recovers_earlier_independent_onset_not_just_highest_score():
    cue = _cue("We follow stars", 2.5, 3.5)
    recognized = (
        TimingWord("We", 2.02, 2.64, 0.92),
        TimingWord("follow", 2.64, 2.91, 0.94),
        TimingWord("stars", 2.91, 3.21, 0.97),
    )
    forced = (replace(recognized[0], start=2.5, probability=0.99), *recognized[1:])
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert _ranges(result) == [pytest.approx((1.87, 3.46))]
    assert result.review_reasons == ("",)
    assert result.confidences == (0.92,)


def test_opening_end_and_next_word_anchors_can_resolve_a_shifted_forced_onset():
    forced = (replace(_words()[0], start=0.9), *_words()[1:])
    result = correct_caption_timings([_cue()], [_evidence(forced, _words())], 10)
    assert _ranges(result) == [pytest.approx((1.05, 2.35))]
    assert result.review_reasons == ("",)


def test_single_short_token_keeps_earliest_agreed_onset_and_stays_separate():
    words = (TimingWord("Oh!", 1.1, 1.4, 0.98),)
    recognized = (TimingWord("oh", 1.0, 1.39, 0.99),)
    cue = _cue("Oh!", 1.2, 1.5)
    result = correct_caption_timings([cue], [_evidence(words, recognized)], 10)
    assert _ranges(result) == [pytest.approx((0.85, 1.64))]
    assert result.review_reasons == ("",)
    _assert_review(cue, _evidence((replace(words[0], start=0.1),), recognized), "anchor")


@pytest.mark.parametrize("explicit_prefix", [True, False])
def test_stutter_normalization_never_discards_an_unmeasured_onset(explicit_prefix):
    cue = _cue("L\u2014Lady waits", 1, 2.8)
    words = (TimingWord("Lady", 1.3, 1.6, 0.95), TimingWord("waits", 1.6, 1.9, 0.95))
    recognized = (TimingWord("L", 1.08, 1.25, 0.96), *words) if explicit_prefix else words
    result = correct_caption_timings([cue], [_evidence(words, recognized)], 10)
    assert result.captions[0].text == cue.text
    if explicit_prefix:
        assert _ranges(result) == [pytest.approx((0.93, 2.15))]
        assert result.review_reasons == ("",)
    else:
        assert _ranges(result) == [(cue.start, 2.15)]
        assert "stutter onset" in result.review_reasons[0].lower()


def test_combined_recognized_stutter_span_preserves_its_onset():
    cue = _cue("I\u2014I'm ready", 1.2, 2.5)
    words = (TimingWord("I'm", 1.02, 1.6, 0.95), TimingWord("ready", 1.6, 1.9, 0.95))
    recognized = (replace(words[0], text="I-I'm"), words[1])
    result = correct_caption_timings([cue], [_evidence(words, recognized)], 10)
    assert _ranges(result) == [pytest.approx((0.87, 2.15))]
    assert result.review_reasons == ("",)


def test_shared_boundary_moves_both_rows_and_recovers_next_opening_without_joining():
    left = _cue("Keep the beacon steady", 3.2, 5.1)
    right = _cue("as the travelers pass", 5.1, 6.2)
    left_words = _words(left.text, 3.08, 0.3)
    right_words = _words(right.text, 4.28, 0.3)
    context = (*left_words, *right_words)
    originals = [left.to_dict(), right.to_dict()]
    result = correct_caption_timings(
        [left, right], [_evidence(left_words, context), _evidence(right_words, context, 1)], 10,
    )
    assert _ranges(result) == [
        pytest.approx((2.93, 4.28)), pytest.approx((4.28, 5.73)),
    ]
    assert result.captions[0].end == result.captions[1].start
    assert [cue.text for cue in result.captions] == [left.text, right.text]
    assert result.review_reasons == ("", "")
    assert [left.to_dict(), right.to_dict()] == originals


@pytest.mark.parametrize("gap", [0, 1e-14, 0.1, 0.3, 0.4, 0.45, 1])
def test_joint_handles_use_actual_gaps_never_cut_spoken_words(gap):
    left_words = _words("Signal glows", 1, 0.3)
    right_words = _words("Birds return", 1.6 + gap, 0.3)
    result = correct_caption_timings(
        [_cue("Signal glows", 1, 2), _cue("Birds return", 2, 3)],
        [_evidence(left_words), _evidence(right_words, index=1)], 10,
    )
    left, right = result.captions
    assert result.review_reasons == ("", "")
    assert left.start == pytest.approx(0.85)
    assert left.end >= left_words[-1].end - 1e-12
    assert right.start <= right_words[0].start + 1e-12
    assert left.end <= right.start
    assert right.end == pytest.approx(right_words[-1].end + 0.25)
    if gap >= 0.4:
        assert left.end == pytest.approx(left_words[-1].end + 0.25)
        assert right.start == pytest.approx(right_words[0].start - 0.15)


def test_real_head_and_tail_stop_before_unassigned_recognized_speech():
    words = _words()
    context = (
        TimingWord("Unrelated", 0.8, 1.15, 0.94),
        *words,
        TimingWord("Another", 2.2, 2.5, 0.94),
    )
    result = correct_caption_timings([_cue()], [_evidence(words, context)], 10)
    assert _ranges(result) == [pytest.approx((1.15, 2.2))]
    assert result.review_reasons == ("",)


def test_source_bounds_and_reversed_input_order_preserve_row_indices():
    first = _cue("First light", 0, 1)
    last = _cue("Last light", 9, 10)
    first_words = _words(first.text, 0.05, 0.3)
    last_words = _words(last.text, 9.32, 0.3)
    result = correct_caption_timings(
        [last, first], [_evidence(first_words, index=1), _evidence(last_words)], 10,
    )
    assert _ranges(result) == [pytest.approx((9.17, 10)), pytest.approx((0, 0.9))]
    assert [cue.text for cue in result.captions] == [last.text, first.text]
    assert result.review_reasons == ("", "")


def test_missing_evidence_is_explicit_and_does_not_change_or_remove_rows():
    cues = [_cue(), _cue("Other phrase", 4, 5)]
    result = correct_caption_timings(cues, [], 10)
    assert _ranges(result) == [(cue.start, cue.end) for cue in cues]
    assert result.confidences == (None, None)
    assert all("Missing" in reason for reason in result.review_reasons)
    empty = correct_caption_timings([], [], 10)
    assert empty.captions == empty.confidences == empty.review_reasons == ()


def test_model_failure_reason_is_kept_for_review():
    _assert_review(_cue(), CaptionTimingEvidence(0, problem="Audio could not be decoded"), "decoded")


def test_uncertain_neighbor_blocks_intruding_words_not_only_padding():
    cues = [_cue("Signal glows", 1, 2.4), _cue("Birds return", 2.4, 3.4)]
    next_words = _words("Birds return", 2.1, 0.3)
    result = correct_caption_timings(cues, [_evidence(next_words, index=1)], 10)
    assert _ranges(result) == [(1, 2.4), pytest.approx((2.4, 2.95))]
    assert "neighboring" in result.review_reasons[1]
    assert result.confidences[0] is None
    assert result.confidences[1] is not None


def test_uncertain_neighbor_only_limits_handles_when_all_spoken_words_fit():
    cues = [_cue("Uncertain", 0, 1.1), _cue()]
    result = correct_caption_timings(cues, [_evidence(index=1)], 10)
    assert _ranges(result) == [(0, 1.1), pytest.approx((1.1, 2.35))]
    assert result.review_reasons[0]
    assert result.review_reasons[1] == ""


def test_conflicting_neighbor_evidence_does_not_cut_either_validated_word():
    cues = [_cue("Signal glows", 1, 2), _cue("Birds return", 2, 3)]
    evidence = [
        _evidence(_words("Signal glows", 1, 0.4)),
        _evidence(_words("Birds return", 1.7, 0.4), index=1),
    ]
    result = correct_caption_timings(cues, evidence, 10)
    assert _ranges(result) == [pytest.approx((0.85, 2)), pytest.approx((2, 2.75))]
    assert all("overlaps" in reason for reason in result.review_reasons)


def test_retaining_an_uncertain_closing_does_not_freeze_other_edges_or_previous_caption():
    cues = [
        _cue("First", 0, 2), _cue("Second", 2, 4), _cue("Unknown", 4, 6),
    ]
    evidence = [
        _evidence((TimingWord("First", 1.8, 2.4, 0.99),)),
        _evidence((TimingWord("Second", 2.8, 4.1, 0.99),), index=1),
    ]
    # Only the second ending conflicts with unknown. Its opening and both first-row edges fit.
    result = correct_caption_timings(cues, evidence, 10)
    assert _ranges(result) == [
        pytest.approx((1.65, 2.65)), pytest.approx((2.65, 4)), (4, 6),
    ]
    assert result.review_reasons[0] == ""
    assert all(result.review_reasons[1:])


def test_nested_uncertain_caption_blocks_nonadjacent_correction():
    cues = [
        _cue("Unknown outer", 0, 6), _cue("Unknown inner", 1, 2),
        _cue("Later words", 3, 4),
    ]
    result = correct_caption_timings(
        cues, [_evidence(_words("Later words", 3.1), index=2)], 10,
    )
    assert _ranges(result) == [(0, 6), (1, 2), pytest.approx((3, 3.95))]
    assert result.review_reasons[2]


def test_handles_cannot_overlap_an_earlier_enclosing_uncertain_caption():
    cues = [
        _cue("Unknown outer", 0, 6), _cue("Unknown inner", 1, 2),
        _cue("Later words", 6.5, 7.5),
    ]
    result = correct_caption_timings(
        cues, [_evidence(_words("Later words", 6.1), index=2)], 10,
    )
    assert _ranges(result) == [(0, 6), (1, 2), pytest.approx((6, 6.95))]
    assert result.review_reasons[2] == ""


@pytest.mark.parametrize("which", ["forced", "recognized"])
@pytest.mark.parametrize(("field", "value"), [
    ("start", float("nan")), ("start", float("inf")), ("start", -1),
    ("start", True), ("start", "1"), ("end", 1.2), ("end", 0.5), ("end", 11),
    ("end", float("-inf")), ("probability", float("nan")),
    ("probability", -0.01), ("probability", 1.01), ("probability", True),
])
def test_invalid_model_numbers_cannot_silently_certify_a_cut(which, field, value):
    words = _words()
    invalid = (replace(words[0], **{field: value}), *words[1:])
    evidence = _evidence(invalid, words) if which == "forced" else _evidence(words, invalid)
    _assert_review(_cue(), evidence, "Invalid")


@pytest.mark.parametrize("which", ["forced", "recognized"])
def test_nonmonotonic_or_overlapping_word_evidence_requires_review(which):
    words = _words()
    invalid = (words[1], words[0], words[2])
    _assert_review(
        _cue(), _evidence(invalid, words) if which == "forced" else _evidence(words, invalid),
        "nonmonotonic",
    )
    invalid = (words[0], replace(words[1], start=1.499), words[2])
    _assert_review(
        _cue(), _evidence(invalid, words) if which == "forced" else _evidence(words, invalid),
        "Overlapping",
    )


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("which", ["forced", "recognized"])
def test_independent_probability_controls_review_not_conditional_forced_score(index, which):
    words = _words()
    changed = tuple(replace(word, probability=0.64) if i == index else word
                    for i, word in enumerate(words))
    if which == "forced":
        result = correct_caption_timings([_cue()], [_evidence(changed, words)], 10)
        assert result.review_reasons == ("",)
        assert _ranges(result) == [pytest.approx((1.05, 2.35))]
        assert result.confidences == (0.96,)
    else:
        _assert_review(
            _cue(), _evidence(words, changed), "confidence",
            bounds=(1, 2.35) if index == 0 else (1.05, 3) if index == 2 else None,
        )


@pytest.mark.parametrize("index", [0, 2])
def test_edge_probability_floor_is_stricter_than_interior(index):
    words = tuple(replace(word, probability=0.74) if i == index else word
                  for i, word in enumerate(_words()))
    _assert_review(_cue(), _evidence(words), "opening/closing",
                   bounds=(1, 2.35) if index == 0 else (1.05, 3))
    interior = tuple(replace(word, probability=0.65) if i == 1 else word
                     for i, word in enumerate(_words()))
    result = correct_caption_timings([_cue()], [_evidence(interior)], 10)
    assert result.review_reasons == ("",)
    assert result.confidences == (0.65,)


@pytest.mark.parametrize("text", [
    "Bright small lantern", "Brillante petite lanterne", "Bright little",
    "Unknown Bright little lantern", "Bright very little lantern", "Brite little lantern",
])
def test_translation_hallucination_partial_and_fuzzy_matches_are_review_only(text):
    bounds = (
        (1.05, 3) if text == "Bright little" else
        (1, 2.35) if text == "Brite little lantern" else None
    )
    _assert_review(_cue(), _evidence(_words(), _words(text)), "corroborate", bounds=bounds)


def test_forced_caption_rewrite_and_multiword_timing_are_review_only():
    _assert_review(_cue(), _evidence(_words("Bright tiny lantern")), "exact caption text")
    _assert_review(_cue(), _evidence((TimingWord("Bright little lantern", 1, 2, 0.99),)),
                   "multi-word")
    _assert_review(_cue("... !"), _evidence(()), "lexical")


@pytest.mark.parametrize("marker", ["[unk]", "<unk>", "(inaudible)", "[Music]", "[BLANK_AUDIO]"])
def test_unknown_word_and_non_speech_placeholders_cannot_certify_timing(marker):
    _assert_review(_cue(marker), _evidence(_words(marker)), "marker")
    recognized = (replace(_words()[0], text="[unk]"), *_words()[1:])
    _assert_review(_cue(), _evidence(_words(), recognized), "marker")


def test_repeated_short_words_are_not_selected_just_for_high_probability():
    forced = (TimingWord("Go", 1, 1.3, 0.99),)
    recognized = (
        TimingWord("Go", 1, 1.15, 0.9), TimingWord("go", 1.15, 1.3, 0.99),
    )
    _assert_review(_cue("Go", 1, 1.5), _evidence(forced, recognized), "ambiguous")


def test_a_repeated_phrase_elsewhere_is_not_a_global_match():
    _assert_review(_cue(), _evidence(_words(), _words(start=7)), "locally")
    words = _words()
    result = correct_caption_timings(
        [_cue()], [_evidence(words, (*words, *_words(start=7)))], 10,
    )
    assert result.review_reasons == ("",)


def test_high_probabilities_alone_do_not_prove_close_enough_edges():
    words = _words()
    shifted = tuple(replace(word, start=word.start + 0.24, end=word.end + 0.24)
                    for word in words)
    _assert_review(_cue(), _evidence(words, shifted), "endings disagree")


@pytest.mark.parametrize("words", [
    (TimingWord("Bright", 1, 1.3, 0.99), TimingWord("little", 1.3, 1.5, 0.99),
     TimingWord("lantern", 1.5, 3.1, 0.99)),
    (TimingWord("Bright", 1, 1.3, 0.99), TimingWord("little", 3, 3.3, 0.99),
     TimingWord("lantern", 3.3, 3.6, 0.99)),
])
def test_suspicious_long_words_and_internal_gaps_are_review_only(words):
    _assert_review(_cue(), _evidence(words), "long",
                   bounds=(0.85, 3) if words[-1].end == 3.1 else (1, 3.85))


def test_nonzero_but_unphysical_microsecond_word_spans_are_review_only():
    words = (replace(_words()[0], end=1.200001), *_words()[1:])
    _assert_review(_cue(), _evidence(words), "short word")


def test_corrections_are_bounded_even_when_both_model_outputs_agree():
    _assert_review(_cue(), _evidence(_words(start=5)), "2-second")


def test_low_conditional_first_word_score_does_not_override_independent_lexical_evidence():
    cue = _cue("We remain extra vigilant", 2, 3.6)
    recognized = _words(cue.text, 1.7, 0.3)
    forced = (replace(recognized[0], start=1.44, probability=0.48), *recognized[1:])
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert result.review_reasons == ("",)
    assert _ranges(result) == [pytest.approx((1.55, 3.15))]
    assert result.confidences == (0.96,)


def test_shared_boundary_uses_one_recognized_clock_not_union_of_tolerated_estimates():
    captions = [_cue("Keep lantern steady", 1, 3.2), _cue("while travelers pass", 3.2, 4.2)]
    left = _words(captions[0].text, 1.2, 0.3)
    right = _words(captions[1].text, 2.1, 0.3)
    context = (*left, *right)
    forced_left = (*left[:-1], replace(left[-1], end=2.08))
    forced_right = (replace(right[0], start=2.08), *right[1:])
    result = correct_caption_timings(captions, [
        _evidence(forced_left, context), _evidence(forced_right, context, 1),
    ], 10)
    assert result.review_reasons == ("", "")
    assert result.captions[0].end == result.captions[1].start == 2.1
    assert _ranges(result) == [pytest.approx((1.05, 2.1)), pytest.approx((2.1, 3.25))]


def test_unmeasured_stutter_opening_does_not_freeze_its_closing_or_next_opening():
    captions = [
        _cue("Q\u2014Quite sorry to intrude", 1, 2.7),
        _cue("But lights are fading", 2.7, 4),
    ]
    left = _words("Quite sorry to intrude", 1.2, 0.3)
    right = _words(captions[1].text, 2.5, 0.3)
    context = (*left, *right)
    result = correct_caption_timings(captions, [
        _evidence(left, context), _evidence(right, context, 1),
    ], 10)
    assert result.captions[0].start == captions[0].start
    assert result.captions[0].end == result.captions[1].start == pytest.approx(2.4625)
    assert result.captions[0].end >= left[-1].end
    assert result.captions[1].start < right[0].start < captions[1].start
    assert "Stutter onset" in result.review_reasons[0]
    assert result.review_reasons[1] == ""
    assert [row.text for row in result.captions] == [row.text for row in captions]
    assert [row.fragments for row in result.captions] == [row.fragments for row in captions]


def test_conservative_stutter_guard_does_not_add_unassigned_neighbor_speech():
    cue = _cue("I\u2014I'm ready", 2, 3)
    words = _words("I'm ready", 1.5, 0.3)
    previous = TimingWord("Earlier", 1.1, 1.46, 0.99)
    result = correct_caption_timings([cue], [_evidence(words, (previous, *words))], 10)
    assert _ranges(result) == [pytest.approx((previous.end, 2.35))]
    assert result.review_reasons[0]


def test_uncertain_interior_words_allow_flagged_independent_edge_corrections_not_text_rewrites():
    cue = _cue("Keep every fragile object perfectly still", 1, 4)
    forced = _words(cue.text, 1.2, 0.3)
    recognized = tuple(
        replace(word, text="heavy") if index == 2 else word
        for index, word in enumerate(forced)
    )
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert _ranges(result) == [pytest.approx((1.05, 3.25))]
    assert "does not corroborate every" in result.review_reasons[0]
    assert "partial model/audio edge correction" in result.captions[0].source
    assert result.captions[0].text == cue.text
    assert result.captions[0].fragments is cue.fragments
    assert audit_caption_text(cue.text, recognized)


def test_weak_interior_model_likelihood_does_not_hide_review_or_freeze_validated_edges():
    cue = _cue("Keep every fragile object perfectly still", 1, 4)
    recognized = _words(cue.text, 1.2, 0.3)
    forced = tuple(
        replace(word, probability=0.01) if index == 2 else word
        for index, word in enumerate(recognized)
    )
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert _ranges(result) == [pytest.approx((1.05, 3.25))]
    assert "Very low forced" in result.review_reasons[0]


def test_grouped_uncertain_opening_retains_original_onset_but_not_spillover_at_closing():
    cue = _cue("Odd prefix keep lantern steady", 1, 3)
    forced = (
        TimingWord("Odd prefix", 1, 1.5, 0.4),
        *_words("keep lantern steady", 1.5, 0.3),
    )
    recognized = _words("keep lantern steady", 1.5, 0.3)
    result = correct_caption_timings([cue], [_evidence(forced, recognized)], 10)
    assert _ranges(result) == [pytest.approx((1, 2.65))]
    assert result.review_reasons[0]
    assert result.captions[0].text == cue.text


def test_partial_opening_limit_does_not_disable_valid_closing_within_its_safety_limit():
    cue = _cue("Keep lamps lit", 0, 3)
    words = _words(cue.text, 2.1, 0.3)
    result = correct_caption_timings([cue], [_evidence(words)], 10)
    assert _ranges(result) == [pytest.approx((0, 3.25))]
    assert "Opening correction exceeds" in result.review_reasons[0]


def test_partial_correction_chains_do_not_create_overlaps_or_out_of_source_ranges():
    randomizer = random.Random(41)
    for _ in range(20):
        captions = [_cue(f"Signal marker{index}", index * 2 + 0.5, index * 2 + 2)
                    for index in range(20)]
        evidence = []
        for index, cue in enumerate(captions):
            if randomizer.random() > 0.25:
                words = _words(
                    cue.text, max(0, cue.start + randomizer.uniform(-0.8, 0.8)),
                    randomizer.uniform(0.2, 0.5),
                )
                evidence.append(_evidence(words, index=index))
        result = correct_caption_timings(captions, evidence, 41)
        previous_end = 0.0
        for cue, source in zip(result.captions, captions, strict=True):
            assert previous_end <= cue.start < cue.end <= 41
            assert cue.text == source.text
            assert cue.fragments == source.fragments
            previous_end = cue.end


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True, "10", None, 10**400])
def test_invalid_duration_is_an_argument_error(duration):
    with pytest.raises(ValueError, match="duration"):
        correct_caption_timings([], [], duration)


@pytest.mark.parametrize(("start", "end"), [
    (-1, 2), (1, 11), (1, 1), (2, 1), (float("nan"), 2), (1, float("inf")), (True, 2),
])
def test_invalid_caption_ranges_are_not_silently_clamped(start, end):
    with pytest.raises(ValueError, match="caption time range"):
        correct_caption_timings([_cue(start=start, end=end)], [], 10)


@pytest.mark.parametrize("evidence", [
    [CaptionTimingEvidence(-1)], [CaptionTimingEvidence(1)], [CaptionTimingEvidence(True)],
    [CaptionTimingEvidence(0), CaptionTimingEvidence(0)],
])
def test_invalid_evidence_indices_are_argument_errors(evidence):
    with pytest.raises(ValueError, match="index"):
        correct_caption_timings([_cue()], evidence, 10)


def test_structurally_invalid_arguments_are_reported():
    for captions, evidence in [(None, []), ([], None), ("", []), ([None], [])]:
        with pytest.raises(ValueError):
            correct_caption_timings(captions, evidence, 10)
    with pytest.raises(ValueError, match="TimingWord"):
        correct_caption_timings([_cue()], [CaptionTimingEvidence(0, (None,))], 10)
    with pytest.raises(ValueError, match="callable"):
        correct_caption_timings([], [], 10, check_cancel=None)


@pytest.mark.parametrize("cancel_at", [1, 20, 500, 1600])
def test_long_processing_passes_are_cancellable(cancel_at):
    calls = 0

    def check_cancel():
        nonlocal calls
        calls += 1
        if calls == cancel_at:
            raise RuntimeError("Canceled")

    captions = [_cue(start=i * 3, end=i * 3 + 2) for i in range(100)]
    evidence = [_evidence(_words(start=i * 3 + 0.2), index=i) for i in range(100)]
    with pytest.raises(RuntimeError, match="Canceled"):
        correct_caption_timings(captions, evidence, 300, check_cancel=check_cancel)
    assert calls == cancel_at


def test_confidences_are_finite_bounded_agreement_scores():
    for probability in [0, 0.5, 0.75, 0.96, 1]:
        words = tuple(replace(word, probability=probability) for word in _words())
        result = correct_caption_timings([_cue()], [_evidence(words)], 10)
        confidence = result.confidences[0]
        assert confidence is None or math.isfinite(confidence) and 0 <= confidence <= 1
