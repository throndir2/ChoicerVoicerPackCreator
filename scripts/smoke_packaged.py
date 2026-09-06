from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from choicer_voicer_pack_creator.separation import write_json_atomic
from choicer_voicer_pack_creator.updates import (
    EXECUTABLE,
    MANIFEST,
    sha256,
    verify_installation,
    write_portable_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    APP_VERSION = str(tomllib.load(project_file)["project"]["version"])
DIST = ROOT / "dist" / f"v{APP_VERSION}"
LATEST_BUILD_MANIFEST = DIST / "latest-portable.json"
MCP_NAME = "Choicer Voicer MCP.exe"
REQUIRED_MCP_TOOLS = {
    "get_help",
    "get_project",
    "new_project",
    "open_project",
    "import_pack",
    "update_project",
    "edit_segments",
    "save_project",
    "analyze_video",
    "get_frame",
    "preview_audio",
    "preview_segment",
    "validate_project",
    "export_pack",
    "validate_pack",
    "show_in_editor",
}


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


def mcp_environment(environment: dict[str, str], executable: Path) -> dict[str, str]:
    isolated = {
        name: value
        for name, value in environment.items()
        if name.upper()
        not in {"PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CHOICER_VOICER_SMOKE_REPORT"}
    }
    # The bundled server must not depend on Python, FFmpeg, or another executable on a dev PATH.
    system_root = Path(
        next(
            (value for name, value in environment.items() if name.upper() == "SYSTEMROOT"),
            r"C:\Windows",
        )
    )
    isolated["PATH"] = os.pathsep.join(
        str(path) for path in (executable.parent / "bin", system_root / "System32", system_root)
    )
    return isolated


async def smoke_mcp(executable: Path, environment: dict[str, str]) -> dict[str, object]:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--headless"],
        env=mcp_environment(environment, executable),
        cwd=str(executable.parent),
    )
    async with asyncio.timeout(45):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                available = await session.list_tools()
                names = {tool.name for tool in available.tools}
                missing = REQUIRED_MCP_TOOLS - names
                if missing:
                    raise RuntimeError(f"Packaged MCP server is missing tools: {sorted(missing)}")
                help_result = await session.call_tool("get_help", {})
                if help_result.isError:
                    raise RuntimeError(f"Packaged MCP help failed: {help_result.content}")
                help_text = "\n".join(
                    content.text
                    for content in help_result.content
                    if content.type == "text"
                ).strip()
                if not help_text:
                    raise RuntimeError("Packaged MCP server returned empty help")
                payload = help_result.structuredContent
                if not isinstance(payload, dict):
                    raise RuntimeError("Packaged MCP server returned invalid structured help")
                if payload.get("mode") != "headless":
                    raise RuntimeError("Packaged MCP server did not enter headless mode")
                if payload.get("version") != APP_VERSION:
                    raise RuntimeError("Packaged MCP server has an unexpected application version")
                guide = payload.get("help")
                if not isinstance(guide, str) or not guide.strip():
                    raise RuntimeError("Packaged MCP server returned empty help")
                return {
                    "server": initialized.serverInfo.name,
                    "protocol_version": initialized.protocolVersion,
                    "tools": sorted(names),
                    "mode": payload["mode"],
                    "help_characters": len(guide),
                }


