from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
    report["mcp"] = asyncio.run(smoke_mcp(mcp_executable, environment))
    print(json.dumps(report, indent=2))
    print("PACKAGED APPLICATION + BUNDLED FFMPEG + MCP STDIO SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
