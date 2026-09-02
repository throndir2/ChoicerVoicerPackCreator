from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from ffmpeg_bundle import prepare_bundle

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "v0.2.2"
BUILD = ROOT / "build" / "pyinstaller-v0.2.2"
APP_NAME = "Choicer Voicer Pack Creator"
FFMPEG_STAGE = ROOT / "build" / "ffmpeg-windows-x64-562ea50b4f2d213e"


def main() -> int:
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path)
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
        str(DIST),
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
        str(ROOT / "src" / "choicer_voicer_pack_creator" / "__main__.py"),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        return completed.returncode

    app_dir = DIST / APP_NAME
    shutil.copytree(FFMPEG_STAGE / "bin", app_dir / "bin", dirs_exist_ok=True)
    shutil.copytree(FFMPEG_STAGE / "licenses", app_dir / "licenses", dirs_exist_ok=True)
    shutil.copy2(FFMPEG_STAGE / "THIRD_PARTY_NOTICES.md", app_dir / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "LICENSE", app_dir / "LICENSE.txt")
    shutil.copy2(ROOT / "README.md", app_dir / "README.md")
    shutil.copy2(
        FFMPEG_STAGE / "licenses" / "FFmpeg-LGPL-3.0.txt",
        app_dir / "licenses" / "LGPL-3.0.txt",
    )
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
    archive = DIST / "Choicer-Voicer-Pack-Creator-0.2.2-Windows-x64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                package.write(path, f"{APP_NAME}/{path.relative_to(app_dir).as_posix()}")
    with zipfile.ZipFile(archive) as package:
        bad_member = package.testzip()
        if bad_member:
            print(f"Generated ZIP failed CRC validation: {bad_member}", file=sys.stderr)
            return 1
    print(f"Bundled application with FFmpeg: {app_dir}", flush=True)
    print(f"Distributable ZIP: {archive}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