def smoke_separation(executable: Path) -> None:
    job = ROOT / "build" / "separation-smoke" / uuid.uuid4().hex
    job.mkdir(parents=True)
    try:
        request = job / "request.json"
        write_json_atomic(request, {"version": 1, "job_id": job.name, "smoke_test": True})
        completed = subprocess.run(
            [str(executable), "--separate-audio", str(request)],
            check=False, timeout=60,
        )
        status_path = job / "status.json"
        status = json.loads(status_path.read_text()) if status_path.is_file() else {}
        if completed.returncode != 0 or status.get("state") != "succeeded":
            raise RuntimeError(f"Packaged separation worker failed: {status}")
        report = json.loads((job / "smoke.json").read_text())
        if report != {
            "frames": 83, "sample_rate": 44100, "numpy": "2.4.6",
            "onnxruntime": "1.26.0", "soundfile": "0.13.1", "qt_imported": False,
        }:
            raise RuntimeError(f"Unexpected packaged separation runtime: {report}")
        resources = executable.parent / "_internal" / "choicer_voicer_pack_creator" / "resources"
        manifest = json.loads((resources / "backing-separation.json").read_text())
        if manifest["model"]["sha256"] != (
            "68d0bf16428ef66e692cdff8a9ccf28f1ef3f69440d57e58605a4cc55fcc5e74"
        ):
            raise RuntimeError("Packaged separation model provenance is incorrect")
        for name in ("StemSplit-MIT.txt", "Demucs-MIT.txt"):
            if not (resources / name).is_file() or not (
                executable.parent / "licenses" / name
            ).is_file():
                raise RuntimeError(f"Missing separation license: {name}")
        for package in ("onnxruntime", "numpy", "soundfile", "cffi", "pycparser",
                        "flatbuffers", "protobuf", "packaging"):
            if not any(path.is_file() for path in (
                executable.parent / "licenses" / package
            ).rglob("*")):
                raise RuntimeError(f"Missing separation dependency licenses: {package}")
        print("PACKAGED QT-FREE CPU SEPARATION + STREAMING AUDIO SMOKE PASSED")
    finally:
        shutil.rmtree(job)


def smoke_speaker_matching(executable: Path) -> None:
    job = ROOT / "build" / "speaker-smoke" / uuid.uuid4().hex
    job.mkdir(parents=True)
    try:
        report_path = job / "report.json"
        completed = subprocess.run(
            [str(executable), "--speaker-matching-smoke", str(report_path)],
            env=mcp_environment(dict(os.environ), executable),
            check=False, timeout=60,
        )
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        if completed.returncode != 0 or report != {
            "features": [1, 198, 80], "kaldi_native_fbank": "1.22.3",
            "numpy": "2.4.6", "onnxruntime": "1.26.0", "soundfile": "0.13.1",
            "qt_imported": False,
        }:
            raise RuntimeError(f"Packaged speaker-matching worker failed: {report}")
        resources = executable.parent / "_internal" / "choicer_voicer_pack_creator" / "resources"
        manifest = json.loads((resources / "speaker-matching.json").read_text())
        if manifest["model"]["sha256"] != (
            "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
        ) or manifest["model"]["bytes"] != 26530550:
            raise RuntimeError("Packaged speaker model provenance is incorrect")
        for name in ("WeSpeaker-Attribution.txt", "WeSpeaker-CC-BY-4.0.txt", "speaker-matching.json"):
            if not (resources / name).is_file() or not (
                executable.parent / "licenses" / name
            ).is_file():
                raise RuntimeError(f"Missing speaker model attribution: {name}")
        if not any(path.is_file() for path in (
            executable.parent / "licenses" / "kaldi-native-fbank"
        ).rglob("*")):
            raise RuntimeError("Missing kaldi-native-fbank license")
        if not (
            executable.parent / "licenses" / "kaldi-native-fbank" / "KaldiNativeFbank-ThirdParty.txt"
        ).is_file():
            raise RuntimeError("Missing kaldi-native-fbank native dependency notices")
        print("PACKAGED QT-FREE SPEAKER FILTERBANK + SUPERVISED WORKER SMOKE PASSED")
    finally:
        shutil.rmtree(job)


