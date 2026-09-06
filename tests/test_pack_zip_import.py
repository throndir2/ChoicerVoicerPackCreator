from __future__ import annotations

import hashlib
import io
import stat
import struct
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from choicer_voicer_pack_creator import pack_io
from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    path_leases,
)
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore

_CAPTION = '  “Résumé” — 勇者 says: "Hello!"\nA  second line.  '
_CHARACTERS = ["Zoë", "勇者"]


class FakeMedia:
    def probe(self, path: Path) -> SimpleNamespace:
        assert path.read_bytes() == b"original-video-with-audio"
        return SimpleNamespace(
            duration=20.0,
            width=1280,
            height=720,
            fps=30.0,
            video_codec="theora",
            audio_codec="vorbis",
            pixel_format="yuv420p",
            audio_sample_rate=48000,
            audio_channels=2,
        )

    def probe_audio_duration(self, path: Path) -> float:
        assert path.read_bytes().startswith(b"manual-prompt")
        return 2.5


def _importer() -> PackImporter:
    return PackImporter(FakeMedia())  # type: ignore[arg-type]


def _pack_files() -> dict[str, bytes]:
    metadata = render_clip_metadata(_CAPTION, "frames/一.png", 1.125, _CHARACTERS)
    metadata = metadata.replace(b"[1.125]", b"[1.125, 8.250]")
    return {
        "_pack_info.ini": render_pack_info(
            "Recovered 雨", "art/顔.png", ["Manual author"], "Keep these notes.\nExact text.",
        ) + b"custom_setting=true\r\n",
        "dub_video.ogv": b"original-video-with-audio",
        "_backing_track.mp3": b"silent-backing",
        "art/顔.png": b"original-icon",
        "001_勇者.txt": metadata + b"custom_prompt=true\r\n",
        "001_勇者.mp3": b"manual-prompt-one",
        "frames/一.png": b"manual-image-one",
        "002_Zoe.txt": render_clip_metadata("Goodbye!", "002_Zoe.png", 12.500, ["Zoë"]),
        "002_Zoe.mp3": b"manual-prompt-two",
        "002_Zoe.png": b"manual-image-two",
        "extras/custom.json": b'{"preserve": true}',
    }


def _write_zip(
    path: Path, files: dict[str, bytes] | None = None, root: str = "Pack",
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        if root:
            archive.writestr(root + "/", b"")
        for name, content in (files if files is not None else _pack_files()).items():
            archive.writestr(f"{root}/{name}" if root else name, content)
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_independent_imports_share_library_without_blocking_existing_readers(tmp_path: Path):
    source = _write_zip(tmp_path / "pack.zip")
    library = tmp_path / "library"
    existing = library / "existing" / "video.ogv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"playing source")
    barrier = threading.Barrier(2)

    def progress(message, fraction):
        if message == "Inspecting pack ZIP...":
            barrier.wait(timeout=3)

    with path_leases(read_paths=[existing]), ThreadPoolExecutor(2) as executor:
        futures = [
            executor.submit(_importer().import_zip, source, library, progress=progress)
            for _ in range(2)
        ]
        results = [future.result(timeout=5) for future in futures]
    roots = [result.project.source_pack_path for result in results]
    assert roots[0] != roots[1]
    assert all(Path(root).is_dir() for root in roots)
    assert existing.read_bytes() == b"playing source"


@pytest.mark.parametrize("phase", ["inventory", "extraction", "folder"])
def test_cancelled_zip_import_cleans_only_its_owned_directory(
    tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zip(tmp_path / "pack.zip")
    original = source.read_bytes()
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"unrelated")
    monkeypatch.setattr(pack_io, "_ZIP_BUFFER_SIZE", 16)
    stopped = False
    updates = []

    def progress(message: str, fraction: float | None) -> None:
        nonlocal stopped
        updates.append((message, fraction))
        if phase == "inventory":
            stopped |= message.startswith("Checking ZIP entry 2/")
        elif phase == "extraction":
            stopped |= message.startswith("Extracting ZIP entry") and bool(fraction)
        else:
            stopped |= message.startswith("Reading clip metadata")

    with pytest.raises(OperationCancelled, match="cancelled"):
        _importer().import_zip(
            source, destination, cancelled=lambda: stopped, progress=progress,
        )
    assert stopped
    assert updates
    assert _snapshot(destination) == {"keep.txt": b"unrelated"}
    assert source.read_bytes() == original


def test_changed_source_archive_discards_owned_extraction(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "pack.zip")
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"unrelated")

    def progress(message: str, fraction: float | None) -> None:
        if message == "Verifying source ZIP has not changed...":
            with source.open("ab") as stream:
                stream.write(b"external change")

    with pytest.raises(SourceChangedError):
        _importer().import_zip(source, destination, progress=progress)
    assert _snapshot(destination) == {"keep.txt": b"unrelated"}
    assert source.read_bytes().endswith(b"external change")


