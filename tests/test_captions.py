from __future__ import annotations

import pytest

from choicer_voicer_pack_creator.captions import pad_source_ranges, parse_json3, refine_captions
from choicer_voicer_pack_creator.models import CaptionFragment, SourceCaption


def test_json3_handles_window_events_appends_and_lingering_auto_captions() -> None:
    value = {
        "events": [
            {"tStartMs": 0, "wWinId": 1},
            {"tStartMs": 1000, "dDurationMs": 5000, "wWinId": 1,
             "segs": [{"utf8": "Hello &amp; "}, {"utf8": "welcome"}]},
            {"tStartMs": 1500, "dDurationMs": 4000, "wWinId": 1, "aAppend": 1,
             "segs": [{"utf8": "back."}]},
            {"tStartMs": 2000, "wWinId": 1, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 3000, "dDurationMs": 5000, "wWinId": 1,
             "segs": [{"utf8": "A new line."}]},
        ]
    }
    cues = parse_json3(value, 7, automatic=True, language="en")
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (1, 3, "Hello & welcome back."),
        (3, 7, "A new line."),
    ]
    assert cues[0].source == "YouTube automatic (en)"
    manual = parse_json3(value, 7, automatic=False, language="en")
    assert manual[0].end == 6
    assert manual[0].source == "YouTube creator (en)"


def test_captions_preserve_repeated_spoken_lines() -> None:
    cues = parse_json3(
        {"events": [
            {"tStartMs": start, "dDurationMs": 1000, "segs": [{"utf8": "Go!"}]}
            for start in (1000, 2000)
        ]},
        5, automatic=True, language="en",
    )
    assert [cue.text for cue in cues] == ["Go!", "Go!"]


@pytest.mark.parametrize("value", [
    {}, {"events": [None]}, {"events": [{"segs": [None]}]},
    {"events": [{"tStartMs": "nan", "dDurationMs": 1000, "segs": [{"utf8": "Hi"}]}]},
])
def test_bad_captions_are_reported(value) -> None:
    with pytest.raises(ValueError):
        parse_json3(value, 5, automatic=False, language="en")


def test_json3_keeps_decoded_spacing_and_absolute_fragment_offsets() -> None:
    cues = parse_json3({"events": [
        {"tStartMs": 1000, "dDurationMs": 5000, "wWinId": 1, "segs": [
            {"utf8": " Hi &amp; ", "tOffsetMs": 100},
            {"utf8": "hi", "tOffsetMs": 700},
            {"utf8": " again"},
        ]},
        {"tStartMs": 2500, "dDurationMs": 3500, "wWinId": 1, "aAppend": 1,
         "segs": [{"utf8": "\u4f60\u597d"}, {"utf8": "\uff01", "tOffsetMs": 100}]},
        {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "Next"}]},
    ]}, 7, automatic=True, language="en")
    assert (cues[0].start, cues[0].end, cues[0].text) == (1, 4, "Hi & hi again \u4f60\u597d\uff01")
    assert cues[0].fragments == (
        CaptionFragment(" Hi & ", 1.1), CaptionFragment("hi", 1.7),
        CaptionFragment(" again", None), CaptionFragment(" \u4f60\u597d", 2.5),
        CaptionFragment("\uff01", 2.6),
    )


@pytest.mark.parametrize("offset", [
    None, "bad", "NaN", "inf", float("-inf"), {}, [], True, -1, 5000,
])
def test_invalid_optional_offsets_retain_text_with_unknown_timing(offset) -> None:
    cue = parse_json3({"events": [
        {"tStartMs": 1000, "dDurationMs": 1000, "segs": [
            {"utf8": "Still "}, {"utf8": "here.", "tOffsetMs": offset},
        ]},
    ]}, 3, automatic=False, language="en")[0]
    assert cue.text == "Still here."
    assert cue.fragments == (CaptionFragment("Still ", 1), CaptionFragment("here."))
    refined = refine_captions([cue], [(1, 1.2), (1.7, 2)], 3)
    assert [(row.start, row.end, row.text) for row in refined] == [(0.85, 2.25, "Still here.")]
    assert "limited fragment timing" in refined[0].source


