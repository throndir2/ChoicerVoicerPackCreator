from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from ffmpeg_bundle import prepare_bundle

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    APP_VERSION = str(tomllib.load(project_file)["project"]["version"])
DIST = ROOT / "dist" / f"v{APP_VERSION}"
BUILD = ROOT / "build" / f"pyinstaller-v{APP_VERSION}"
APP_NAME = "Choicer Voicer Pack Creator"
FFMPEG_STAGE = ROOT / "build" / "ffmpeg-windows-x64-562ea50b4f2d213e"
LATEST_BUILD_MANIFEST = DIST / "latest-portable.json"
PENDING_BUILD_MANIFEST = DIST / "pending-portable.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_fsynced(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if _sha256(temporary) != _sha256(source):
            raise RuntimeError(f"Copied file failed verification: {destination}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_tree_streamed(source: Path, destination: Path) -> None:
    """Copy files through userspace to avoid Windows block-cloned executable images."""
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as input_stream, destination_path.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        shutil.copystat(source_path, destination_path)


def _write_manifest_atomic(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.{value['build_id']}.partial")
    try:
        with temporary.open("wb") as stream:
            stream.write((json.dumps(value, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dist_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    try:
        path.relative_to(DIST.resolve())
    except ValueError as error:
        raise RuntimeError(f"Build manifest path escapes the distribution folder: {value}") from error
    return path


def build_candidate() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(parents=True, exist_ok=True)
    build_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    portable_root = DIST / f"portable-{build_id}"
    print("Preparing pinned LGPL FFmpeg runtime…", flush=True)
    prepare_bundle(FFMPEG_STAGE)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        APP_NAME,
        "--windowed",
        "--onedir",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(portable_root),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        "--paths",
        str(ROOT / "src"),
        "--hidden-import",
        "PySide6.QtMultimedia",
        "--hidden-import",
        "PySide6.QtMultimediaWidgets",
        "--add-data",
        f"{ROOT / 'assets'}{os.pathsep}assets",
        "--add-data",
        f"{ROOT / 'src' / 'choicer_voicer_pack_creator' / 'resources'}"
        f"{os.pathsep}choicer_voicer_pack_creator/resources",
        str(ROOT / "src" / "choicer_voicer_pack_creator" / "__main__.py"),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        return completed.returncode

    app_dir = portable_root / APP_NAME
    _copy_tree_streamed(FFMPEG_STAGE / "bin", app_dir / "bin")
    shutil.copytree(FFMPEG_STAGE / "licenses", app_dir / "licenses", dirs_exist_ok=True)
    shutil.copy2(FFMPEG_STAGE / "THIRD_PARTY_NOTICES.md", app_dir / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "LICENSE", app_dir / "LICENSE.txt")
    shutil.copy2(ROOT / "README.md", app_dir / "README.md")
    shutil.copy2(
        FFMPEG_STAGE / "licenses" / "FFmpeg-LGPL-3.0.txt",
        app_dir / "licenses" / "LGPL-3.0.txt",
    )
    resource_dir = ROOT / "src" / "choicer_voicer_pack_creator" / "resources"
    shutil.copy2(resource_dir / "WhisperCpp-MIT.txt", app_dir / "licenses")
    shutil.copy2(resource_dir / "OpenAI-Whisper-MIT.txt", app_dir / "licenses")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        print(f"Python license was not found: {python_license}", file=sys.stderr)
        return 1
    shutil.copy2(python_license, app_dir / "licenses" / "Python-3.12.txt")
    pyinstaller_licenses = sorted(
        (Path(sys.prefix) / "Lib" / "site-packages").glob(
            "pyinstaller-*.dist-info/licenses/COPYING.txt"
        )
    )
    if len(pyinstaller_licenses) != 1:
        print("Could not uniquely locate the PyInstaller license", file=sys.stderr)
        return 1
    shutil.copy2(
        pyinstaller_licenses[0], app_dir / "licenses" / "PyInstaller-bootloader.txt"
    )

    ffmpeg = app_dir / "bin" / "ffmpeg.exe"
    ffprobe = app_dir / "bin" / "ffprobe.exe"
    for executable in (ffmpeg, ffprobe):
        result = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"Bundled tool failed to start: {executable}", file=sys.stderr)
            return 1
    stable_archive = DIST / f"Choicer-Voicer-Pack-Creator-{APP_VERSION}-Windows-x64.zip"
    candidate_archive = portable_root / f".{stable_archive.name}.candidate"
    partial_archive = portable_root / f".{stable_archive.name}.partial"
    try:
        with zipfile.ZipFile(
            partial_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as package:
            for path in sorted(app_dir.rglob("*")):
                if path.is_file():
                    package.write(
                        path, f"{APP_NAME}/{path.relative_to(app_dir).as_posix()}"
                    )
        with zipfile.ZipFile(partial_archive) as package:
            bad_member = package.testzip()
            if bad_member:
                print(
                    f"Generated ZIP failed CRC validation: {bad_member}",
                    file=sys.stderr,
                )
                return 1
        os.replace(partial_archive, candidate_archive)
    finally:
        partial_archive.unlink(missing_ok=True)

    manifest: dict[str, str] = {
        "version": APP_VERSION,
        "build_id": build_id,
        "application_directory": app_dir.relative_to(ROOT).as_posix(),
        "executable": (app_dir / f"{APP_NAME}.exe").relative_to(ROOT).as_posix(),
        "candidate_archive": candidate_archive.relative_to(ROOT).as_posix(),
        "archive": stable_archive.relative_to(ROOT).as_posix(),
    }
    _write_manifest_atomic(PENDING_BUILD_MANIFEST, manifest)
    print(f"Bundled application with FFmpeg: {app_dir}", flush=True)
    print(f"Unpromoted ZIP candidate: {candidate_archive}", flush=True)
    return 0


def promote_candidate(expected_build_id: str) -> int:
    if not PENDING_BUILD_MANIFEST.is_file():
        raise FileNotFoundError(f"Pending portable-build manifest not found: {PENDING_BUILD_MANIFEST}")
    value = json.loads(PENDING_BUILD_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Pending portable-build manifest must contain a JSON object")
    manifest = {str(key): str(item) for key, item in value.items()}
    if manifest.get("version") != APP_VERSION:
        raise RuntimeError("Pending portable-build manifest has the wrong version")
    if manifest.get("build_id") != expected_build_id:
        raise RuntimeError("Pending portable-build manifest has an unexpected build identifier")
    candidate_archive = _dist_path(manifest["candidate_archive"])
    stable_archive = _dist_path(manifest["archive"])
    executable = _dist_path(manifest["executable"])
    if not candidate_archive.is_file() or not executable.is_file():
        raise RuntimeError("Pending portable build is incomplete")

    backup = DIST / f".{stable_archive.name}.previous-{expected_build_id}"
    stable_existed = stable_archive.is_file()
    stable_backed_up = False
    candidate_promoted = False
    try:
        if stable_existed:
            _copy_file_fsynced(stable_archive, backup)
            stable_backed_up = True
        os.replace(candidate_archive, stable_archive)
        candidate_promoted = True
        latest_manifest = dict(manifest)
        latest_manifest.pop("candidate_archive", None)
        _write_manifest_atomic(LATEST_BUILD_MANIFEST, latest_manifest)
    except Exception as promotion_error:
        rollback_errors: list[str] = []
        if candidate_promoted and stable_archive.is_file():
            if stable_existed:
                try:
                    _copy_file_fsynced(stable_archive, candidate_archive)
                except Exception as error:
                    rollback_errors.append(f"could not retain validated candidate: {error}")
            else:
                try:
                    os.replace(stable_archive, candidate_archive)
                except OSError as error:
                    rollback_errors.append(f"could not retract candidate ZIP: {error}")
        if stable_backed_up and backup.is_file():
            try:
                os.replace(backup, stable_archive)
            except OSError as error:
                rollback_errors.append(
                    f"could not restore previous stable ZIP; backup retained at {backup}: {error}"
                )
        elif not candidate_promoted and backup.is_file():
            try:
                backup.unlink()
            except OSError as error:
                rollback_errors.append(f"could not remove unused ZIP backup {backup}: {error}")
        if rollback_errors:
            raise RuntimeError(
                f"Portable ZIP promotion failed ({promotion_error}); rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from promotion_error
        raise

    try:
        PENDING_BUILD_MANIFEST.unlink(missing_ok=True)
    except OSError as error:
        print(f"Warning: could not remove pending-build manifest: {error}", file=sys.stderr)
    try:
        backup.unlink(missing_ok=True)
    except OSError as error:
        print(f"Warning: could not remove previous portable ZIP: {error}", file=sys.stderr)
    print(f"Validated distributable ZIP promoted: {stable_archive}", flush=True)
    return 0


def main() -> int:
    if len(sys.argv) == 1:
        return build_candidate()
    if len(sys.argv) == 3 and sys.argv[1] == "--promote":
        return promote_candidate(sys.argv[2])
    print("Usage: build.py [--promote BUILD_ID]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