def test_cancel_request_during_zip_import_commit_keeps_successful_result(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "pack.zip")
    stopped = False

    def progress(message: str, fraction: float | None) -> None:
        nonlocal stopped
        stopped |= message.startswith("Finishing pack ZIP import")

    result = _importer().import_zip(
        source, tmp_path / "library", cancelled=lambda: stopped, progress=progress,
    )
    assert stopped
    assert _snapshot(Path(result.project.source_pack_path)) == _pack_files()


@pytest.mark.parametrize("root", ["Pack", "", "日本語 — 雨"])
@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_zip_import_preserves_manual_work_and_durable_project_paths(
    tmp_path: Path, root: str, compression: int,
) -> None:
    original = _pack_files()
    source = _write_zip(tmp_path / "pack.zip", original, root, compression)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "library"

    result = _importer().import_zip(source, destination)

    project = result.project
    pack = Path(project.source_pack_path)
    assert pack.is_relative_to(destination)
    if root:
        assert pack.name == root
    else:
        assert pack.name.startswith("pack-import-")
    assert _snapshot(pack) == original
    assert project.title == "Recovered 雨"
    assert project.authors == ["Manual author"]
    assert project.readme == "Keep these notes.\nExact text."
    assert project.preserve_source_video
    assert project.video_duration == 20.0
    assert project.video_fps == 30
    assert project.video_height == 720
    assert Path(project.video_path).read_bytes() == original["dub_video.ogv"]
    assert Path(project.backing_track_path).read_bytes() == original["_backing_track.mp3"]
    assert Path(project.icon_path).read_bytes() == original["art/顔.png"]
    segments = project.segments
    assert [item.start for item in segments] == [1.125, 8.25, 12.5]
    assert [item.end for item in segments] == [3.625, 10.75, 15.0]
    assert [item.caption for item in segments] == [_CAPTION, _CAPTION, "Goodbye!"]
    assert [item.characters for item in segments] == [_CHARACTERS, _CHARACTERS, ["Zoë"]]
    assert all(item.audio_mode == "file" for item in segments)
    assert all(not item.source_range_known for item in segments)
    assert Path(segments[0].audio_path).read_bytes() == original["001_勇者.mp3"]
    assert Path(segments[0].image_path).read_bytes() == original["frames/一.png"]
    assert segments[1].audio_path == segments[0].audio_path
    assert segments[1].image_path == segments[0].image_path
    assert Path(segments[2].audio_path).read_bytes() == original["002_Zoe.mp3"]
    assert Path(segments[2].image_path).read_bytes() == original["002_Zoe.png"]
    assert any("custom_setting" in warning for warning in result.warnings)
    assert any("custom_prompt" in warning for warning in result.warnings)
    assert any("extras/custom.json" in warning for warning in result.warnings)
    assert any("expanded" in warning for warning in result.warnings)
    assert result.warnings == project.import_warnings
    assert result.warnings == _importer().import_folder(pack).warnings

    saved_path = tmp_path / "projects" / "recovered.json"
    ProjectStore.save(project, saved_path)
    loaded = ProjectStore.load(saved_path)
    assert loaded.to_dict() == project.to_dict()
    media_paths = [
        loaded.video_path, loaded.backing_track_path, loaded.icon_path,
        *(item.audio_path for item in loaded.segments),
        *(item.image_path for item in loaded.segments),
    ]
    assert all(Path(path).is_file() and Path(path).is_relative_to(pack) for path in media_paths)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_repeated_zip_imports_do_not_overwrite_existing_folders(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "pack.zip")
    destination = tmp_path / "library"
    existing = destination / "Pack"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_bytes(b"existing manual work")
    first = _importer().import_zip(source, destination)
    first_root = Path(first.project.source_pack_path)
    (first_root / "personal-edit.json").write_bytes(b"new manual edit")
    before_second = _snapshot(destination)

    second = _importer().import_zip(source, destination)

    second_root = Path(second.project.source_pack_path)
    assert first_root != second_root
    assert _snapshot(second_root) == _pack_files()
    assert all(_snapshot(destination)[name] == data for name, data in before_second.items())
    assert len(list(destination.iterdir())) == 3


