from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
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
