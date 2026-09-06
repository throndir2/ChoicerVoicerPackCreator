from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import wave
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from choicer_voicer_pack_creator.automation import HeadlessProjectAccess, PackAutomation
from choicer_voicer_pack_creator.mcp_server import create_server
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.project_io import ProjectStore


def test_tool_schemas_errors_and_headless_state(tmp_path):
    automation = PackAutomation(HeadlessProjectAccess(), tmp_path)
    server = create_server(automation)

    async def exercise():
        async with create_connected_server_and_client_session(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(tools) == 16
            assert tools["get_project"].annotations.readOnlyHint
            assert "expected_revision" in tools["edit_segments"].inputSchema["required"]
            assert tools["update_project"].inputSchema["$defs"]["ProjectPatch"]["additionalProperties"] is False
            help_result = await client.call_tool("get_help", {})
            assert not help_result.isError
            assert help_result.structuredContent["mode"] == "headless"
            resources = await client.list_resources()
            assert str(resources.resources[0].uri) == "choicer-voicer://help"
            assert (await client.read_resource("choicer-voicer://help")).contents
            before = (await client.call_tool("get_project", {})).structuredContent
            bad = await client.call_tool("update_project", {
                "expected_revision": before["revision"], "patch": {"video_fps": 0},
            })
            assert bad.isError
            unchanged = (await client.call_tool("get_project", {})).structuredContent
            assert before == unchanged
            assert (await client.call_tool("show_in_editor", {})).isError
            assert (await client.call_tool("get_project", {"limit": 0})).isError
            results = []

            async def competing_edit(title):
                results.append(await client.call_tool("update_project", {
                    "expected_revision": before["revision"], "patch": {"title": title},
                }))

            async with anyio.create_task_group() as group:
                group.start_soon(competing_edit, "First writer")
                group.start_soon(competing_edit, "Second writer")
            assert sum(result.isError for result in results) == 1

    anyio.run(exercise)


@pytest.fixture
def synthetic_video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    media = MediaTools()
    source = tmp_path / "source.mp4"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=320x180:r=12:d=2", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=2", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], "Creating MCP fixture")
    return source


@pytest.mark.integration
@pytest.mark.parametrize("live", [False, True], ids=["headless", "live-editor"])
def test_real_stdio_client_creates_reviews_exports_and_reimports_pack(tmp_path, synthetic_video, live):
    app_data = tmp_path / "app-data"
    arguments = ["-m", "choicer_voicer_pack_creator", "--mcp", "--headless"]
    if live:
        arguments = [
            "-c",
            "import sys; from PySide6.QtCore import QStandardPaths; "
            "QStandardPaths.writableLocation = staticmethod(lambda _: sys.argv[1]); "
            "from choicer_voicer_pack_creator.app import main; "
            "raise SystemExit(main(['test', '--mcp']))",
            str(app_data),
        ]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=arguments,
        env={**os.environ, "LOCALAPPDATA": str(app_data)},
    )

    async def exercise():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client,
        ):
            await client.initialize()

            async def call(name, **arguments):
                result = await client.call_tool(name, arguments)
                assert not result.isError, result.content
                return result.structuredContent

            assert (await call("get_help"))["mode"] == ("live" if live else "headless")
            created = await call(
                "new_project", video_path=str(synthetic_video), title="MCP Fixture", authors=["Tester"]
            )
            updated = await call(
                "update_project", expected_revision=created["revision"],
                patch={"video_height": 180, "video_fps": 12},
            )
            edited = await call(
                "edit_segments", expected_revision=updated["revision"],
                upsert=[{"start": 0.2, "end": 0.8, "caption": "Synthetic tone", "characters": ["Test"]}],
            )
            segment_id = edited["changed_ids"][0]
            if live:
                assert (await call("show_in_editor", segment_id=segment_id))["status"] == "shown"
            frame = await client.call_tool("get_frame", {"timestamp": 0.5})
            assert not frame.isError
            assert base64.b64decode(frame.content[0].data).startswith(b"\x89PNG")
            for name, arguments in (
                ("preview_audio", {"start": 0.2, "end": 0.8}),
                ("preview_segment", {"segment_id": segment_id}),
            ):
                audio = await client.call_tool(name, arguments)
                assert not audio.isError
                with wave.open(io.BytesIO(base64.b64decode(audio.content[0].data))) as wav:
                    assert wav.getnframes() > 0
                    assert wav.getframerate() == 16000
            analysis = await call("analyze_video")
            assert analysis["suggestions"]
            assert (await call("get_project"))["total_segments"] == 1
            assert (await call("validate_project"))["valid"]
            saved = await call(
                "save_project", expected_revision=edited["revision"],
                path=str(tmp_path / "fixture.cvpack.json"),
            )
            exported = await call(
                "export_pack", output_parent=str(tmp_path / "output"),
                expected_revision=saved["revision"],
            )
            assert Path(exported["zip_path"]).is_file()
            assert exported["validation"]["clip_count"] == 1
            assert len(exported["file_hashes"]) == 7
            refused = await client.call_tool("export_pack", {
                "output_parent": str(tmp_path / "output"), "expected_revision": saved["revision"],
            })
            assert refused.isError
            validated = await call(
                "validate_pack", folder=exported["pack_path"], zip_path=exported["zip_path"]
            )
            assert validated["zip_valid"]
            imported = await call("import_pack", path=exported["pack_path"])
            assert imported["segments"][0]["audio_mode"] == "file"
            assert not imported["segments"][0]["source_range_known"]
            assert (await call("get_project"))["dirty"]

    anyio.run(exercise)
    assert ProjectStore.load(tmp_path / "fixture.cvpack.json").segments[0].caption == "Synthetic tone"
    if live:
        assert (app_data / "recovery-v2.json").is_file()


def test_headless_cli_exits_on_stdin_eof_without_gui(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "choicer_voicer_pack_creator", "--mcp", "--headless"],
        input="", text=True, capture_output=True, timeout=20,
        env={**os.environ, "QT_QPA_PLATFORM": "invalid-test-platform", "LOCALAPPDATA": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_live_cli_exits_on_stdin_eof(tmp_path):
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from PySide6.QtCore import QStandardPaths; "
            "QStandardPaths.writableLocation = staticmethod(lambda _: sys.argv[1]); "
            "from choicer_voicer_pack_creator.app import main; "
            "raise SystemExit(main(['test', '--mcp']))",
            str(tmp_path),
        ],
        input="", text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
