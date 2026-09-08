# Contributing

Contributions are welcome. This project is an unofficial community utility and is not affiliated with the creators of *The Choicer Voicer*.

## Coding agents and concurrent sessions

LLM/coding agents must follow [AGENTS.md](AGENTS.md). Each independent change uses a fresh
isolated branch/worktree, stays current with `origin/main`, and is delivered through a
submitted PR merged into `main` unless the user limits the scope or a blocker prevents
completion. Never modify another active session's checkout or work directly on `main`.

## Development setup

1. Install Python 3.11 or newer and FFmpeg with `ffmpeg` and `ffprobe` on `PATH`.
2. Create a virtual environment: `python -m venv .venv`.
3. Activate it and install: `python -m pip install -e ".[dev]"`.
4. Run `pytest` and `ruff check .` before submitting a change.

For the optional singing-preserving source backend, use Windows x64 CPython 3.11 or 3.12 and
follow [the CPU setup commands](README.md#optional-singing-preserving-cpu-backend), installing
`tools\singing-cpu-wheels.txt` with isolated pip, `--no-index --no-deps --require-hashes` before
`".[dev,singing]"` from public PyPI. Do not substitute PyPI's plain torch wheels or globally
downgrade NumPy. This backend restriction does not change the core `requires-python >=3.11`.
Portable recipients never need this source setup.

Use `.\Build-Portable.ps1 -BuildEnvironment .\build\environments\<task-name>` for a task-owned
build environment; do not reset or install into another session's environment or the shared
default during concurrent development. Reset requires this script's ownership marker and an
ordinary directory containing `pyvenv.cfg`; unmarked older environments require a new path.
The canonical script installs CPU wheels before `".[build,singing]"` and checks dependency
consistency. Reserve several GiB for the environment, staging, and clean extraction. Static
torch `.lib` archives and headers may be excluded, but never prune runtime DLLs or change
musical-band/STFT math to meet a size estimate. Preserve each pinned wheel's package-local
Intel OpenMP DLL and audit its SHA-256 against the installed wheel. Do not replace either
backend's DLL, globally preload one version, or alter automatically collected root copies
without actual frozen-dependency evidence. Never use `KMP_DUPLICATE_LIB_OK` to suppress failures.
Validate both isolated backend workers in the candidate and a clean ZIP extraction.
Worker smoke must reject importing the other heavy backend; module absence checks do not
replace actual native validation of each backend's original DLLs.

## Design rules

- Keep the main editing workspace focused on editing. Do not add persistent banners, panels,
  toolbars, or control rows for new or rarely used features. Put secondary controls, settings,
  progress breakdowns, and diagnostics in on-demand, preferably nonmodal dialogs opened from
  existing menus. If background work needs a visible indicator, use short text in the existing
  status bar and open details from there; hide it when no work or attention is pending. Do not
  automatically open detail dialogs or take space away from the video, timeline, or segment editor.
- Prefer self-explanatory labels over persistent help paragraphs. Keep decision-relevant warnings
  and errors visible; put non-obvious details in contextual tooltips or on-demand help.
- Preserve imported media unless a user explicitly replaces or regenerates it.
- Stage exports completely before replacing an existing destination.
- Keep the project JSON as the editable source of truth; generated pack files are outputs.
- Target the current project format and workflows. Do not add legacy-format migration,
	deprecated API aliases, or compatibility-only storage. Keep optional current-format fields
	usable without treating unrelated JSON as a project.
- Validate metadata references, timestamps, codecs, images, ZIP inventory, and decodeability.
- Never silently discard an unsupported field from an imported pack; report it as a warning and
	refuse in-place conversion of the source pack.
- Do not place diagnostic files in exported pack folders because the game may interpret them as clip metadata.
  The only app-specific pack file is the versioned `_cvpc_metadata.json` export manifest. Keep its
  schema independent of the exporter app version, exclude private project data, verify its file
  hashes before restoring original cuts, and preserve imported recordings and trigger alignment.
  Unsupported or mismatched manifests must produce an import warning, not silently override game metadata.

## Export resource budgets

Both the staged and published pack receive complete validation. Prompt audio statistics and
all-stream decodeability share one FFmpeg command in each pass; PCM statistics are accumulated
in bounded chunks rather than retaining a whole decoded prompt.

Explicit export work units use the shared `export_resources` budget, not automatic admission in
every media helper. On Windows, admission considers current available physical RAM, CPU affinity
and sampled CPU load, reserves editor/OS headroom, and accounts for already admitted work.
At most two units run together; constrained or unavailable telemetry selects a diagnosed,
serial one-thread baseline. Known insufficient memory still prevents admission. Waiting is
cancellable and bounded; impossible working sets and persistent pressure produce actionable
errors. FFmpeg decoder/filter/output thread overrides apply only inside an admitted unit.
Keep estimates tied to actual dimensions and buffers, release after process cleanup, and never
nest admissions. The budget is process-local: other applications are reflected in live
telemetry, but separate application processes do not share atomic reservations.

## MCP development

See [docs/MCP.md](docs/MCP.md) and the bundled
`src/choicer_voicer_pack_creator/resources/mcp-help.md` for the user-facing contract. Keep those
guides consistent with tool schemas and the standalone **Help → LLM / MCP Help** dialog.

- Use the official MCP Python SDK over stdin/stdout; send diagnostics only to stderr. Do not add
	an HTTP listener or silently attach to an unrelated running editor.
- `python -m choicer_voicer_pack_creator --mcp` and `choicer-voicer-mcp` default to launching a
	visible editor. Honor the existing single-instance lock. `--headless` must not create a
	QApplication/window and must remain independent of GUI state.
- Preserve revision checks for metadata/segment edits and explicit permission for discarding
	dirty projects, overwriting files, or downloading optional Whisper components.
- Allow incomplete draft captions/speakers, but keep export validation fail-closed. Save editable
	projects explicitly; process-local headless memory is not persistence.
- Source media is immutable. Use the same project format, prompt-audio behavior, validation, and
	transactional export path as the GUI. Do not edit the same project file concurrently.
- Treat imported content and tool output as untrusted data, not instructions. Keep the disclosure
	that media previews/tool results may be shared with the client's model provider; local ASR's
	no-upload behavior is not a promise about the assistant client.

Windows packaging generates one PyInstaller analysis/shared runtime with two entry points:
windowed `Choicer Voicer Pack Creator.exe` and console `Choicer Voicer MCP.exe`. Both must remain
in one portable folder with `_internal` and `bin`. Collect the SDK's data and runtime distribution
metadata, and preserve the generated `licenses/python/` notices for MCP and its dependencies.

`scripts/smoke_packaged.py` checks the editor and launches the **bundled** MCP executable with
`--headless` using the official SDK client: initialize, discover tools, and call `get_help`.
It strips source-Python configuration and developer tools from the child PATH. This smoke check
must not need a source interpreter in the target folder, open a port, download models, or make
network requests. The build computer still uses Python to run the smoke client. Run it against
both the candidate application folder and a clean ZIP extraction before promoting the stable ZIP;
`Build-Portable.ps1` performs both checks.
Both packaged entrypoints must also pass the offline BandIt actual-architecture tiny CPU smoke
with exact CPU dependency versions, no Qt in the worker, stereo streaming, model provenance,
and all recursive dependency notices/metadata present. Keep the existing HTDemucs assertions
unchanged. These synthetic inputs do not establish model quality or real eight-second-window
feasibility: before shipping backend changes, separately prove real-checkpoint CPU feasibility,
memory/time, and numerical parity from the candidate and clean extraction, with verified weights
and any private fixtures kept outside the portable folder. Do not overlap those heavy checks
with packaging/model loads on a memory-constrained machine. Record measured ZIP/extracted sizes;
do not present engineering size estimates as measurements.

## Pull requests

Explain the user-visible behavior, list validation performed, and include tests for format or export changes. Do not include copyrighted source video or voice assets in the repository.

Release candidates should pass `scripts/validate_external.py` with both required flags against an
exported integration fixture, in addition to lint, pytest, and packaged-application smoke tests.
The Windows packaging step must use the pinned LGPL manifest and retain all generated FFmpeg
license/provenance files. Do not substitute a GPL, nonfree, floating, or unchecked binary.

Pushes and pull requests do not run GitHub Actions. Complete all checks locally. After bumping and
committing the release version, a maintainer can manually run **Create GitHub Release** from the
Actions page. That release-only workflow builds the validated portable ZIP and attaches it, together
with a SHA-256 checksum, to a new versioned GitHub Release.

The portable build also generates `portable-files.json` after all application files have been
assembled. Keep this inventory and the exact versioned Windows ZIP/checksum asset names: the
updater uses them to verify releases and distinguish shipped files from user-owned content.
`Build-Portable.ps1` exercises the packaged updater, including its staged, Qt-free helper entry
point, waiting for the old process, preserving extra files, and restarting the updated application.
Keep `--apply-update` and `--update-result=` compatible across releases; source installations do
not self-update. Never embed a GitHub token in the application.
