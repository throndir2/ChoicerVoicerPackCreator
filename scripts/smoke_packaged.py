from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    APP_VERSION = str(tomllib.load(project_file)["project"]["version"])
DIST = ROOT / "dist" / f"v{APP_VERSION}"
LATEST_BUILD_MANIFEST = DIST / "latest-portable.json"


def default_executable() -> Path:
    if not LATEST_BUILD_MANIFEST.is_file():
        raise FileNotFoundError(LATEST_BUILD_MANIFEST)
    manifest = json.loads(LATEST_BUILD_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != APP_VERSION:
        raise RuntimeError("Latest portable-build manifest has the wrong version")
    executable = (ROOT / str(manifest["executable"])).resolve()
    try:
        executable.relative_to(DIST.resolve())
    except ValueError as error:
        raise RuntimeError("Latest portable-build executable escapes dist") from error
    return executable


def main() -> int:
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_executable()
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
    analysis_manifest = Path(report["analysis_manifest"]).resolve()
    expected_manifest = (
        executable.parent
        / "_internal"
        / "choicer_voicer_pack_creator"
        / "resources"
        / "whisper-analysis-windows-x64.json"
    ).resolve()
    if analysis_manifest != expected_manifest:
        raise RuntimeError(f"Unexpected packaged analysis manifest: {analysis_manifest}")
    if not report.get("analysis_manifest_present") or not report.get(
        "analysis_licenses_present"
    ):
        raise RuntimeError("Packaged optional-analysis provenance or licenses are missing")
    if report.get("whisper_runtime_build") != "b4938":
        raise RuntimeError("Packaged optional-analysis runtime metadata is incorrect")
    if report.get("whisper_models") != ["base", "tiny"]:
        raise RuntimeError("Packaged optional-analysis model metadata is incomplete")
    if int(report.get("analysis_cpu_threads", 0)) < 1:
        raise RuntimeError("Packaged optional-analysis hardware detection failed")
    if int(report.get("activity_scan_regions", 0)) != 1 or not isinstance(
        report.get("activity_scan_threshold_db"), (int, float)
    ):
        raise RuntimeError("Packaged deterministic activity scanning failed")
    print(json.dumps(report, indent=2))
    print("PACKAGED APPLICATION + BUNDLED FFMPEG SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
