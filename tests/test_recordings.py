from __future__ import annotations

import os
from pathlib import Path

import pytest

from choicer_voicer_pack_creator import recordings
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope
from choicer_voicer_pack_creator.recordings import (
    default_game_location,
    find_takes,
    match_take,
    read_pack,
    resolve_game_location,
    scan_library,
)


def make_pack(folder: Path, stems: tuple[str, ...] = ("opaque-name",)) -> Path:
    folder.mkdir(parents=True)
    (folder / "_pack_info.ini").write_text('[data]\ntitle="Test pack"\nauthors=["Tester"]\n')
    (folder / "dub_video.ogv").write_bytes(b"Discovery must not try to decode media.")
    for stem in stems:
        (folder / f"{stem}.ini").write_text(
            '[data]\ncaption="Hello"\ndub_timestamps=[0.374, 1.25]\ndub_characters=["Cat"]\n',
        )
        (folder / f"{stem}.wav").write_bytes(b"Original prompts must not be probed or used.")
    return folder


def make_take(folder: Path, stems: tuple[str, ...] = ("opaque-name",)) -> Path:
    folder.mkdir(parents=True)
    for stem in stems:
        (folder / f"_dubrecord_{stem}.wav").write_bytes(b"Metadata-only discovery.")
    return folder


def test_location_is_bounded_and_accepts_known_parent_or_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_game_location() is None
    game = tmp_path / "YeahMaybe" / "ChoicerVoicer" / "game"
    (game / "packs_voice").mkdir(parents=True)
    executable = game / "ChoicerVoicer.exe"
    executable.write_bytes(b"")
    assert default_game_location() == game
    for selection in (game, game.parent, game.parent.parent, tmp_path, executable):
        location = resolve_game_location(selection)
        assert location.root == game
        assert location.packs_path == game / "packs_voice"
        assert location.recordings_path == game / "recordings" / "dub_recordings"
    unrelated = tmp_path / "unrelated"
    (unrelated / "nested" / "game" / "packs_voice").mkdir(parents=True)
    with pytest.raises(ValueError, match="No Choicer Voicer"):
        resolve_game_location(unrelated)
    with pytest.raises(ValueError, match="does not exist"):
        resolve_game_location(tmp_path / "missing")


def test_discovery_reads_metadata_not_audio_and_never_parses_name_timestamps(tmp_path):
    pack = read_pack(make_pack(tmp_path / "Pack", ("13-not-a-time", "x")))
    assert pack.title == "Test pack"
    assert pack.prompts[0].stem == "13-not-a-time"
    assert pack.prompts[0].timestamps == (0.374, 1.25)
    assert pack.prompts[0].characters == ("Cat",)
    assert pack.backing_path is None
    assert pack.snapshot is not None


def test_matching_uses_all_exact_identities_and_keeps_versions_ambiguous(tmp_path):
    pack_a = read_pack(make_pack(tmp_path / "same name", ("a", "b")))
    pack_b = read_pack(make_pack(tmp_path / "different name", ("a", "b", "c")))
    take = find_takes(make_take(tmp_path / "same name" / "take", ("a",)))[0]
    assert match_take(take, (pack_a, pack_b)) == (pack_a, pack_b)
    unknown = find_takes(make_take(tmp_path / "unknown" / "take", ("a", "no-match")))[0]
    assert match_take(unknown, (pack_a, pack_b)) == ()
    uppercase = find_takes(make_take(tmp_path / "uppercase" / "take", ("A",)))[0]
    assert match_take(uppercase, (pack_a,)) == ()


def test_find_takes_accepts_three_known_levels_and_surfaces_unknown_files(tmp_path):
    root = tmp_path / "dub_recordings"
    folder = make_take(root / "Pack" / "dated take")
    (folder / "wrong.wav").write_bytes(b"bad filename")
    (folder / "_dubrecord_bad.mp3").write_bytes(b"not a game WAV")
    (folder / "unexpected.txt").write_text("unknown")
    direct = find_takes(folder)
    assert find_takes(folder.parent) == direct
    assert find_takes(root) == direct
    assert {path.name for path in direct[0].recordings} == {
        "_dubrecord_opaque-name.wav", "wrong.wav", "_dubrecord_bad.mp3",
    }
    assert any("wrong.wav" in warning for warning in direct[0].warnings)
    assert any("unexpected.txt" in warning for warning in direct[0].warnings)