def test_duplicate_and_backwards_downloaded_offsets_become_unknown() -> None:
    cue = parse_json3({"events": [
        {"tStartMs": 0, "dDurationMs": 5000, "segs": [
            {"utf8": "Go ", "tOffsetMs": 1000},
            {"utf8": "go ", "tOffsetMs": 1000},
            {"utf8": "go!", "tOffsetMs": 500},
        ]},
    ]}, 5, automatic=True, language="en")[0]
    assert [fragment.start for fragment in cue.fragments] == [1, None, None]
    assert cue.text == "Go go go!"


def _cue(
    fragments: list[tuple[str, float | None]], start: float = 0, end: float = 5,
) -> SourceCaption:
    return SourceCaption(
        start, end, " ".join("".join(text for text, _ in fragments).split()),
        "YouTube automatic (en)",
        tuple(CaptionFragment(text, timestamp) for text, timestamp in fragments),
    )


def test_refinement_splits_only_recorded_boundaries_backed_by_actual_pause() -> None:
    cue = _cue([("Go, ", 0.1), ("go! ", 0.7), ("& ", 2.0), ("go.", 2.6)])
    original = cue.to_dict()
    rows = refine_captions([cue], [(0.1, 1.1), (2, 3)], 5)
    assert [row.text for row in rows] == ["Go, go!", "& go."]
    assert [(row.start, row.end) for row in rows] == [
        pytest.approx((0, 1.35)), pytest.approx((1.85, 3.25)),
    ]
    assert [fragment for row in rows for fragment in row.fragments] == list(cue.fragments)
    assert all("pause split" in row.source and "music/effects" in row.source for row in rows)
    assert cue.to_dict() == original


@pytest.mark.parametrize("spans", [
    [(0.1, 3.2)],  # A held vowel is activity, not silence between word starts.
    [(0.1, 1.0), (1.2, 3.2)],  # A short dip is below the requested pause length.
    [(0.1, 1.0), (1.5, 3.2)],  # A gap far from the next fragment is not its boundary.
    [(0.1, 2.5), (1.8, 3.2)],  # Overlapping scanner spans do not create silence.
])
def test_refinement_never_infers_silence_from_word_start_distance(spans) -> None:
    cue = _cue([("Nooooo ", 0.1), ("way!", 2.0)])
    rows = refine_captions([cue], spans, 5)
    assert len(rows) == 1
    assert rows[0].text == cue.text
    assert "pause split" not in rows[0].source


def test_pause_threshold_uses_unpadded_gap_not_padded_row_edges() -> None:
    cue = _cue([("First ", 0), ("second", 1.4)])
    spans = [(0, 1), (1.4, 2)]
    assert len(refine_captions([cue], spans, 5, pause_threshold=0.4)) == 2
    assert len(refine_captions([cue], spans, 5, pause_threshold=0.5)) == 1
    rows = refine_captions([cue], [(0, 1.2), (1.4, 2)], 5, pause_threshold=0.2)
    assert len(rows) == 2
    assert rows[0].end <= rows[1].start + 1e-9


def test_padding_respects_early_fragment_onsets_without_overlapping_cuts() -> None:
    cue = _cue([("First ", 0.1), ("second", 1.29)])
    rows = refine_captions([cue], [(0.21, 1.2), (1.4, 2)], 5, pause_threshold=0.2)
    assert len(rows) == 2
    assert rows[0].start <= 0.1
    assert 1.2 <= rows[0].end <= rows[1].start <= 1.29


@pytest.mark.parametrize("fragments", [
    [("All these words form one phrase.", 0.1)],
    [("First ", 0.1), ("unknown ", None), ("last.", 2)],
    [("First ", 0.1), ("same ", 0.1), ("last.", 2)],
    [("First ", 0.5), ("backwards ", 0.1), ("last.", 2)],
    [("First ", -1), ("last.", 2)],
    [("First ", float("nan")), ("last.", 2)],
    [("First ", 0.1), ("last.", 6)],
])
def test_limited_or_inconsistent_timing_keeps_whole_original_row(fragments) -> None:
    cue = _cue(fragments)
    rows = refine_captions([cue], [(0.1, 1), (2, 3)], 5)
    assert [(row.start, row.end, row.text) for row in rows] == [(0, 5, cue.text)]
    assert "unsplit: limited fragment timing" in rows[0].source


