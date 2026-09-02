from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = (
    ROOT
    / "dist"
    / "v0.2.2"
    / "Choicer Voicer Pack Creator"
    / "Choicer Voicer Pack Creator.exe"
)


def main() -> int:
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_EXE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    expected_bin = executable.parent / "bin"
    expected_ffmpeg = (expected_bin / "ffmpeg.exe").resolve()
    expected_ffprobe = (expected_bin / "ffprobe.exe").resolve()
    if not expected_ffmpeg.is_file() or not expected_ffprobe.is_file():
        raise RuntimeError("Packaged FFmpeg runtime is missing")

    with tempfile.TemporaryDirectory(prefix="cvpc-smoke-") as temporary:
        report_path = Path(temporary) / "report.json"
        environment = os.environ.copy()
        environment["CHOICER_VOICER_SMOKE_REPORT"] = str(report_path)
        # Prove the packaged app does not accidentally fall back to a developer's PATH copy.
        environment["PATH"] = os.pathsep.join(
            value
            for value in environment.get("PATH", "").split(os.pathsep)
            if "ffmpeg" not in value.casefold()
        )
        completed = subprocess.run(
            [str(executable), "--smoke-test"],
            env=environment,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Packaged application exited with {completed.returncode}")
        if not report_path.is_file():
            raise RuntimeError("Packaged application did not write its smoke report")
        report = json.loads(report_path.read_text(encoding="utf-8"))

    selected_ffmpeg = Path(report["ffmpeg"]).resolve()
    selected_ffprobe = Path(report["ffprobe"]).resolve()
    if selected_ffmpeg != expected_ffmpeg or selected_ffprobe != expected_ffprobe:
        raise RuntimeError(
            "Packaged application did not select its bundled FFmpeg pair: "
            f"{selected_ffmpeg}, {selected_ffprobe}"
        )
    if "n9.0.1-11-ge47273f4d9-20260901" not in report["version"]:
        raise RuntimeError(f"Unexpected bundled FFmpeg version: {report['version']}")
    print(json.dumps(report, indent=2))
    print("PACKAGED APPLICATION + BUNDLED FFMPEG SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
