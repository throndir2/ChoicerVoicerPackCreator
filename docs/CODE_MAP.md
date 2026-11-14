# Application code map

**Status:** Implemented
**Last Updated:** 2026-11-13
**Audience:** Developers and maintainers
**Last verified:** 2026-11-13 against `5466178`
**Code:** `src/choicer_voicer_pack_creator/`, `tests/`

This map identifies the source and tests for the main application features.
The [user guide](USER_GUIDE.md) describes the user workflows.

## Application and editor

- `__main__.py` dispatches the editor, MCP server, separation worker, and update helper.
  `app.py` starts the editor. See `tests/test_app.py`.
- `ui/main_window.py` contains `MainWindow` and `ProjectEditor`.
  `ui/timeline.py` contains `TimelineWidget`. See `tests/test_workspace.py` and
  `tests/test_timeline.py`.
- `history.py` contains `ProjectHistory`. `ui/history.py` connects history to the editor.
  See `tests/test_history.py` and `tests/test_history_ui.py`.

Paths in this page are relative to `src/choicer_voicer_pack_creator/`, unless they
start with `tests/`.

## Projects and packs

- `models.py` defines `PackProject` and its segments. `project_session.py` holds
  document identity and revisions. See `tests/test_models.py` and `tests/test_workspace.py`.
- `project_io.py` contains `ProjectStore` for project files and recovery.
  See `tests/test_project_io.py`.
- `pack_io.py` contains `PackImporter`. `pack_manifest.py` handles the app export manifest.
  See `tests/test_pack_import.py`, `tests/test_pack_zip_import.py`, and
  `tests/test_pack_manifest.py`.
- `exporter.py` contains `PackExporter`. `validation.py` contains `PackValidator`.
  `media.py` provides `MediaTools` for media operations. See `tests/test_exporter.py`,
  `tests/test_export_integration.py`, and `tests/test_validation.py`.
- `scene_editing.py` plans and executes scene edits. See `tests/test_scene_editing.py`.
  `recordings.py` and `recording_renderer.py` handle recordings and rendering.
  See `tests/test_recordings.py` and `tests/test_recording_renderer.py`.

## Work and analysis

- `jobs.py` owns bounded background jobs. `operations.py` provides cancellation,
  path leases, and source snapshots. See `tests/test_jobs.py` and
  `tests/test_operations.py`.
- `analysis.py` analyzes video and audio. Caption and transcription code is in
  `captions.py`, `caption_timing.py`, and `parakeet.py`. See `tests/test_analysis.py`,
  `tests/test_captions.py`, `tests/test_caption_timing.py`, and `tests/test_parakeet.py`.
- `separation.py` and `separation_worker.py` handle backing-track separation.
  The optional BandIt runtime is in `bandit_runtime.py` and `bandit_cuda_runtime.py`.
  See `tests/test_separation.py`, `tests/test_bandit_runtime.py`, and
  `tests/test_bandit_cuda_runtime.py`.
- `youtube.py` handles YouTube imports. See `tests/test_youtube.py`.

## Automation and updates

- `mcp_server.py` exposes MCP tools. `automation.py` contains shared pack operations;
  `mcp_gui.py` connects them to the editor. `ui_automation.py` implements the optional
  UI automation bridge. See `tests/test_mcp_server.py`, `tests/test_automation.py`, and
  `tests/test_mcp_gui.py`. The [MCP guide](../MCP/README.md) describes client setup.
- `updates.py` handles portable and installer updates. `ui/update_controller.py`
  connects updates to the editor. See `tests/test_updates.py` and
  `tests/test_update_controller.py`.
