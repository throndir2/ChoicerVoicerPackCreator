from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "v0.1.0"
BUILD = ROOT / "build" / "pyinstaller-v0.1.0"
APP_NAME = "Choicer Voicer Pack Creator"


def main() -> int:
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path)
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
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