def test_legacy_captions_and_mismatched_fragment_text_are_not_rewritten() -> None:
    cues = [
        SourceCaption(0, 3, "Old original", "YouTube"),
        SourceCaption(3, 5, "Edited metadata mismatch", "YouTube",
                      (CaptionFragment("Something ", 3), CaptionFragment("else.", 4))),
    ]
    rows = refine_captions(cues, [(0, 1), (2, 5)], 5)
    assert [(r.start, r.end, r.text) for r in rows] == [
        (r.start, r.end, r.text) for r in cues
    ]
    assert all("limited fragment timing" in row.source for row in rows)


@pytest.mark.parametrize("spans", [None, [], [(4, 5)]])
def test_absent_silent_or_misaligned_audio_preserves_caption(spans) -> None:
    cue = _cue([("First ", 0.1), ("second.", 2)])
    rows = refine_captions([cue], spans, 5)
    assert [(row.start, row.end, row.text) for row in rows] == [(0, 5, cue.text)]
    assert "unsplit:" in rows[0].source


def test_refinement_keeps_multiword_tail_and_all_later_detected_audio() -> None:
    phrase = _cue([("First ", 0.1), ("a long final phrase", 2)])
    rows = refine_captions([phrase], [(0.1, 1), (2, 3)], 5)
    assert [row.text for row in rows] == ["First", "a long final phrase"]
    assert rows[-1].end == 5
    word = _cue([("First ", 0.1), ("last.", 2)])
    rows = refine_captions([word], [(0.1, 1), (2, 3), (4, 4.8)], 5)
    assert rows[-1].end == 5


def test_adjacent_display_rows_regroup_without_changing_originals() -> None:
    cues = [
        _cue([("Hello ", 0.1), ("hello", 0.5)], 0, 1),
        _cue([("world", 1.0), ("!", 1.2)], 1, 2),
    ]
    originals = [cue.to_dict() for cue in cues]
    rows = refine_captions(cues, [(0.1, 1.8)], 2)
    assert len(rows) == 1
    assert rows[0].text == "Hello hello world!"
    assert "display rows joined" in rows[0].source
    assert [cue.to_dict() for cue in cues] == originals


def test_standalone_punctuation_stays_with_its_words_across_a_pause() -> None:
    cue = _cue([("Wait", 0.1), ("! ", 2), ("Go", 2.1), (".", None)])
    rows = refine_captions([cue], [(0.1, 0.7), (2.1, 2.8)], 5)
    assert [row.text for row in rows] == ["Wait!", "Go."]
    assert tuple(fragment for row in rows for fragment in row.fragments) == cue.fragments
    punctuation = _cue([("!", 0.1), ("?", 2)])
    unchanged = refine_captions([punctuation], [(0.1, 0.7), (2, 2.8)], 5)
    assert len(unchanged) == 1
    assert unchanged[0].text == "!?"


def test_no_space_language_text_survives_splitting_and_display_regrouping() -> None:
    cue = _cue([("\u4f60\u597d", 0.1), ("\u4e16\u754c", 1.5), ("\uff01", 1.8)])
    split = refine_captions([cue], [(0.1, 0.8), (1.5, 2)], 5)
    assert [row.text for row in split] == ["\u4f60\u597d", "\u4e16\u754c\uff01"]
    joined = refine_captions([
        _cue([("\u4f60\u597d", 0.1)], 0, 1), _cue([("\u4e16\u754c", 1.1)], 1, 2),
    ], [(0.1, 1.9)], 2)
    assert [row.text for row in joined] == ["\u4f60\u597d\u4e16\u754c"]