def smoke_update(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="cvpc-update-smoke-") as temporary:
        root = Path(temporary)
        target = root / "Installed app"
        directory = root / ".cvpc-update-smoke"
        staged = directory / "application"
        shutil.copytree(executable.parent, target)
        shutil.copytree(executable.parent, staged)
        # Model a previous package without requiring a historical release download.
        obsolete = target / "_internal" / "obsolete-smoke.txt"
        obsolete.write_text("previous app file", encoding="utf-8")
        write_portable_manifest(target, "0.0.0")
        extra = target / "projects" / "keep.cvpack.json"
        extra.parent.mkdir()
        extra.write_text("user-owned project", encoding="utf-8")
        report = root / "report.json"
        (directory / "plan.json").write_text(
            json.dumps({
                "target": str(target), "version": APP_VERSION, "previous_version": "0.0.0",
                "previous_manifest_hash": sha256(target / MANIFEST),
            }),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["CHOICER_VOICER_SMOKE_REPORT"] = str(report)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        # This parent holds the old EXE open without FILE_SHARE_DELETE, like the editor.
        # The staged packaged helper must wait for it to exit before replacing files.
        parent_code = """
import ctypes, sys
from ctypes import wintypes
from pathlib import Path
from choicer_voicer_pack_creator.updates import PreparedUpdate, launch_update
directory, target, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
handle = kernel.CreateFileW(str(target / "Choicer Voicer Pack Creator.exe"),
                           0x80000000, 1, None, 3, 0, None)
if handle == wintypes.HANDLE(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
launch_update(PreparedUpdate(directory, target, version), Path("--smoke-test"))
"""
        subprocess.run(
            [sys.executable, "-c", parent_code, str(directory), str(target), APP_VERSION],
            check=True, env=environment, timeout=45,
        )
        deadline = time.monotonic() + 90
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        result_path = directory / "result.json"
        if not result_path.is_file():
            raise RuntimeError(f"The packaged update helper did not finish: {directory}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result.get("success"):
            raise RuntimeError(f"Packaged update failed: {result.get('message')}")
        if not report.is_file():
            raise RuntimeError("The updated application did not restart and write its smoke report")
        restarted = json.loads(report.read_text(encoding="utf-8"))
        if Path(restarted["ffmpeg"]).resolve() != (target / "bin" / "ffmpeg.exe").resolve():
            raise RuntimeError("The updater restarted the wrong application folder")
        verify_installation(target, APP_VERSION)
        if obsolete.exists() or extra.read_text(encoding="utf-8") != "user-owned project":
            raise RuntimeError("The updater did not preserve user files/remove obsolete managed files")
        # Wait until both packaged processes release their mapped EXE/DLLs before cleanup.
        for path in (staged / EXECUTABLE, target / EXECUTABLE):
            for attempt in range(100):
                try:
                    path.unlink()
                    break
                except PermissionError:
                    if attempt == 99:
                        raise
                    time.sleep(0.1)
        print("PACKAGED IN-PLACE UPDATE + RESTART SMOKE PASSED")


def main() -> int:
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_executable()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    mcp_executable = executable.with_name(MCP_NAME)
    if not mcp_executable.is_file():
        raise FileNotFoundError(mcp_executable)
    resources = executable.parent / "_internal" / "choicer_voicer_pack_creator" / "resources"
    if not (resources / "mcp-help.md").is_file():
        raise RuntimeError("Packaged MCP help is missing")
    if not (executable.parent / "licenses" / "python" / "mcp" / "METADATA.txt").is_file():
        raise RuntimeError("Packaged MCP license metadata is missing")
    expected_bin = executable.parent / "bin"
    expected_ffmpeg = (expected_bin / "ffmpeg.exe").resolve()
    expected_ffprobe = (expected_bin / "ffprobe.exe").resolve()
    if not expected_ffmpeg.is_file() or not expected_ffprobe.is_file():
        raise RuntimeError("Packaged FFmpeg runtime is missing")
    verify_installation(executable.parent, APP_VERSION)

    scratch_root = ROOT / "build"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cvpc-smoke-", dir=scratch_root) as temporary:
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
    expected_deno = (
        executable.parent / "_internal" / "runtime" / "deno" / "deno.exe"
    ).resolve()
    if Path(report["youtube_runtime"]).resolve() != expected_deno:
        raise RuntimeError("YouTube import did not select its bundled JavaScript runtime")
    if report.get("youtube_runtime_version") != "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)":
        raise RuntimeError("Packaged YouTube JavaScript runtime did not start correctly")
    if not report.get("youtube_ejs_present"):
        raise RuntimeError("Packaged YouTube JavaScript solver files are missing")
    if report.get("youtube_worker_probe_duration") != 1.0:
        raise RuntimeError("Packaged YouTube worker could not run its isolated media check")
    for package in ("yt-dlp", "yt-dlp-ejs", "deno"):
        if not (executable.parent / "licenses" / package / "LICENSE").is_file():
            raise RuntimeError(f"Packaged {package} license notice is missing")
    report["mcp"] = asyncio.run(smoke_mcp(mcp_executable, environment))
    print(json.dumps(report, indent=2))
    print("PACKAGED APPLICATION + BUNDLED FFMPEG + MCP STDIO SMOKE PASSED")
    smoke_separation(executable)
    smoke_separation(mcp_executable)
    smoke_speaker_matching(executable)
    smoke_speaker_matching(mcp_executable)
    if "--update-smoke" in sys.argv:
        smoke_update(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
