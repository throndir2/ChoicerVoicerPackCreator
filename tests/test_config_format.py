from __future__ import annotations

from choicer_voicer_pack_creator.config_format import (
    parse_config_text,
    render_clip_metadata,
    render_pack_info,
)


def test_pack_info_round_trip_and_canonical_crlf() -> None:
    payload = render_pack_info(
        'A "quoted" pack',
        "icon.png",
        ["Alice", "ボブ"],
        "First line\nSecond line",
    )
    assert b"\r\n" in payload
    assert b"\r\r\n" not in payload
    assert payload.replace(b"\r\n", b"").find(b"\n") == -1
    data = parse_config_text(payload.decode("utf-8"))["data"]
    assert data == {
        "title": 'A "quoted" pack',
        "icon": "icon.png",
        "authors": ["Alice", "ボブ"],
        "readme": "First line\nSecond line",
    }


def test_clip_metadata_uses_seconds_and_arrays() -> None:
    payload = render_clip_metadata(
        "Keep your eyes on me.",
        "037_Kamisato-Ayaka.png",
        141.56,
        ["Kamisato Ayaka", "Narrator"],
    )
    data = parse_config_text(payload.decode("utf-8"))["data"]
    assert data["dub_timestamps"] == [141.56]
    assert data["dub_characters"] == ["Kamisato Ayaka", "Narrator"]
    assert data["image"] == "037_Kamisato-Ayaka.png"