@pytest.mark.parametrize(("left", "right", "expected"), [
    ("\u4f60\u597d\uff0c", "\u4e16\u754c", "\u4f60\u597d\uff0c\u4e16\u754c"),
    ("\uc548\ub155\ud558\uc138\uc694", "\uc138\uacc4", "\uc548\ub155\ud558\uc138\uc694 \uc138\uacc4"),
    ("\u4f60\u597d", "Python", "\u4f60\u597dPython"),
])
def test_regrouping_does_not_invent_spaces_in_unspaced_languages(left, right, expected) -> None:
    rows = refine_captions([
        _cue([(left, 0.1)], 0, 1), _cue([(right, 1.1)], 1, 2),
    ], [(0.1, 1.9)], 2)
    assert [row.text for row in rows] == [expected]


def test_preserve_sentence_breaks_pauses_and_conservative_music_merge_limits() -> None:
    sentence = [_cue([("Stop.", 0.1)], 0, 1), _cue([("Next", 1.1)], 1, 2)]
    assert len(refine_captions(sentence, [(0.1, 2)], 2)) == 2
    short = [_cue([("Hello", 0.1)], 0, 1), _cue([("there", 1.1)], 1, 2)]
    assert len(refine_captions(short, [(0.1, 0.5), (1.1, 2)], 2)) == 2
    music = [_cue([("hello", i + 0.1)], i, i + 1) for i in range(100)]
    rows = refine_captions(music, [(0, 100)], 100)
    assert len(rows) > 10
    assert all(row.end - row.start <= 6 for row in rows)
    assert sum(row.text.count("hello") for row in rows) == 100
    verbose = [_cue([("a" * 15 + " ", i), ("b" * 15, i + 0.1)], i, i + 1)
               for i in range(5)]
    rows = refine_captions(verbose, [(0, 5)], 5)
    assert all(len(row.text) <= 120 for row in rows)


def test_overlapping_and_duplicate_events_are_preserved_not_deduplicated() -> None:
    cues = [
        _cue([("Again ", 0.1), ("again!", 1)], 0, 3),
        _cue([("Again ", 0.1), ("again!", 1)], 0, 3),
        _cue([("Also ", 2), ("here.", 3)], 2, 5),
    ]
    rows = refine_captions(cues, [(0.1, 0.6), (1, 5)], 5)
    assert [(r.start, r.end, r.text) for r in rows] == [
        (r.start, r.end, r.text) for r in cues
    ]
    assert all("overlapping" in row.source for row in rows)


def test_appended_events_keep_every_fragment_and_use_the_recorded_pause() -> None:
    cues = parse_json3({"events": [
        {"tStartMs": 0, "dDurationMs": 5000, "wWinId": 1, "segs": [
            {"utf8": "Hi &amp; "}, {"utf8": "hi.", "tOffsetMs": 300},
        ]},
        {"tStartMs": 2000, "dDurationMs": 3000, "wWinId": 1, "aAppend": 1,
         "segs": [{"utf8": "Hi "}, {"utf8": "again!", "tOffsetMs": 300}]},
    ]}, 5, automatic=True, language="en")
    rows = refine_captions(cues, [(0, 1), (2, 3)], 5)
    assert [row.text for row in rows] == ["Hi & hi.", "Hi again!"]
    assert tuple(fragment for row in rows for fragment in row.fragments) == cues[0].fragments


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1, 0.19, 1.01, True])
def test_refinement_rejects_invalid_threshold(threshold) -> None:
    with pytest.raises(ValueError, match="threshold"):
        refine_captions([], [], 5, pause_threshold=threshold)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_refinement_rejects_invalid_duration(duration) -> None:
    with pytest.raises(ValueError, match="duration"):
        refine_captions([], [], duration)


@pytest.mark.parametrize("span", [(0, float("nan")), (0, float("inf")), (-1, 2), (2, 1)])
def test_refinement_propagates_bad_audio_ranges(span) -> None:
    with pytest.raises(ValueError, match="audio activity range"):
        refine_captions([], [span], 5)


