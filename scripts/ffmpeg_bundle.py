from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    APP_VERSION = str(tomllib.load(project_file)["project"]["version"])
DEFAULT_MANIFEST = ROOT / "third_party" / "ffmpeg-windows-x64.json"
DEFAULT_CACHE = ROOT / ".cache" / "ffmpeg"
BUFFER_SIZE = 1024 * 1024
MARKER_NAME = ".bundle-complete.json"


class BundleError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "archive_name",
        "archive_url",
        "archive_sha256",
        "archive_root",
        "license_file",
        "runtime_files",
        "required_encoders",
    }
    missing = sorted(required - set(value))
    if missing:
        raise BundleError(f"FFmpeg manifest is missing: {', '.join(missing)}")
    if not isinstance(value["runtime_files"], list) or not value["runtime_files"]:
        raise BundleError("FFmpeg manifest runtime_files must be a non-empty array")
    return value


def download_archive(manifest: dict[str, Any], cache_dir: Path = DEFAULT_CACHE) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / str(manifest["archive_name"])
    expected = str(manifest["archive_sha256"]).casefold()
    if destination.is_file() and sha256(destination) == expected:
        return destination
    destination.unlink(missing_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(
        str(manifest["archive_url"]),
        headers={"User-Agent": f"ChoicerVoicerPackCreator-build/{APP_VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, BUFFER_SIZE)
        actual = sha256(partial)
        if actual != expected:
            raise BundleError(
                f"FFmpeg archive checksum mismatch: expected {expected}, downloaded {actual}"
            )
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def extract_runtime(
    archive: Path,
    destination: Path,
    manifest: dict[str, Any],
) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    bin_dir = destination / "bin"
    license_dir = destination / "licenses"
    bin_dir.mkdir(parents=True)
    license_dir.mkdir(parents=True)
    root = str(manifest["archive_root"]).strip("/")
    members = [str(item) for item in manifest["runtime_files"]]
    license_member = str(manifest["license_file"])
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        expected_members = [f"{root}/{item}" for item in [*members, license_member]]
        missing = [name for name in expected_members if name not in names]
        if missing:
            raise BundleError(f"FFmpeg archive is missing expected files: {missing}")
        for relative in members:
            source_name = f"{root}/{relative}"
            output = bin_dir / Path(relative).name
            with package.open(source_name) as source, output.open("wb") as target:
                shutil.copyfileobj(source, target, BUFFER_SIZE)
        with package.open(f"{root}/{license_member}") as source, (
            license_dir / "FFmpeg-LGPL-3.0.txt"
        ).open("wb") as target:
            shutil.copyfileobj(source, target, BUFFER_SIZE)
    refresh_metadata(destination, manifest)
    runtime_hashes = {
        path.name: sha256(path) for path in sorted(bin_dir.iterdir()) if path.is_file()
    }
    (destination / MARKER_NAME).write_text(
        json.dumps(
            {
                "archive_sha256": str(manifest["archive_sha256"]).casefold(),
                "runtime_sha256": runtime_hashes,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def refresh_metadata(destination: Path, manifest: dict[str, Any]) -> None:
    license_dir = destination / "licenses"
    license_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", destination / "THIRD_PARTY_NOTICES.md")
    (license_dir / "FFmpeg-build-provenance.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_runtime(destination: Path, manifest: dict[str, Any]) -> None:
    if sys.platform != "win32":
        raise BundleError("The pinned FFmpeg runtime is for Windows x86-64")
    ffmpeg = destination / "bin" / "ffmpeg.exe"
    ffprobe = destination / "bin" / "ffprobe.exe"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise BundleError("Extracted FFmpeg runtime is incomplete")
    import subprocess

    version = subprocess.run(
        [str(ffmpeg), "-version"], capture_output=True, text=True, check=False
    )
    if version.returncode != 0 or str(manifest["version"]) not in version.stdout:
        raise BundleError(
            "Extracted FFmpeg version does not match the pinned manifest "
            f"(exit {version.returncode}): {version.stdout or version.stderr}"
        )
    encoders = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    missing = [
        encoder
        for encoder in manifest["required_encoders"]
        if encoder not in encoders.stdout
    ]
    if encoders.returncode != 0 or missing:
        raise BundleError(f"Extracted FFmpeg lacks required encoders: {missing}")
    probe = subprocess.run(
        [str(ffprobe), "-version"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        raise BundleError("Extracted FFprobe did not start")


def prepare_bundle(destination: Path, cache_dir: Path = DEFAULT_CACHE) -> Path:
    manifest = load_manifest()
    marker = destination / MARKER_NAME
    if marker.is_file():
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        expected_archive = str(manifest["archive_sha256"]).casefold()
        runtime_hashes = metadata.get("runtime_sha256", {})
        if metadata.get("archive_sha256") != expected_archive or not isinstance(
            runtime_hashes, dict
        ):
            raise BundleError(
                f"Existing FFmpeg bundle has invalid provenance; remove it first: {destination}"
            )
        for filename, expected_hash in runtime_hashes.items():
            path = destination / "bin" / filename
            if not path.is_file() or sha256(path) != expected_hash:
                raise BundleError(
                    f"Existing FFmpeg bundle is incomplete or modified; remove it first: {destination}"
                )
            refresh_metadata(destination, manifest)
        verify_runtime(destination, manifest)
        return destination
    if destination.exists():
        raise BundleError(
            f"Existing FFmpeg bundle is incomplete; remove it first: {destination}"
        )
    archive = download_archive(manifest, cache_dir)
    if sha256(archive) != str(manifest["archive_sha256"]).casefold():
        raise BundleError("Cached FFmpeg archive no longer matches its manifest")
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        shutil.rmtree(temporary)
    extract_runtime(archive, temporary, manifest)
    verify_runtime(temporary, manifest)
    os.replace(temporary, destination)
    return destination


def main() -> int:
    destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "ffmpeg"
    prepare_bundle(destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
