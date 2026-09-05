from __future__ import annotations

import pytest

from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.captions import compare_caption, parse_json3


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


def test_comparison_normalizes_punctuation_but_flags_text_and_timing() -> None:
    drafts = [AnalysisSuggestion(1.1, 2.1, "Hello, WORLD!", "Whisper")]
    assert compare_caption(1, 2, "hello world", drafts).status == "Text agrees"
    assert compare_caption(1, 2, "Hello word", drafts).status == "Text differs - review"
    assert compare_caption(0, 3, "Hello world", drafts).status == "Timing differs - review"
    assert compare_caption(4, 5, "Hello world", drafts).status == "No Whisper match - review"


def test_comparison_combines_overlapping_whisper_cuts_without_claiming_accuracy() -> None:
    drafts = [
        AnalysisSuggestion(1, 2, "Hello", "Whisper"),
        AnalysisSuggestion(2, 3, "world", "Whisper"),
        AnalysisSuggestion(3, 4, "", "Audio activity"),
    ]
    comparison = compare_caption(1, 3, "Hello world", drafts)
    assert comparison.text == "Hello world"
    assert comparison.status == "Text agrees"
    assert "1.000-2.000: Hello" in comparison.timing
