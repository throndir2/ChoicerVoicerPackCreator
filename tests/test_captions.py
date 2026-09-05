from __future__ import annotations

import pytest

from choicer_voicer_pack_creator.captions import parse_json3


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
