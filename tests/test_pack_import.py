from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
from choicer_voicer_pack_creator.operations import OperationCancelled, SourceChangedError
from choicer_voicer_pack_creator.pack_io import PackImporter


class FakeMedia:
    def probe(self, _path: Path) -> SimpleNamespace:
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

    def probe_audio_duration(self, _path: Path) -> float:
        return 2.5


def test_imports_real_triplet_layout_and_expands_reused_timestamps(tmp_path: Path) -> None:
    (tmp_path / "_pack_info.ini").write_bytes(
        render_pack_info("Imported Pack", "icon.png", ["Creator"], "Notes")
    )
    (tmp_path / "dub_video.ogv").write_bytes(b"video")
    (tmp_path / "icon.png").write_bytes(b"png")
    (tmp_path / "001_Hero.mp3").write_bytes(b"audio")
    (tmp_path / "001_Hero.png").write_bytes(b"image")
    metadata = render_clip_metadata("Hello!", "001_Hero.png", 4.0, ["Hero"])
    metadata = metadata.replace(b"dub_timestamps=[4.000]", b"dub_timestamps=[4.000, 10.000]")
    metadata = metadata.replace(b"\r\n\r\n", b"\r\n\r\ndub_only=true\r\n", 1)
    (tmp_path / "001_Hero.txt").write_bytes(metadata)
    (tmp_path / "custom-notes.json").write_text("{}", encoding="utf-8")

    result = PackImporter(FakeMedia()).import_folder(tmp_path)  # type: ignore[arg-type]
    assert result.project.title == "Imported Pack"
    assert result.project.authors == ["Creator"]
    assert [item.start for item in result.project.segments] == [4.0, 10.0]
    assert [item.end for item in result.project.segments] == [6.5, 12.5]
    assert all(item.audio_mode == "file" for item in result.project.segments)
    assert all(not item.source_range_known for item in result.project.segments)
    assert any("expanded" in warning for warning in result.warnings)
    assert any("dub_only" in warning for warning in result.warnings)
    assert any("custom-notes.json" in warning for warning in result.warnings)
    assert result.project.import_warnings == result.warnings


def test_import_does_not_follow_image_reference_outside_pack(tmp_path: Path) -> None:
    outside = tmp_path.parent / "private.png"
    outside.write_bytes(b"private")
    (tmp_path / "_pack_info.ini").write_bytes(
        render_pack_info("Unsafe", "../private.png", ["Creator"], "")
    )
    (tmp_path / "dub_video.ogv").write_bytes(b"video")
    (tmp_path / "001_Hero.mp3").write_bytes(b"audio")
    metadata = render_clip_metadata("Hello", "../private.png", 1.0, ["Hero"])
    (tmp_path / "001_Hero.txt").write_bytes(metadata)

    result = PackImporter(FakeMedia()).import_folder(tmp_path)  # type: ignore[arg-type]
    assert result.project.icon_path == ""
    assert result.project.segments[0].image_path == ""
    assert any("escapes the selected pack folder" in warning for warning in result.warnings)


def _write_cancellation_fixture(root: Path) -> None:
    (root / "_pack_info.ini").write_bytes(
        render_pack_info("Pack", "icon.png", ["Creator"], "")
    )
    (root / "dub_video.ogv").write_bytes(b"video")
    for index in range(3):
        (root / f"{index}.txt").write_bytes(
            render_clip_metadata("Hello", f"{index}.png", float(index + 1), ["Hero"])
        )
        (root / f"{index}.mp3").write_bytes(b"audio")


def test_folder_cancellation_preserves_all_source_files(tmp_path: Path) -> None:
    _write_cancellation_fixture(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    stopped = False
    updates = []

    def progress(message: str, fraction: float | None) -> None:
        nonlocal stopped
        updates.append((message, fraction))
        stopped |= message == "Reading clip metadata 2/3"

    with pytest.raises(OperationCancelled):
        PackImporter(FakeMedia()).import_folder(  # type: ignore[arg-type]
            tmp_path, cancelled=lambda: stopped, progress=progress,
        )
    assert updates[-1] == ("Reading clip metadata 2/3", 1 / 3)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_folder_import_rejects_source_inventory_changes(tmp_path: Path) -> None:
    _write_cancellation_fixture(tmp_path)

    def progress(message: str, fraction: float | None) -> None:
        if message == "Pack import ready":
            (tmp_path / "new-file.txt").write_bytes(b"external change")

    with pytest.raises(SourceChangedError):
        PackImporter(FakeMedia()).import_folder(tmp_path, progress=progress)  # type: ignore[arg-type]
    assert (tmp_path / "new-file.txt").read_bytes() == b"external change"
    assert (tmp_path / "dub_video.ogv").read_bytes() == b"video"
