from __future__ import annotations

import asyncio
import importlib.util
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