def test_silent_high_compression_backing_is_not_treated_as_zip_bomb(tmp_path: Path) -> None:
    files = _pack_files()
    files["_backing_track.mp3"] = b"\0" * (4 * 1024 * 1024)
    source = _write_zip(tmp_path / "silent.zip", files)
    with zipfile.ZipFile(source) as archive:
        backing = archive.getinfo("Pack/_backing_track.mp3")
        assert backing.file_size / backing.compress_size > 250

    result = _importer().import_zip(source, tmp_path / "library")

    assert Path(result.project.backing_track_path).read_bytes() == files["_backing_track.mp3"]
    assert result.project.segments[0].caption == _CAPTION


@pytest.mark.parametrize(
    "entry",
    [
        "../outside.txt", "Pack/../../outside.txt", "/absolute.txt",
        "\\absolute.txt", "\\\\server\\share\\file.txt", "C:/absolute.txt", "C:relative.txt",
        "Pack\\nested.txt", "Pack/../nested.txt", "Pack/./nested.txt", "Pack//nested.txt",
        "Pack/file:stream", "Pack/con", "Pack/NUL.txt", "Pack/PRN.json", "Pack/AUX.ini",
        "Pack/COM1.txt", "Pack/lpt9.bin", "Pack/COM¹.txt", "Pack/LPT².txt",
        "Pack/CONIN$", "Pack/CONOUT$", "Pack/NUL .txt", "Pack/file.", "Pack/file ",
        "Pack/question?.txt", "Pack/star*.txt", 'Pack/quote".txt', "Pack/pipe|.txt",
        "Pack/<angle>.txt", "Pack/control\x01.txt", "//server/share/file.txt",
        "Pack/" + "nested/" * 64 + "file.txt",
    ],
)
def test_unsafe_member_is_rejected_before_any_extraction(tmp_path: Path, entry: str) -> None:
    source = _write_zip(tmp_path / "attack.zip")
    stored_name = entry.replace("\\", "~")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(stored_name, b"unsafe")
    if stored_name != entry:
        source.write_bytes(source.read_bytes().replace(stored_name.encode(), entry.encode()))
    before = _snapshot(tmp_path)

    with patch.object(pack_io, "_extract_pack_zip") as extract:
        with pytest.raises(ValueError, match="Unsafe ZIP entry path"):
            _importer().import_zip(source, tmp_path / "library")
        extract.assert_not_called()

    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "library").exists()


