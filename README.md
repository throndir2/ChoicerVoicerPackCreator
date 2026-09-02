# Choicer Voicer Pack Creator

A visual desktop editor for creating and modifying dub packs for *The Choicer Voicer*.

> **Unofficial community project.** This project is not affiliated with, endorsed by, or sponsored by the creators of *The Choicer Voicer*. Do not redistribute video, audio, or artwork unless you have permission to do so.

## What it does

- Creates a new project from MP4, MKV, MOV, WebM, OGV, or AVI video.
- Plays the source video inside the editor.
- Extracts and displays a zoomable waveform.
- Marks precise In/Out points in seconds.
- Adds, previews, splits, duplicates, deletes, and re-times segments.
- Resizes or collapses Pack Details, Segments, and Selected Segment so the segment list can use
	most of the sidebar when needed.
- Defines, moves, and trims ranges directly on the waveform; segment blocks also support body and
	edge dragging.
- Assigns one or more speakers and an exact performance line to every segment.
- Duplicates a segment at the same timestamp for independently recorded simultaneous speakers.
- Imports existing Choicer Voicer pack folders and preserves their prompt audio and still images.
- Lets an imported segment switch back to source-video audio when its cut needs to be regenerated.
- Saves a relocatable editable `.cvpack.json` project (media beside the project is stored by relative path).
- Atomically exports a game-ready folder and sharing ZIP.
- Validates metadata, references, inventory, PNG signatures, timestamps, codecs, complete media decoding, and ZIP CRC before publishing.

## Pack format

An exported pack contains:

```text
My Pack/
├── _pack_info.ini
├── icon.png
├── dub_video.ogv
├── _backing_track.mp3
├── 001_Speaker.mp3
├── 001_Speaker.png
├── 001_Speaker.txt
└── ...
```

Each clip metadata file is a Godot `ConfigFile` section:

```ini
[data]

caption="The line to perform."
image="001_Speaker.png"
dub_timestamps=[12.345]
dub_characters=["Speaker"]
```

`dub_timestamps` values are seconds from the start of the video. Newly generated prompts receive physical head/tail silence, and their exported timestamp is moved earlier by the head-padding amount so synchronization remains exact.

## Requirements

- Windows 10/11 x86-64 for the packaged desktop build. **FFmpeg and FFprobe are included**;
	end users do not install them separately.
- Python 3.11 or newer for development.
- Source development and tests require FFmpeg/FFprobe on `PATH`, built with `libtheora`,
	`libvorbis`, and `libmp3lame`.

## Build a portable Windows package locally

On a Windows x86-64 computer, double-click `Build-Portable.cmd`. From PowerShell, the equivalent is:

```powershell
.\Build-Portable.ps1
```

The build computer needs **64-bit Python 3.12** and an internet connection for the first build. The
script creates an isolated `.build-venv`, installs the pinned Python packaging dependencies, securely
downloads and verifies the pinned LGPL FFmpeg runtime, builds the application, and smoke-tests the
finished executable. It does not require a system FFmpeg installation. Use
`-ResetBuildEnvironment` if the isolated environment ever needs to be recreated.

The finished outputs are:

```text
dist/v0.3.0/portable-<build-id>/Choicer Voicer Pack Creator/
dist/v0.3.0/Choicer-Voicer-Pack-Creator-0.3.0-Windows-x64.zip
```

The script prints the exact generated application-folder path. Each rebuild uses a new path to avoid
a Windows executable/DLL cache issue that can affect tools replaced repeatedly at the same location;
older `portable-*` generation folders can be deleted when no copy of the app is running.

This is a **portable application folder**, not an installer. To use or share it:

1. Extract the complete ZIP; do not run the executable from inside the ZIP viewer.
2. Keep the extracted directory together, including its `bin` and `_internal` directories.
3. Run `Choicer Voicer Pack Creator.exe`.

