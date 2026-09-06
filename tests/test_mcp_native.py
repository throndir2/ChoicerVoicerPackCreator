"""Real Windows desktop + SDK stdio acceptance, using only generated local media."""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from choicer_voicer_pack_creator.exporter import sha256
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore


@pytest.mark.integration
def test_native_stdio_background_projects_and_real_ui(tmp_path):
    if sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM") != "windows":
        pytest.skip("Native visible acceptance requires Windows and QT_QPA_PLATFORM=windows explicitly")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Native media acceptance requires FFmpeg and FFprobe")
    media = MediaTools()
    source = tmp_path / "synthetic.mp4"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=640x360:rate=24:duration=30", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=30", "-shortest",
        "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", str(source),
    ], "Creating original synthetic UI fixture")
    source_hash = sha256(source)
    paths = {}
    for title in ("A", "B", "C"):
        segments = [
            Segment(0.2 + index * 0.8, 0.6 + index * 0.8, f"Synthetic tone {index}", ["Test"])
            for index in range(32 if title == "A" else 1)
        ]
        project = PackProject(
            title=f"Native {title}", authors=["Synthetic fixture"],
            video_path=str(source), video_duration=30,
            video_height=360, video_fps=24, segments=segments,
        )
        path = tmp_path / f"{title}.cvpack.json"
        ProjectStore.save(project, path)
        paths[title] = path
    artifacts = Path(os.environ.get("CVPC_MCP_ARTIFACT_DIR", str(tmp_path / "artifacts")))
    artifacts.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "isolated-profile"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "choicer_voicer_pack_creator", "--mcp", "--ui-test-hooks",
              "--data-root", str(profile)],
        env={**os.environ, "QT_QPA_PLATFORM": "windows"},
    )
    recovery_identity = {}

    async def exercise():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as client,
        ):
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"start_export", "start_analysis", "get_ui_state", "ui_interact"} <= tools

            async def call(name, **args):
                result = await client.call_tool(name, args)
                assert not result.isError, (name, result.content)
                return result.structuredContent

            async def state():
                result = await call("get_ui_state")
                assert result["platform"] == "windows"
                assert result["visible"]
                assert Path(result["data_root"]) == profile
                assert result["process_id"] != os.getpid()
                assert not any(item["visible"] and item["modal"] for item in result["windows"])
                return result

            async def screenshot(name):
                result = await client.call_tool("get_ui_screenshot", {})
                assert not result.isError, result.content
                image = result.content[0]
                assert image.type == "image" and image.mimeType == "image/png"
                data = base64.b64decode(image.data)
                assert data.startswith(b"\x89PNG") and len(data) > 10000
                (artifacts / name).write_bytes(data)

            async def interact(selector, action, **args):
                accepted = await call("ui_interact", selector=selector, action=action, **args)
                assert accepted["state"] == "queued"
                with anyio.fail_after(15):
                    while True:
                        current = await state()
                        record = next(
                            item for item in current["actions"]
                            if item["action_id"] == accepted["action_id"]
                        )
                        if record["state"] == "failed":
                            await screenshot("mcp-failure.png")
                            (artifacts / "mcp-failure-state.json").write_text(
                                json.dumps(current, indent=2), encoding="utf-8"
                            )
                        assert record["state"] != "failed", record
                        if record["state"] == "completed":
                            return current
                        await anyio.sleep(0.02)

            async def tab(project_id):
                current = await state()
                tabs = next(item for item in current["widgets"] if item["selector"] == "projectTabs")
                index = next(item["index"] for item in tabs["tabs"] if item["project_id"] == project_id)
                current = await interact("projectTabs", "select", index=index)
                assert current["active_project_id"] == project_id

            async def terminal(job_id, timeout=180):
                with anyio.fail_after(timeout):
                    while True:
                        record = await call("get_job", job_id=job_id)
                        if not record["active"]:
                            return record
                        await anyio.sleep(0.05)

            help_result = await call("get_help")
            assert help_result["mode"] == "live"
            assert help_result["ui_test_hooks"] and help_result["background_jobs"]
            await state()
            projects = {
                title: await call("open_project", path=str(path)) for title, path in paths.items()
            }
            a, b, c = (projects[title] for title in ("A", "B", "C"))
            assert len({item["project_id"] for item in projects.values()}) == 3
            listed = await call("list_projects")
            assert {item["project_id"] for item in projects.values()} <= {
                item["project_id"] for item in listed["projects"]
            }
            export = await call(
                "start_export", project_id=a["project_id"], expected_revision=a["revision"],
                output_parent=str(tmp_path / "export"),
            )
            scan = await call(
                "start_analysis", project_id=b["project_id"], expected_revision=b["revision"],
            )
            assert export["active"] and scan["active"]
            queued = await call(
                "start_export", project_id=a["project_id"], expected_revision=a["revision"],
                output_parent=str(tmp_path / "export"),
            )
            assert queued["state"] in {"queued", "waiting"}
            await call("cancel_job", job_id=queued["job_id"])
            assert (await terminal(queued["job_id"]))["state"] == "cancelled"
            with anyio.fail_after(30):
                while True:
                    exporting = await call("get_job", job_id=export["job_id"])
                    assert exporting["active"], exporting
                    if exporting["state"] == "running" and exporting["message"] != "Starting":
                        break
                    await anyio.sleep(0.02)

            await tab(c["project_id"])
            editor_scroll = next(
                item for item in (await state())["widgets"]
                if item["selector"] == "projectEditorScroll"
            )
            if editor_scroll["vertical_maximum"] > 0:
                scrolled = await interact(
                    "projectEditorScrollbar", "key", project_id=c["project_id"], key="End"
                )
                assert next(
                    item for item in scrolled["widgets"] if item["selector"] == "projectEditorScroll"
                )["vertical_scroll"] > 0
                await interact(
                    "projectEditorScrollbar", "key", project_id=c["project_id"], key="Home"
                )
            await interact("projectTitle", "reveal", project_id=c["project_id"])
            await interact("projectTitle", "type", project_id=c["project_id"], text="C edited in UI")
            await interact("segmentsTable", "reveal", project_id=c["project_id"])
            await interact("segmentsTable", "select", project_id=c["project_id"], index=0)
            await interact("segmentCaption", "reveal", project_id=c["project_id"])
            await interact(
                "segmentCaption", "type", project_id=c["project_id"], text="Caption typed in visible UI"
            )
            assert (await call("get_job", job_id=export["job_id"]))["state"] == "running", (
                "Workload finished too quickly to prove editing during actual processing"
            )
            await screenshot("mcp-during-processing.png")
            during = await state()
            assert during["active_project_id"] == c["project_id"]
            (artifacts / "mcp-during-state.json").write_text(json.dumps(during, indent=2), encoding="utf-8")
            edited = await call("get_project", project_id=c["project_id"])
            assert edited["project"]["title"] == "C edited in UI"
            assert edited["segments"][0]["caption"] == "Caption typed in visible UI"
            assert edited["dirty"] and edited["revision"] != c["revision"]
            refused = await client.call_tool("update_project", {
                "project_id": c["project_id"], "expected_revision": c["revision"],
                "patch": {"title": "Stale should not apply"},
            })
            assert refused.isError
            assert (await client.call_tool("get_project", {"project_id": "missing"})).isError
            assert (await call("get_project", project_id=c["project_id"])) == edited

            await interact("saveProject", "click", project_id=c["project_id"])
            with anyio.fail_after(20):
                while (await call("get_project", project_id=c["project_id"]))["dirty"]:
                    await anyio.sleep(0.05)
            assert ProjectStore.load(paths["C"]).title == "C edited in UI"
            await tab(a["project_id"])
            await tab(b["project_id"])
            await tab(c["project_id"])
            assert (await call("get_project", project_id=a["project_id"]))["revision"] == a["revision"]
            assert (await call("get_project", project_id=b["project_id"]))["revision"] == b["revision"]

            await interact("showTasks", "click")
            task_table = next(
                item for item in (await state())["widgets"] if item["selector"] == "tasksTable"
            )
            export_row = task_table["row_ids"].index(export["job_id"])
            await interact("tasksTable", "select", index=export_row)
            await interact("taskDetails", "click")
            await interact("exportDetailsClose", "click")
            await interact("tasksWindow", "key", key="Escape")
            assert not (await call("get_job", job_id=export["job_id"]))["cancel_requested"]
            await interact("projectTitle", "reveal", project_id=c["project_id"])
            await interact("projectTitle", "click", project_id=c["project_id"])
            focus_before = (await state())["focus_selector"]
            assert focus_before == "projectTitle"

            exported = await terminal(export["job_id"])
            analyzed = await terminal(scan["job_id"])
            assert exported["state"] == "succeeded", exported
            assert analyzed["state"] == "succeeded", analyzed
            assert exported["result"]["project_id"] == a["project_id"]
            assert exported["result"]["exported_revision"] == a["revision"]
            assert analyzed["result"]["project_id"] == b["project_id"]
            assert analyzed["result"]["revision"] == b["revision"]
            assert analyzed["result"]["suggestions"]
            assert Path(exported["result"]["zip_path"]).is_file()
            assert exported["result"]["validation"]["clip_count"] == 32
            completed_state = await state()
            assert completed_state["active_project_id"] == c["project_id"]
            assert completed_state["focus_selector"] == focus_before

            cancel_export = await call(
                "start_export", project_id=a["project_id"], expected_revision=a["revision"],
                output_parent=str(tmp_path / "cancelled-export"),
            )
            with anyio.fail_after(20):
                while True:
                    running = await call("get_job", job_id=cancel_export["job_id"])
                    assert running["active"], running
                    if running["state"] == "running" and running["message"] != "Starting":
                        break
                    await anyio.sleep(0.02)
            await interact("showTasks", "click")
            task_table = next(
                item for item in (await state())["widgets"] if item["selector"] == "tasksTable"
            )
            await interact(
                "tasksTable", "select", index=task_table["row_ids"].index(cancel_export["job_id"])
            )
            await interact("taskCancel", "click")
            await interact("tasksWindow", "key", key="Escape")
            cancelled = await call("get_job", job_id=cancel_export["job_id"])
            assert cancelled["cancel_requested"]
            cancelled = await terminal(cancel_export["job_id"])
            assert cancelled["state"] == "cancelled", cancelled
            cancelled_parent = tmp_path / "cancelled-export"
            assert not cancelled_parent.exists() or not any(cancelled_parent.iterdir())
            assert (await state())["active_project_id"] == c["project_id"]
            await tab(b["project_id"])
            tabs = next(
                item for item in (await state())["widgets"] if item["selector"] == "projectTabs"
            )
            await interact("projectTabs", "close_tab", index=tabs["index"])
            with anyio.fail_after(10):
                while b["project_id"] in {
                    item["project_id"] for item in (await call("list_projects"))["projects"]
                }:
                    await anyio.sleep(0.02)
            assert (await client.call_tool("get_project", {"project_id": b["project_id"]})).isError
            reopened = await call("open_project", path=str(paths["B"]))
            assert reopened["project_id"] != b["project_id"]
            assert reopened["project"]["title"] == "Native B"
            await tab(c["project_id"])
            await interact("projectTitle", "reveal", project_id=c["project_id"])
            await interact(
                "projectTitle", "type", project_id=c["project_id"], text="C recovery draft"
            )
            recovery_identity.update(project_id=c["project_id"])
            assert (await call("get_project", project_id=c["project_id"]))["dirty"]
            await screenshot("mcp-after-processing.png")
            (artifacts / "mcp-results.json").write_text(json.dumps({
                "projects": projects, "export": exported, "analysis": analyzed,
                "queued_cancel": queued["job_id"], "running_cancel": cancelled,
                "saved_c": await call("get_project", project_id=c["project_id"]),
            }, indent=2), encoding="utf-8")

    anyio.run(exercise)
    assert (profile / "settings.ini").is_file()
    assert ProjectStore.load(paths["C"]).segments[0].caption == "Caption typed in visible UI"
    assert ProjectStore.load(paths["C"]).title == "C edited in UI"
    assert sha256(source) == source_hash

    async def recover():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client,
        ):
            await client.initialize()
            with anyio.fail_after(20):
                while True:
                    result = await client.call_tool("list_projects", {})
                    assert not result.isError, result.content
                    if recovery_identity["project_id"] in {
                        item["project_id"] for item in result.structuredContent["projects"]
                    }:
                        break
                    await anyio.sleep(0.05)
            recovered = await client.call_tool("get_project", recovery_identity)
            assert not recovered.isError, recovered.content
            assert recovered.structuredContent["dirty"]
            assert recovered.structuredContent["project"]["title"] == "C recovery draft"
            current = await client.call_tool("get_ui_state", {})
            assert not current.isError
            assert current.structuredContent["platform"] == "windows"
            assert current.structuredContent["visible"]

    anyio.run(recover)