def test_embedded_nul_in_zip_name_is_rejected_before_extraction(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "nul.zip")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("Pack/nulXfile.bin", b"unsafe")
    source.write_bytes(source.read_bytes().replace(b"nulXfile", b"nul\0file"))
    with pytest.raises(ValueError, match="Unsafe ZIP entry path"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("Pack/extra.bin", b"one"), ("Pack/extra.bin", b"two")],
        [("Pack/EXTRA.bin", b"one"), ("Pack/extra.bin", b"two")],
        [("Pack/dir/one.bin", b"one"), ("Pack/DIR/two.bin", b"two")],
        [("PACK/extra.bin", b"one")],
        [("Pack/extra.bin", b"one"), ("Pack/extra.bin/child.bin", b"two")],
        [("Pack/extra.bin/child.bin", b"one"), ("Pack/extra.bin", b"two")],
        [("Pack/dir/", b""), ("Pack/dir/", b"")],
        [("Pack/dir/", b""), ("Pack/dir", b"file")],
        [("Pack/dir", b"file"), ("Pack/dir/", b"")],
    ],
)
def test_duplicate_case_collisions_and_file_directory_conflicts_are_rejected(
    tmp_path: Path, entries: list[tuple[str, bytes]],
) -> None:
    source = _write_zip(tmp_path / "collision.zip")
    with zipfile.ZipFile(source, "a") as archive:
        for name, content in entries:
            if sum(other == name for other, _ in entries) > 1 and name in archive.namelist():
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(name, content)
            else:
                archive.writestr(name, content)
    with patch.object(pack_io, "_extract_pack_zip") as extract:
        with pytest.raises(ValueError, match="duplicate|collision|conflict"):
            _importer().import_zip(source, tmp_path / "library")
        extract.assert_not_called()
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize("directory_first", [True, False])
def test_explicit_directories_can_precede_or_follow_their_children(
    tmp_path: Path, directory_first: bool,
) -> None:
    source = _write_zip(tmp_path / "directories.zip")
    entries = [("Pack/extra/", b""), ("Pack/extra/note.json", b"{}")]
    with zipfile.ZipFile(source, "a") as archive:
        for name, content in entries if directory_first else reversed(entries):
            archive.writestr(name, content)

    result = _importer().import_zip(source, tmp_path / "library")

    assert (Path(result.project.source_pack_path) / "extra" / "note.json").read_bytes() == b"{}"


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFBLK])
def test_symlinks_and_special_entries_are_rejected_before_extraction(
    tmp_path: Path, mode: int,
) -> None:
    source = _write_zip(tmp_path / "link.zip")
    entry = zipfile.ZipInfo("Pack/special")
    entry.create_system = 3
    entry.external_attr = (mode | 0o777) << 16
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(entry, b"../../outside")

    with pytest.raises(ValueError, match="link or special file"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


def test_windows_reparse_entry_is_rejected(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "reparse.zip")
    entry = zipfile.ZipInfo("Pack/reparse")
    entry.create_system = 0
    entry.external_attr = 0x400
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(entry, b"outside")
    with pytest.raises(ValueError, match="link or special file"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


def test_directory_payload_is_rejected_before_extraction(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "directory-data.zip")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("Pack/data/", b"hidden file data")

    with pytest.raises(ValueError, match="invalid entry size"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


def _patch_header_field(source: Path, field: str, value: int) -> None:
    payload = bytearray(source.read_bytes())
    for signature, offset in (
        (b"PK\x03\x04", 6 if field == "flags" else 8),
        (b"PK\x01\x02", 8 if field == "flags" else 10),
    ):
        start = payload.index(signature)
        struct.pack_into("<H", payload, start + offset, value)
    source.write_bytes(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("flags", 1, "encrypted"), ("flags", 0x40, "encrypted"), ("method", 99, "unsupported")],
)
def test_encryption_and_unsupported_compression_are_actionable(
    tmp_path: Path, field: str, value: int, message: str,
) -> None:
    source = _write_zip(tmp_path / "unsupported.zip")
    _patch_header_field(source, field, value)
    with pytest.raises(ValueError, match=message):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"readme.txt": b"not a pack"},
        {"A/_pack_info.ini": b"", "B/_pack_info.ini": b""},
        {"_pack_info.ini": b"", "nested/_pack_info.ini": b""},
        {"wrapper/Pack/_pack_info.ini": b""},
        {"Pack/_pack_info.ini": b"", "unrelated.txt": b"keep"},
    ],
)
def test_empty_missing_ambiguous_and_wrapped_roots_are_rejected(
    tmp_path: Path, files: dict[str, bytes],
) -> None:
    source = _write_zip(tmp_path / "layout.zip", files, root="")
    with pytest.raises(ValueError, match="empty|one pack root|top-level pack folder"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("_MAX_ZIP_MEMBERS", 2, "entry import limit"),
        ("_MAX_ZIP_EXPANDED_BYTES", 1, "expanded import limit"),
    ],
)
def test_declared_inventory_limits_are_checked_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int, message: str,
) -> None:
    source = _write_zip(tmp_path / "huge.zip")
    monkeypatch.setattr(pack_io, limit, value)
    with patch.object(pack_io, "_extract_pack_zip") as extract:
        with pytest.raises(ValueError, match=message):
            _importer().import_zip(source, tmp_path / "library")
        extract.assert_not_called()
    assert not (tmp_path / "library").exists()