The receiving computer does not need Python, FFmpeg, FFprobe, Godot, administrator access, or an
installation step. The folder can be moved or deleted as a unit. The app stores recent-directory and
layout preferences in the current Windows user's settings. While edits are unsaved, local
application data also contains project recovery metadata, captions, and absolute media references;
source media itself is never copied there. A folder-based package is used instead of a
self-extracting single EXE for faster startup, simpler antivirus behavior, and replaceable LGPL
FFmpeg components.

Community builds are not currently code-signed, so Windows SmartScreen may show an unrecognized-app
warning. Verify the ZIP's SHA-256 printed by the build script and use only a package from a trusted
source.

## Run from source

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m choicer_voicer_pack_creator
```

If `py` is unavailable, invoke your installed Python executable directly.

## Create a pack

1. Choose **File → New from Video**.
2. Scrub the video or click the waveform to find a line.
3. Set **In** at the beginning of the spoken line and **Out** after its final phoneme.
4. Select **Add Segment**.
5. Enter the speaker name and the exact line in the right panel.
6. Repeat for the whole video. Drag segment edges on the timeline for fine adjustments.
7. Optionally choose a clean backing track and custom icon.
8. Save the editable project.
9. Choose **Export Pack + ZIP** and select an output directory.

### Direct waveform editing

- Drag across empty waveform space to define a new In/Out range.
- Drag the cyan **IN** or orange **OUT** handle to trim the range precisely.
- Drag inside the highlighted waveform range to move it without changing its duration.
- Drag the center of an existing segment block to move that segment, or either edge to trim it.
- Press **Esc** during a drag to cancel it. The In/Out number fields remain available for exact
	entry.

For source-video prompts, a range change automatically affects the MP3 generated during the next
export. The editor does not create or destructively replace prompt MP3s while dragging. Imported or
manually selected audio files are preserved by default; after changing one of their ranges, the app
asks whether to keep that recording, regenerate from source video, or undo the range change.

When no backing track is selected, the exporter creates a duration-matched silent backing track;
it never places the source video's original voices under new recordings.

The exporter stages and validates every file before replacing an existing output. It retains the
previous folder and ZIP until the newly published copies have been hash-checked and fully validated.
A failed publication restores both previous artifacts.

## Saving and recovery

**Save Project** updates the current editable project JSON. Before replacing an existing project,
the editor retains one previous version beside it with a `.previous` suffix. **File → Restore
Previous Save** loads that version as unsaved edits, so the current saved file remains unchanged
until Save is chosen again.

**Save Project As** writes a separate project and does not modify the original project. Project
saves store edit decisions and media references; they do not rewrite imported source packs or media.

While editing, the app also writes debounced recovery snapshots to the current Windows user's local
application-data directory. After a crash or power loss, the next launch offers to recover those
edits without overwriting the saved project. A normal Save or an explicit Discard clears the
snapshot. Only one editor instance runs at a time so two projects cannot race over the recovery
journal. If a saved project becomes unreadable, opening it offers its adjacent previous version.
Export independently stages every generated file and retains rollback copies until the new folder
and ZIP pass final validation.

## Modify an existing pack

1. Choose **File → Import Existing Pack** and select the folder containing `_pack_info.ini`.
2. Existing MP3 and PNG assets are preserved by default.
3. Edit captions, speakers, or timeline positions.
4. Existing packs store a trigger timestamp and padded recording, not the original spoken cut. To
	regenerate one safely, mark the exact source-video In/Out range, click **Apply Range**, and choose
	**Yes** when asked whether to regenerate the prompt audio.
5. Save as a `.cvpack.json` project, then export.

When one recording is reused at multiple timestamps, import expands it into independent editable segments. Exporting then gives each occurrence its own triplet.

Unknown metadata fields, extra sections, and noncanonical files are reported at import. They remain
untouched in the source folder but are not silently represented as supported. The app refuses to
overwrite an imported source pack; export a canonical copy to another directory or under another
title. This protects custom extensions that the editor cannot round-trip.

If a project is moved without its media, use the **Choose** control beside Video, Backing, Audio
file, or Still image to relink the missing asset.

## Simultaneous speakers

Use **Duplicate Segment** to create a second prompt at exactly the same timestamp, then change its speaker and line. This lets each actor record independently while both performances play together. A single metadata item may also list multiple comma-separated speakers when one shared recording is desired.

## Audio and backing tracks

For newly cut segments, prompt audio comes from the source video. The exporter normalizes it and adds 150 ms head / 250 ms tail padding by default; both values are editable. Imported or manually chosen prompt files are preserved when already MP3 and converted otherwise.

A custom backing track is optional. For best results, choose music/effects audio with the original
dialogue removed. Without one, the app emits silence in the required backing-track file. It
deliberately never treats the source video's original mix as automatic backing audio.

## Validation levels

Every GUI export performs built-in fail-closed checks for:

- canonical Godot-style metadata values and CRLF line endings;
- Theora/Vorbis video, 48 kHz mono prompt MP3s, and a 44.1 kHz stereo backing MP3;
- prompt audibility, decoded duration, and physical head/tail silence;
- PNG signatures and prompt-image dimensions matching the exported video;
- exact folder inventory, full FFmpeg decodes, per-file hashes, ZIP inventory, and ZIP CRC;
- final published copies while rollback backups are still available.

Release maintainers can additionally run Godot's real `ConfigFile` parser and Xiph reference Ogg,
Vorbis, and Theora decoders:

```powershell
.\.venv\Scripts\python.exe scripts\create_validation_fixture.py build\runtime-validation
.\.venv\Scripts\python.exe scripts\validate_external.py "build\runtime-validation\output\Pack Creator Validation Fixture" --require-godot --require-xiph
```

Godot and Docker are only required for these independent release gates, not normal GUI use.

## Why Qt, FFmpeg, and Godot?

- **PySide6/Qt is the application framework.** It provides the native desktop window, controls,
	multimedia preview, background workers, and custom waveform/timeline rendering.
- **FFmpeg performs media work.** It probes source files, generates waveform samples, extracts and
	pads prompt audio, captures stills, and writes the Theora/Vorbis video required by the game.
	Windows builds bundle a pinned FFmpeg 9.0.1 x86-64 LGPL shared distribution, so users do not
	need to install it.
- **Godot is only an independent test oracle.** The Choicer Voicer is made with Godot and consumes
	pack metadata through Godot's `ConfigFile` parser. Release tests therefore ask the real parser to
	read every generated metadata file. Godot is not bundled and is not needed to run the editor.
- **Xiph tools are another release-only oracle.** They independently validate Ogg framing and fully
	decode Theora and Vorbis streams instead of relying solely on FFmpeg to validate its own output.

## Bundled FFmpeg provenance

`scripts/build.py` downloads the archive specified in
`third_party/ffmpeg-windows-x64.json`, verifies its SHA-256, extracts only the FFmpeg/FFprobe
runtime and required DLLs, and checks the required encoders before packaging. The immutable
archive is cached under `.cache/ffmpeg/` for later builds.

The selected upstream variant is **LGPL shared**, not GPL or nonfree. The output includes the exact
upstream LGPL text, build-provenance manifest, and [third-party notices](THIRD_PARTY_NOTICES.md).
The application invokes FFmpeg as a separate program and lets users replace the `bin` directory
with another compatible pair.

## Development

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -ra
.\Build-Portable.ps1
```

The Windows application folder is written below `dist/v0.3.0/portable-<build-id>/`, with a
shareable `Choicer-Voicer-Pack-Creator-0.3.0-Windows-x64.zip` beside it. The first build downloads
about 64 MiB of pinned FFmpeg input and emits a self-contained bundle. The stable sharing ZIP is not
replaced until both the application folder and a clean ZIP extraction pass packaged smoke tests.
Startup rejects a missing/mismatched tool pair or builds lacking `libtheora`, `libvorbis`, or
`libmp3lame`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for project conventions.

## License

MIT. See [LICENSE](LICENSE).
