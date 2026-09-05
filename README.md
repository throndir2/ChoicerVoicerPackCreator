# Choicer Voicer Pack Creator

A visual desktop editor for creating and modifying dub packs for *The Choicer Voicer*.

> **Unofficial community project.** This project is not affiliated with, endorsed by, or sponsored by the creators of *The Choicer Voicer*. Do not redistribute video, audio, or artwork unless you have permission to do so.

## What it does

- Creates a new project from MP4, MKV, MOV, WebM, OGV, or AVI video.
- Downloads a single YouTube video from its URL and offers YouTube and local Whisper transcripts
  side by side, each with its own text and timings.
- Plays the source video inside the editor.
- Keeps the decoded video frame visible when seeking while playback is stopped.
- Cues playback to a segment's In point whenever that segment is selected.
- Extracts and displays a zoomable waveform.
- Marks precise In/Out points in seconds.
- Adds, previews, splits, duplicates, deletes, and re-times segments.
- Resizes or collapses Pack Details, Segments, and Selected Segment so the segment list can use
	most of the sidebar when needed.
- Defines, moves, and trims ranges directly on the waveform; segment blocks also support body and
	edge dragging.
- Highlights substantial, non-identical segment overlaps for deterministic human review.
- Offers a one-time initial scan that proposes editable ranges from deterministic audio activity.
- Optionally downloads a pinned local Whisper CPU runtime/model to draft captions and timestamps;
	source audio and transcripts are not uploaded.
- Assigns one or more speakers and an exact performance line to every segment.
- Duplicates a segment at the same timestamp for independently recorded simultaneous speakers.
- Imports existing Choicer Voicer pack folders and preserves their prompt audio and still images.
- Lets an imported segment switch back to source-video audio when its cut needs to be regenerated.
- Saves a relocatable editable `.cvpack.json` project (media beside the project is stored by relative path).
- Atomically exports a game-ready folder and sharing ZIP.
- Validates metadata, references, inventory, PNG signatures, timestamps, codecs, complete media decoding, and ZIP CRC before publishing.
- Checks public GitHub releases and offers verified, in-place Windows updates without replacing
  projects, media, or unrelated files.

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
script creates an isolated environment under the current user's local application data, installs
the pinned Python packaging dependencies, securely
downloads and verifies the pinned LGPL FFmpeg runtime, builds the application, and smoke-tests the
finished executable. It does not require a system FFmpeg installation. Use
`-ResetBuildEnvironment` if the isolated environment ever needs to be recreated.

The finished outputs are:

```text
dist/v0.5.1/portable-<build-id>/Choicer Voicer Pack Creator/
dist/v0.5.1/Choicer-Voicer-Pack-Creator-0.5.1-Windows-x64.zip
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

### Application updates

Portable Windows builds check the public
[GitHub releases](https://github.com/throndir2/ChoicerVoicerPackCreator/releases) in the background
on startup. **Prereleases are included by default**, since the current releases use that channel.
The **Help** menu provides **Check for Updates**, **Check for Updates on Startup**, and
**Include Prereleases**. An offline or rate-limited automatic check reports its failure in the
status bar without interrupting your work. Checks contact GitHub, not your media or project files.

When a newer compatible release is found, you can decline it or download its Windows x64 ZIP.
Downloads are cancelable and checked against the release's SHA-256 checksum and GitHub asset
digest when available. After the download is verified, a separate confirmation offers a restart.
The normal Save / Discard / Cancel prompt still protects unsaved edits; active exports and
import/analysis dialogs must finish first. The app restarts in the same folder and reopens the
saved project.

Each new portable package includes `portable-files.json`, an inventory of shipped files. The
updater replaces only those files and removes obsolete inventoried files. Extra files, projects,
media, Windows preferences, recovery data, and downloaded Whisper components are not removed.
Missing or modified application files (including custom FFmpeg binaries), path conflicts,
links/junctions, locked files, and permission problems prevent an unsafe replacement. The app
offers the GitHub release page when preparation fails. For a manual update, extract the new
release into a separate folder. The updater never requests administrator access or
mirrors/deletes your application folder.

Updates are staged beside the application and installed by a separate process after the editor
exits. Previous application files are retained as rollback backups until installation succeeds.
A failed replacement attempts to restore them and reports any incomplete rollback, retaining the
staging/backup folder for recovery. If an update is interrupted by a power loss or the updater is
forcibly terminated, keep that `.cvpc-update-*` folder and restore from its `backup` directory or
extract a fresh release into a separate folder. Do not delete backups until the app works again.

**Existing packages without an updater/inventory need one manual upgrade** to a release containing
this feature. Extract that release into a new folder rather than merging it over an old package.
Source checkouts can check releases manually but are never modified by the updater. Release
checksums verify integrity, not publisher identity; continue to use only the official repository,
as these builds are not code-signed.

## Run from source

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m choicer_voicer_pack_creator
```