def test_refinement_bounds_output_and_keeps_empty_success_distinct() -> None:
    assert refine_captions([], [], 5) == []
    cue = _cue([("First ", 0), ("last", 3)], 0, 9)
    rows = refine_captions([cue], [(0, 1), (3, 8)], 5)
    assert [(row.start, row.end) for row in rows] == [(0, 1.25), (2.85, 5)]
    assert all(0 <= row.start < row.end <= 5 for row in rows)
    with pytest.raises(ValueError, match="caption time range"):
        refine_captions([_cue([("outside", 6)], 6, 7)], [], 5)


@pytest.mark.parametrize("phase", ["activity", "fragments", "regrouping"])
def test_refinement_checks_cancellation_in_long_loops(phase) -> None:
    calls = 0
    cancel_at = 1800 if phase == "regrouping" else 100

    class Cancelled(Exception):
        pass

    def check_cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == cancel_at:
            raise Cancelled

    spans = [(i * 2, i * 2 + 1) for i in range(500)] if phase == "activity" else [(0, 1000)]
    cues = (
        [_cue([("word ", i) for i in range(500)], 0, 999)]
        if phase == "fragments" else
        [_cue([("word", i)], i, i + 1) for i in range(500)]
    )
    with pytest.raises(Cancelled):
        refine_captions(cues, spans, 1000, check_cancel=check_cancel)
    assert calls == cancel_at


@pytest.mark.parametrize(("ranges", "expected"), [
    ([(1, 2)], [(0.85, 2.25)]),
    ([(0.1, 4.9)], [(0, 5)]),
    ([(1, 2), (2.1, 3)], [(0.85, 2.05), (2.05, 3.25)]),
    ([(1, 2), (2, 3)], [(0.85, 2), (2, 3.25)]),
    ([(3, 4), (1, 2)], [(2.85, 4.25), (0.85, 2.25)]),
    ([(1, 3), (2, 4)], [(1, 3), (2, 4)]),
    ([(1, 2), (1, 2), (3, 4)], [(1, 2), (1, 2), (2.85, 4.25)]),
    ([(0, 3), (1, 2), (3.1, 4)], [(0, 3), (1, 2), (3.05, 4.25)]),
    ([], []),
])
def test_source_handles_are_bounded_and_do_not_introduce_overlaps(ranges, expected):
    original = list(ranges)
    result = pad_source_ranges(ranges, 5)
    assert result == pytest.approx(expected)
    assert ranges == original
    for (start, end), (padded_start, padded_end) in zip(ranges, result, strict=True):
        assert 0 <= padded_start <= start < end <= padded_end <= 5


@pytest.mark.parametrize("span", [
    (float("nan"), 2), (1, float("inf")), (-1, 2), (1, 1), (2, 1), (1, 6),
])
def test_source_handles_reject_invalid_ranges(span):
    with pytest.raises(ValueError, match="time range"):
        pad_source_ranges([span], 5)


def test_source_handles_check_cancellation():
    def cancel():
        raise RuntimeError("Canceled")

    with pytest.raises(RuntimeError, match="Canceled"):
        pad_source_ranges([(1, 2)], 5, check_cancel=cancel)


@pytest.mark.parametrize("fragments", [(), (CaptionFragment("A whole phrase", 1),)])
def test_creator_captions_get_real_source_handles_without_inventing_word_timings(fragments):
    cue = SourceCaption(1, 2, "A whole phrase", "YouTube creator (en)", fragments)
    rows = refine_captions([cue], [(0.91, 2.18)], 5)
    assert (rows[0].start, rows[0].end) == (0.85, 2.25)
    assert rows[0].text == cue.text
    assert rows[0].fragments == cue.fragments
    assert "limited fragment timing" in rows[0].source
    assert "source audio handles" in rows[0].source
    assert (cue.start, cue.end) == (1, 2)


def test_refinement_adds_source_handles_once_at_audio_edges():
    cue = _cue([("First ", 1), ("last.", 2.5)], start=1, end=3)
    row = refine_captions([cue], [(0.95, 3.04)], 5)[0]
    assert (row.start, row.end) == (0.85, 3.25)