def test_actual_extraction_size_limit_cleans_owned_directory(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "expanding.zip")
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"unrelated")
    real_open = zipfile.ZipFile.open

    def oversized_open(
        archive: zipfile.ZipFile, entry: zipfile.ZipInfo, *args: object, **kwargs: object,
    ) -> object:
        if entry.filename.endswith("_pack_info.ini"):
            return io.BytesIO(b"x" * (entry.file_size + 1))
        return real_open(archive, entry, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(zipfile.ZipFile, "open", oversized_open),
        pytest.raises(ValueError, match="beyond its declared size"),
    ):
        _importer().import_zip(source, destination)

    assert list(destination.iterdir()) == [destination / "keep.txt"]
    assert (destination / "keep.txt").read_bytes() == b"unrelated"


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_crc_failure_cleans_whole_transaction_not_only_pack_root(
    tmp_path: Path, compression: int,
) -> None:
    source = _write_zip(tmp_path / "bad-crc.zip", compression=compression)
    with zipfile.ZipFile(source) as archive:
        damaged = archive.getinfo("Pack/002_Zoe.mp3")
    payload = bytearray(source.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", payload, damaged.header_offset + 26)
    data_offset = damaged.header_offset + 30 + name_length + extra_length
    payload[data_offset] ^= 0xFF
    source.write_bytes(payload)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"existing")

    with pytest.raises(ValueError, match="CRC/decompression failure"):
        _importer().import_zip(source, destination)

    assert list(destination.iterdir()) == [destination / "keep.txt"]
    assert (destination / "keep.txt").read_bytes() == b"existing"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_invalid_pack_metadata_cleans_owned_transaction(tmp_path: Path) -> None:
    files = _pack_files()
    files["_pack_info.ini"] = b"not valid metadata"
    source = _write_zip(tmp_path / "bad-pack.zip", files)
    destination = tmp_path / "library"
    destination.mkdir()

    with pytest.raises(ValueError, match="Config value before a section"):
        _importer().import_zip(source, destination)

    assert list(destination.iterdir()) == []


def test_incomplete_member_data_cleans_owned_transaction(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "truncated-member.zip")
    destination = tmp_path / "library"
    real_open = zipfile.ZipFile.open

    def truncated_open(
        archive: zipfile.ZipFile, entry: zipfile.ZipInfo, *args: object, **kwargs: object,
    ) -> object:
        if entry.filename.endswith("_pack_info.ini"):
            return io.BytesIO(b"")
        return real_open(archive, entry, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(zipfile.ZipFile, "open", truncated_open),
        pytest.raises(ValueError, match="incomplete data"),
    ):
        _importer().import_zip(source, destination)

    assert list(destination.iterdir()) == []


def test_interrupted_import_cleans_owned_transaction(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "interrupted.zip")
    destination = tmp_path / "library"
    with (
        patch.object(PackImporter, "import_folder", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        _importer().import_zip(source, destination)
    assert list(destination.iterdir()) == []


def test_invalid_archive_is_actionable_and_leaves_destination_untouched(tmp_path: Path) -> None:
    source = tmp_path / "not-a-zip.zip"
    source.write_bytes(b"not a ZIP")
    with pytest.raises(ValueError, match="damaged or incomplete"):
        _importer().import_zip(source, tmp_path / "library")
    assert not (tmp_path / "library").exists()
    assert source.read_bytes() == b"not a ZIP"


def test_failed_unique_directory_creation_does_not_clean_existing_data(tmp_path: Path) -> None:
    source = _write_zip(tmp_path / "pack.zip")
    token = uuid.UUID(int=123)
    destination = tmp_path / "library"
    existing = destination / f"pack-import-{token.hex}"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_bytes(b"existing import")

    with (
        patch.object(pack_io.uuid, "uuid4", return_value=token),
        pytest.raises(ValueError, match="Could not read or extract"),
    ):
        _importer().import_zip(source, destination)

    assert _snapshot(existing) == {"keep.txt": b"existing import"}


def test_metadata_cannot_reference_media_outside_extracted_pack(tmp_path: Path) -> None:
    files = _pack_files()
    files["_pack_info.ini"] = render_pack_info("Unsafe references", "../outside.png", ["A"], "")
    files["001_勇者.txt"] = render_clip_metadata("Keep me", "../outside.png", 1.125, ["A"])
    source = _write_zip(tmp_path / "references.zip", files)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")

    result = _importer().import_zip(source, tmp_path / "library")

    assert result.project.icon_path == ""
    assert result.project.segments[0].image_path == ""
    assert result.project.segments[0].caption == "Keep me"
    assert any("escapes the selected pack folder" in warning for warning in result.warnings)
    assert outside.read_bytes() == b"private"
