from __future__ import annotations

from pathlib import Path

import pytest

from choicer_voicer_pack_creator.exporter import safe_name


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("A normal title - (2026) & friends!", "A normal title - (2026) & friends!"),
        ('A: "great" <video> / take\\2|?*', "A great video  take2"),
        ("Start\x00\x01\x1f\t\nEnd", "StartEnd"),
        ("../Not a filename", "..Not a filename"),
        ("  Title . .  ", "Title"),
        ("", "Dub Pack"),
        (" . . ", "Dub Pack"),
        ('<>:"/\\|?*', "Dub Pack"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("CoN .clip", "_CoN .clip"),
        ("PRN", "_PRN"),
        ("aux", "_aux"),
        ("NUL", "_NUL"),
        ("CONIN$", "_CONIN$"),
        ("conout$", "_conout$"),
        ("COM1", "_COM1"),
        ("com9.mp4", "_com9.mp4"),
        ("LPT1", "_LPT1"),
        ("lpt9", "_lpt9"),
        ("COM\u00b9", "_COM\u00b9"),
        ("LPT\u00b2", "_LPT\u00b2"),
        ("LPT\u00b3.txt", "_LPT\u00b3.txt"),
        ("Company", "Company"),
        ("COM10", "COM10"),
        ("LPT0", "LPT0"),
        ("CONcert.txt", "CONcert.txt"),
        ("Caf\u00e9 \u65e5\u672c\u8a9e \U0001f3b5", "Caf\u00e9 \u65e5\u672c\u8a9e \U0001f3b5"),
    ],
)
def test_safe_name_produces_writable_names_without_losing_valid_text(
    tmp_path: Path, title: str, expected: str,
) -> None:
    assert safe_name(title) == expected
    assert safe_name(expected) == expected
    folder = tmp_path / expected
    folder.mkdir()
    project = folder / f"{expected}.cvpack.json"
    project.write_text("{}", encoding="utf-8")
    assert project.read_text(encoding="utf-8") == "{}"


def test_safe_name_preserves_custom_fallback() -> None:
    assert safe_name("???", fallback="YouTube video") == "YouTube video"
