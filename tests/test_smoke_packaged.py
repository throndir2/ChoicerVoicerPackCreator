from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_packaged.py"
SPEC = importlib.util.spec_from_file_location("smoke_packaged_under_test", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


@pytest.mark.parametrize("path_key,root_key", [("PATH", "SystemRoot"), ("Path", "SYSTEMROOT")])
def test_mcp_environment_removes_source_python_and_developer_path(
    tmp_path: Path, path_key: str, root_key: str
) -> None:
    executable = tmp_path / "portable app" / SMOKE.MCP_NAME
    environment = {
        path_key: r"C:\Source\.venv\Scripts;C:\Developer\ffmpeg",
        "PYTHONPATH": r"C:\Source\src",
        "PYTHONHOME": r"C:\Python",
        "VIRTUAL_ENV": r"C:\Source\.venv",
        "CHOICER_VOICER_SMOKE_REPORT": "report.json",
        root_key: r"D:\Windows",
        "UNCHANGED": "value",
    }

    isolated = SMOKE.mcp_environment(environment, executable)

    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & isolated.keys()
    assert "CHOICER_VOICER_SMOKE_REPORT" not in isolated
    assert isolated["PATH"].split(os.pathsep) == [
        str(executable.parent / "bin"),
        r"D:\Windows\System32",
        r"D:\Windows",
    ]
    assert "Path" not in isolated
    assert isolated["UNCHANGED"] == "value"
    assert environment["PYTHONPATH"] == r"C:\Source\src"


@pytest.mark.parametrize(
    "failure",
    [None, "missing-tool", "tool-error", "empty-help", "empty-guide", "invalid-help", "wrong-mode", "wrong-version"],
)
def test_packaged_mcp_smoke_initializes_lists_and_calls_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    executable = tmp_path / SMOKE.MCP_NAME
    calls = []

    @asynccontextmanager
    async def stdio_client(parameters):
        calls.append("start")
        assert parameters.command == str(executable)
        assert parameters.args == ["--headless"]
        assert parameters.cwd == str(executable.parent)
        assert "PYTHONPATH" not in parameters.env
        try:
            yield ("read", "write")
        finally:
            calls.append("stop")

    class Session:
        def __init__(self, read_stream, write_stream):
            assert (read_stream, write_stream) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            calls.append("close")

        async def initialize(self):
            calls.append("initialize")
            return SimpleNamespace(
                serverInfo=SimpleNamespace(name="Choicer Voicer"),
                protocolVersion="test-protocol",
            )

        async def list_tools(self):
            calls.append("list")
            names = SMOKE.REQUIRED_MCP_TOOLS - (
                {"get_help"} if failure == "missing-tool" else set()
            )
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        async def call_tool(self, name, arguments):
            calls.append("help")
            assert (name, arguments) == ("get_help", {})
            return SimpleNamespace(
                isError=failure == "tool-error",
                structuredContent=None if failure == "invalid-help" else {
                    "version": "wrong" if failure == "wrong-version" else SMOKE.APP_VERSION,
                    "mode": "live" if failure == "wrong-mode" else "headless",
                    "help": "" if failure == "empty-guide" else "The bundled help guide",
                },
                content=[
                    SimpleNamespace(
                        type="text",
                        text="" if failure == "empty-help" else "The bundled help guide",
                    )
                ],
            )

    monkeypatch.setattr(SMOKE, "stdio_client", stdio_client)
    monkeypatch.setattr(SMOKE, "ClientSession", Session)
    if failure:
        with pytest.raises(
            RuntimeError,
            match="missing tools|help failed|empty help|structured help|headless mode|application version",
        ):
            asyncio.run(SMOKE.smoke_mcp(executable, {"PYTHONPATH": "source"}))
    else:
        result = asyncio.run(SMOKE.smoke_mcp(executable, {}))
        assert result["server"] == "Choicer Voicer"
        assert result["tools"] == sorted(SMOKE.REQUIRED_MCP_TOOLS)
        assert result["mode"] == "headless"
        assert result["help_characters"] > 0
    assert calls[:3] == ["start", "initialize", "list"]
    assert calls[-2:] == ["close", "stop"]


@pytest.mark.parametrize("update_smoke", [False, True])
def test_main_checks_editor_youtube_mcp_both_separation_entrypoints_and_optional_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update_smoke: bool
) -> None:
    application = tmp_path / "portable app"
    executable = application / SMOKE.EXECUTABLE
    mcp_executable = application / SMOKE.MCP_NAME
    resources = application / "_internal" / "choicer_voicer_pack_creator" / "resources"
    for path in (
        executable,
        mcp_executable,
        application / "bin" / "ffmpeg.exe",
        application / "bin" / "ffprobe.exe",
        resources / "mcp-help.md",
        application / "licenses" / "python" / "mcp" / "METADATA.txt",
        *[
            application / "licenses" / package / "LICENSE"
            for package in ("yt-dlp", "yt-dlp-ejs", "deno")
        ],
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("smoke fixture", encoding="utf-8")
    report = {
        "ffmpeg": str(application / "bin" / "ffmpeg.exe"),
        "ffprobe": str(application / "bin" / "ffprobe.exe"),
        "version": "n9.0.1-11-ge47273f4d9-20260901",
        "analysis_manifest": str(resources / "whisper-analysis-windows-x64.json"),
        "analysis_manifest_present": True,
        "analysis_licenses_present": True,
        "whisper_runtime_build": "b4938",
        "whisper_models": ["base", "tiny"],
        "analysis_cpu_threads": 1,
        "activity_scan_regions": 1,
        "activity_scan_threshold_db": -40,
        "youtube_runtime": str(application / "_internal" / "runtime" / "deno" / "deno.exe"),
        "youtube_runtime_version": "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)",
        "youtube_ejs_present": True,
        "youtube_worker_probe_duration": 1.0,
    }
    calls = []

    def smoke_editor(command, *, env, timeout, check):
        assert command == [str(executable), "--smoke-test"]
        assert timeout == 30 and check is False
        calls.append(("editor", executable))
        Path(env["CHOICER_VOICER_SMOKE_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    async def smoke_mcp(path, environment):
        assert path == mcp_executable
        assert "CHOICER_VOICER_SMOKE_REPORT" in environment
        calls.append(("mcp", path))
        return {"mode": "headless"}

    monkeypatch.setattr(SMOKE, "ROOT", tmp_path)
    monkeypatch.setattr(
        SMOKE.sys, "argv",
        [str(SCRIPT_PATH), str(executable), *(["--update-smoke"] if update_smoke else [])],
    )
    monkeypatch.setattr(
        SMOKE, "verify_installation",
        lambda directory, version: calls.append(("verify", directory, version)),
    )
    monkeypatch.setattr(SMOKE.subprocess, "run", smoke_editor)
    monkeypatch.setattr(SMOKE, "smoke_mcp", smoke_mcp)
    monkeypatch.setattr(SMOKE, "smoke_separation", lambda path: calls.append(("separation", path)))
    monkeypatch.setattr(SMOKE, "smoke_update", lambda path: calls.append(("update", path)))

    assert SMOKE.main() == 0
    assert calls == [
        ("verify", application, SMOKE.APP_VERSION),
        ("editor", executable),
        ("mcp", mcp_executable),
        ("separation", executable),
        ("separation", mcp_executable),
        *([("update", executable)] if update_smoke else []),
    ]