If `py` is unavailable, invoke your installed Python executable directly.

## Create a pack

1. Choose **File → New from Video**.
2. Optionally use the initial analysis window to scan for possible lines and draft local Whisper
	captions, then check only the suggestions you want to add.
3. Assign a speaker and verify every suggested caption and boundary against the source video.
4. Scrub the video or click the waveform to find any remaining line.
5. Set **In** at the beginning of the spoken line and **Out** after its final phoneme.
6. Select **Add Segment**.
7. Enter the speaker name and the exact line in the right panel.
8. Repeat for the whole video. Drag segment edges on the timeline for fine adjustments.
9. Optionally choose a clean backing track and custom icon.
10. Save the editable project.
11. Choose **Export Pack + ZIP** and select an output directory.

### Analyze and transcribe a video

The initial analysis window opens after **New from Video** and can be reopened with **Tools →
Analyze Video & Suggest Segments** (`Ctrl+Shift+R`). It always offers a dependency-free activity
scan with Balanced, Sensitive, and Conservative modes. This scan measures deterministic audio
energy and suggests possible regions; music, effects, and silence changes can still be false
positives.

Local transcription is optional. On first use, the app asks before downloading:

- an official, pinned whisper.cpp 1.9.3 Windows x64 CPU runtime (about 8 MiB); and
- either the multilingual Tiny model (about 74 MiB) or Base model (about 141 MiB).

The app recommends Base when at least 2 GiB of physical memory is currently available and Tiny on
more constrained systems. The
CPU runtime automatically selects an optimized instruction-set backend and does not require CUDA.
Every download uses an immutable upstream revision and is checked for exact size and SHA-256 before
use. Components are cached in the current user's local application-data directory, while temporary
analysis audio and transcript JSON are deleted after each scan.

Results list editable In/Out values and draft captions. Double-click a row or use **Preview Row** to
audition it, uncheck unwanted rows, correct text, then add the checked suggestions. Existing segments
are never replaced. Suggested segments intentionally have no speaker; assign speakers manually.
Whisper is probabilistic and can mishear names, stylized vocalizations, non-English speech, music,
or overlapping speakers, so every result remains review evidence rather than authoritative data.
One language selection applies to an entire scan; use Auto-detect for primarily single-language
sources, or rerun selected-language passes when a video deliberately mixes languages. Long videos
also receive conservative free-disk and memory checks before analysis begins.

### Import a YouTube video

Choose **File → New from YouTube**, paste a video URL, and choose where to keep the downloaded
media. Only download material you own or have permission to use. Each import creates a new
`YouTube-<video-id>-<unique-id>` folder without replacing existing files. Save your `.cvpack.json`
beside that folder to keep the project relocatable; downloaded media is not a temporary cache.
The downloader retrieves the best available video/audio and may merge them into MKV.

YouTube can provide timestamped creator captions or automatic speech-recognition captions.
The importer prefers creator captions in the selected language and otherwise uses available
automatic captions. **Original language (auto)** uses YouTube's language metadata where available;
you can also select or type a language code. YouTube-generated translations are excluded, but
creator-uploaded tracks can themselves be translations. Some videos have no accessible captions,
and caption delivery can fail independently of the video download.