def test_library_keeps_invalid_packs_unmatched_takes_and_ambiguity_visible(tmp_path):
    root = tmp_path / "game"
    make_pack(root / "packs_voice" / "a")
    make_pack(root / "packs_voice" / "b")
    bad = make_pack(root / "packs_voice" / "invalid")
    (bad / "opaque-name.ini").write_text("[data]\ndub_timestamps=[-1]\n")
    make_take(root / "recordings" / "dub_recordings" / "Pack" / "take")
    make_take(root / "recordings" / "dub_recordings" / "Other" / "take", ("unknown",))
    library = scan_library(resolve_game_location(root))
    assert len(library.packs) == 3
    assert len(library.takes) == 2
    assert library.packs[2].errors
    assert any("ambiguous" in warning for warning in library.warnings)
    assert any("no matching pack" in warning for warning in library.warnings)


@pytest.mark.parametrize(
    "value", ["[-0.1]", "[NaN]", "[Infinity]", "[true]", '["1"]', "[]", "1", f"[{10**400}]"],
)
def test_invalid_timestamps_fail_closed(tmp_path, value):
    folder = make_pack(tmp_path / "pack")
    (folder / "opaque-name.ini").write_text(f"[data]\ndub_timestamps={value}\n")
    with pytest.raises(ValueError, match="Invalid dub_timestamps"):
        read_pack(folder)


@pytest.mark.parametrize(
    "text",
    [
        "[data]\ndub_timestamps=[1]\ndub_timestamps=[2]\n",
        "[data]\ndub_timestamps=[1]\n[data]\ncaption=\"x\"\n",
        "[data]\ndub_timestamps=[1, 1]\n",
    ],
)
def test_duplicate_timing_declarations_fail_closed(tmp_path, text):
    folder = make_pack(tmp_path / "pack")
    (folder / "opaque-name.ini").write_text(text)
    with pytest.raises(ValueError, match="Duplicate"):
        read_pack(folder)


@pytest.mark.parametrize("reference", ['"../secret.png"', '"C:/outside.png"', '"file.png:stream"'])
def test_unsafe_metadata_references_fail_closed(tmp_path, reference):
    folder = make_pack(tmp_path / "pack")
    (folder / "opaque-name.ini").write_text(
        f"[data]\ndub_timestamps=[0]\nimage={reference}\n",
    )
    with pytest.raises(ValueError, match="Unsafe"):
        read_pack(folder)


def test_unknown_metadata_is_reported_and_duplicate_prompt_stems_are_rejected(tmp_path):
    folder = make_pack(tmp_path / "pack")
    metadata = folder / "opaque-name.ini"
    metadata.write_text(metadata.read_text() + 'future_key="future"\n[other]\nvalue=1\n')
    assert any("future_key" in warning for warning in read_pack(folder).warnings)
    (folder / "opaque-name.txt").write_text("[data]\ndub_timestamps=[1]\n")
    with pytest.raises(ValueError, match="Ambiguous"):
        read_pack(folder)


def test_missing_recordings_remain_visible(tmp_path):
    folder = tmp_path / "pack" / "empty take"
    folder.mkdir(parents=True)
    takes = find_takes(folder)
    assert len(takes) == 1
    assert not takes[0].recordings
    assert any("no recording" in warning for warning in takes[0].warnings)


def test_discovery_limits_and_cancellation_are_explicit(tmp_path, monkeypatch):
    folder = make_take(tmp_path / "pack" / "take")
    (folder / "nested" / "one" / "two").mkdir(parents=True)
    with pytest.raises(ValueError, match="nested"):
        find_takes(folder)
    monkeypatch.setattr(recordings, "_MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="entry limit"):
        find_takes(folder)
    with pytest.raises(OperationCancelled), operation_scope(cancelled=lambda: True):
        find_takes(folder)


def test_metadata_size_is_bounded(tmp_path, monkeypatch):
    folder = make_pack(tmp_path / "pack")
    monkeypatch.setattr(recordings, "_MAX_METADATA_BYTES", 8)
    with pytest.raises(ValueError, match="byte limit"):
        read_pack(folder)


def test_directory_links_are_not_followed(tmp_path):
    outside = make_pack(tmp_path / "outside")
    link = tmp_path / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevation.")
    with pytest.raises(ValueError, match="Links and reparse"):
        read_pack(link)
    with pytest.raises(ValueError, match="Links and reparse"):
        read_pack(link / "child")
