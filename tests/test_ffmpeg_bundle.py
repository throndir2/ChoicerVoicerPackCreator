from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.media import MediaTools

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ffmpeg_bundle.py"
SPEC = importlib.util.spec_from_file_location("ffmpeg_bundle_under_test", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
FFMPEG_BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FFMPEG_BUNDLE)
BundleError = FFMPEG_BUNDLE.BundleError
download_archive = FFMPEG_BUNDLE.download_archive
extract_runtime = FFMPEG_BUNDLE.extract_runtime
refresh_metadata = FFMPEG_BUNDLE.refresh_metadata


def test_download_and_extract_runtime_from_verified_archive(tmp_path: Path) -> None:
    root = "ffmpeg-test"
    runtime_files = ["bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/avcodec.dll"]
    archive = tmp_path / "upstream.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for relative in runtime_files:
            package.writestr(f"{root}/{relative}", relative.encode())
        package.writestr(f"{root}/LICENSE.txt", b"LGPL test license")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "archive_name": "cached.zip",
        "archive_url": archive.as_uri(),
        "archive_sha256": digest,
        "archive_root": root,
        "license_file": "LICENSE.txt",
        "runtime_files": runtime_files,
        "required_encoders": ["libtheora"],
    }

    downloaded = download_archive(manifest, tmp_path / "cache")
    assert downloaded.read_bytes() == archive.read_bytes()
    output = extract_runtime(downloaded, tmp_path / "output", manifest)
    assert sorted(path.name for path in (output / "bin").iterdir()) == [
        "avcodec.dll",
        "ffmpeg.exe",
        "ffprobe.exe",
    ]
    assert (output / "licenses" / "FFmpeg-LGPL-3.0.txt").read_bytes() == b"LGPL test license"
    assert (output / "THIRD_PARTY_NOTICES.md").is_file()
    assert json.loads(
        (output / "licenses" / "FFmpeg-build-provenance.json").read_text(encoding="utf-8")
    ) == manifest
    (output / "THIRD_PARTY_NOTICES.md").write_text("stale", encoding="utf-8")
    refresh_metadata(output, manifest)
    assert "Bundled binary provenance" in (output / "THIRD_PARTY_NOTICES.md").read_text(
        encoding="utf-8"
    )


def test_download_rejects_checksum_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / "upstream.zip"
    archive.write_bytes(b"not the expected archive")
    manifest = {
        "archive_name": "cached.zip",
        "archive_url": archive.as_uri(),
        "archive_sha256": "0" * 64,
        "archive_root": "unused",
        "license_file": "LICENSE.txt",
        "runtime_files": ["bin/ffmpeg.exe"],
        "required_encoders": [],
    }
    with pytest.raises(BundleError, match="checksum mismatch"):
        download_archive(manifest, tmp_path / "cache")
    assert not (tmp_path / "cache" / "cached.zip").exists()
    assert not (tmp_path / "cache" / "cached.zip.partial").exists()


def test_application_directory_ffmpeg_pair_precedes_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = ".exe" if sys.platform == "win32" else ""
    application = tmp_path / "application"
    bundled = application / "bin"
    system = tmp_path / "system"
    bundled.mkdir(parents=True)
    system.mkdir()
    for directory in (bundled, system):
        (directory / f"ffmpeg{suffix}").write_bytes(b"tool")
        (directory / f"ffprobe{suffix}").write_bytes(b"tool")
    monkeypatch.setattr(sys, "argv", [str(application / f"creator{suffix}")])
    monkeypatch.setattr(
        "choicer_voicer_pack_creator.media.shutil.which",
        lambda name: str(system / f"{name}{suffix}"),
    )

    ffmpeg, ffprobe = MediaTools._find_tool_pair()
    assert Path(ffmpeg) == (bundled / f"ffmpeg{suffix}").resolve()
    assert Path(ffprobe) == (bundled / f"ffprobe{suffix}").resolve()


@pytest.mark.parametrize("entrypoint", ["MCP", "_internal"])
def test_nested_frozen_mcp_finds_shared_ffmpeg_without_developer_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    application = tmp_path / "Portable app ü"
    bundled = application / "bin"
    bundled.mkdir(parents=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    for tool in ("ffmpeg", "ffprobe"):
        (bundled / f"{tool}{suffix}").write_bytes(b"bundled")
    executable = application / entrypoint / "Choicer Voicer MCP.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(application / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "argv", [str(executable)])
    monkeypatch.setattr("choicer_voicer_pack_creator.media.shutil.which", lambda _name: None)
    assert MediaTools._find_tool_pair() == (
        str(bundled / f"ffmpeg{suffix}"), str(bundled / f"ffprobe{suffix}"),
    )