Available captions immediately populate the **YouTube text + timings** panel. Whisper starts
automatically on a background thread, asking permission first if its runtime/model must be
downloaded. Its result appears alongside YouTube in the **Whisper text + timings** panel. Each
transcript keeps its own row count, text, and In/Out boundaries: a longer Whisper passage is not
forced onto shorter YouTube captions or flagged as a conflict. You can edit, preview, and uncheck
rows in either draft independently.

Choose the preferred source, then click **Use YouTube Transcript** or **Use Whisper Transcript**.
The checked rows from that source become editable project segments with that source's timings
and no assigned speakers. The other source is not mixed in. Choosing YouTube before Whisper
finishes stops the scan; otherwise, wait to review both. Existing project segments are never
silently replaced; adding another set requires confirmation.

If captions are unavailable, the same automatic Whisper pass drafts suggestions from the audio.
Declining setup or a failed/canceled scan leaves downloaded media and available captions intact.
**Cancel Scan** keeps the analysis window open. **Keep Drafts & Close** stops a running scan and
retains both available drafts, including edits, checked rows, and source selection, without adding
segments. Draft changes are included in recovery snapshots and **Save Project**, just like other
project edits. **Tools → Analyze Video & Suggest Segments** restores the saved drafts without
rerunning Whisper. A successful rescan replaces only the local draft after confirmation; a failed
or canceled rescan keeps its previous result. Original YouTube caption evidence and the source URL
are also retained. Replacing or clearing the source video clears its caption evidence and drafts.

The portable build includes pinned yt-dlp, its JavaScript solver, and Deno, and uses its existing
FFmpeg tools; no extra end-user installation is required. YouTube import contacts YouTube and its
media servers, but local Whisper does not upload your audio or captions. Playlists, live/upcoming
streams, and age-/sign-in-restricted videos are unsupported. Downloads may fail because of
availability, rate limits, or upstream changes; the importer does not use browser cookies or
attempt access-restriction workarounds. An authorized local copy can still be opened with
**New from Video**. Downloader/solver updates ship with application updates.

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

### Deterministic review assistance

The editor highlights pairs of segments that overlap by more than 125 ms and lists the exact overlap
duration in the status tooltip. Exact duplicate ranges with disjoint speaker assignments remain
unflagged because they intentionally support simultaneous speakers; duplicates that repeat any
speaker are flagged. These amber notices are review evidence, not export-blocking errors; the tool
never moves a range automatically.

Experimental Silero VAD, OCR, source separation, and game-dialogue lookup tools were evaluated but
are not treated as correctness oracles. Their existing results varied with model settings, merged
speakers, missed stylized vocalizations, or produced timing that did not match the spoken cut.
Whisper was integrated only as an explicitly optional local drafting tool with pinned provenance;
it never assigns speakers or silently changes project data. Exact lines still require human review
against the decoded source.

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

The Windows application folder is written below `dist/v0.5.1/portable-<build-id>/`, with a
shareable `Choicer-Voicer-Pack-Creator-0.5.1-Windows-x64.zip` beside it. The first build downloads
about 64 MiB of pinned FFmpeg input and emits a self-contained bundle. The stable sharing ZIP is not
replaced until both the application folder and a clean ZIP extraction pass packaged smoke tests.
Startup rejects a missing/mismatched tool pair or builds lacking `libtheora`, `libvorbis`, or
`libmp3lame`.

## GitHub releases

This repository intentionally has **no automatic push or pull-request CI**. Pushing commits does not
start an Actions runner. Tests and validation are run locally during development.

The only GitHub Actions workflow is **Create GitHub Release**, and it runs solely when a maintainer
manually selects **Actions → Create GitHub Release → Run workflow**. It reads the version from
`pyproject.toml`, builds and smoke-tests the portable Windows package, creates tag `v<version>`, and
publishes a GitHub Release containing the ZIP and its SHA-256 checksum. Running it again without
bumping the project version fails before the expensive build rather than replacing an existing
release or tag. Releases are restricted to the canonical `main` branch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for project conventions.

## License

MIT. See [LICENSE](LICENSE).
