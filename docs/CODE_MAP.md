# Application code map

**Status:** Implemented
**Last Updated:** 2026-11-13
**Audience:** Developers and maintainers
**Last verified:** 2026-11-13 against `25a1d36`
**Code:** `src/choicer_voicer_pack_creator/`, `tests/`

This map identifies the source and tests for the main application features.
The [user guide](USER_GUIDE.md) describes the user workflows.

## Application and editor

- `__main__.py` dispatches the editor, MCP server, separation worker, and update helper.
  `app.py` starts the editor. See `tests/test_app.py`.
- `ui/main_window.py` contains `MainWindow` and `ProjectEditor`.
  `ui/timeline.py` contains `TimelineWidget`, which handles waveform ranges, segment
  selection, edge dragging, panning, and drag scrolling.
  See `tests/test_workspace.py` and `tests/test_timeline.py`.
- `history.py` contains `ProjectHistory`. `ui/history.py` connects history to the editor.
  Timeline dragging becomes one undoable project edit; canceling the drag adds no history.
  See `tests/test_history.py` and `tests/test_history_ui.py`.

Paths in this page are relative to `src/choicer_voicer_pack_creator/`, unless they
start with `tests/`.

## Projects and packs

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

### Editable segments and document revisions

`models.py` defines `PackProject` and `Segment`, including editable captions, speakers,
timings, and media references. It serializes the supported project schema.
`project_session.py` holds each open document's identity, project, path, and revision counters.
Its dirty state compares the current revision with the saved revision.
Its snapshot makes an independent project copy.

`automation.py` exposes `ProjectSnapshot` and `require_revision` for revision-checked edits.
`mcp_gui.py` connects these operations to live documents.
The process-local document identity differs from the persistent pack identity.
See `tests/test_models.py`, `tests/test_workspace.py`, `tests/test_automation.py`, and
`tests/test_mcp_gui.py` for serialization, independent documents, and stale-edit rejection.

## Work and analysis

- `jobs.py` owns bounded background jobs. `operations.py` provides cancellation,
  path leases, and source snapshots. See `tests/test_jobs.py` and
  `tests/test_operations.py`.
- `analysis.py` analyzes video and audio. See `tests/test_analysis.py`.
- `youtube.py` handles YouTube imports. See `tests/test_youtube.py`.

### Caption alignment and local recognition

`captions.py` handles source captions. `caption_timing.py` implements caption alignment:
`correct_caption_timings` takes source captions, word evidence, duration, and a cancellation check.
It returns a caption-preserving timing draft and flags uncertain corrections.
`audit_caption_text` compares proposed text with independent recognition of the cut.
Agreement does not prove that the cut contains every speech sound.
See `tests/test_captions.py` and `tests/test_caption_timing.py`.

`parakeet.py` owns `ParakeetManager`, the verified model cache and local Parakeet recognition requests.
It takes audio, language, hardware, progress, and cancellation inputs and returns transcript suggestions.
Recognition runs in a separate worker process.
See `tests/test_parakeet.py` for model verification, chunk timings, and transcript ranges.

### BandIt runtime isolation and CPU fallback

`separation.py` owns `SeparationManager`, which verifies models, reserves resources, launches
isolated workers, and publishes verified backing audio. `separation_worker.py` dispatches worker requests.
`bandit_runtime.py` implements the singing-preserving inference and streaming audio operations.
It defines the CPU and GPU host-memory reservations.
`bandit_cuda_runtime.py` installs and verifies the separately consented NVIDIA runtime cache
and isolates its Python packages and DLLs inside the worker.

If CUDA host-resource admission fails, the manager reports the reason and requests the CPU budget.
A recoverable GPU worker failure permits one fresh CPU retry after worker cleanup.
It discards partial audio and resets the time estimate; cancellation does not start fallback.
See `tests/test_separation.py`, `tests/test_bandit_runtime.py`, and
`tests/test_bandit_cuda_runtime.py` for cancellation, streaming, cache consent, verification, and isolation.
These tests do not establish real GPU performance or hardware qualification.

## Automation and updates

- `mcp_server.py` exposes MCP tools. `automation.py` contains shared pack operations;
  `mcp_gui.py` connects them to the editor. `ui_automation.py` implements the optional
  UI automation bridge. See `tests/test_mcp_server.py`, `tests/test_automation.py`, and
  `tests/test_mcp_gui.py`. The [MCP guide](../MCP/README.md) describes client setup.
- `updates.py` handles portable and installer updates. `ui/update_controller.py`
  connects updates to the editor. See `tests/test_updates.py` and
  `tests/test_update_controller.py`.
