# Choicer Voicer Pack Creator

A visual desktop editor for creating and modifying dub packs for *The Choicer Voicer*.

> **Unofficial community project.** This project is not affiliated with, endorsed by, or sponsored by the creators of *The Choicer Voicer*. Do not redistribute video, audio, or artwork unless you have permission to do so.

## What it does

- Creates a new project from MP4, MKV, MOV, WebM, OGV, or AVI video.
- Keeps multiple projects in independent tabs, with a shared dockable **Tasks** panel.
- Downloads a single YouTube video from its URL and offers YouTube and local Whisper transcripts
  side by side, each with its own text and timings.
- Plays the source video inside the editor. Press **Space** in the video preview, timeline,
  or segment list to play/pause; spaces still work normally when editing text.
- Overlays each segment's line during playback with its speaker(s) above it. Subtitles update
  as you edit, follow seeking and segment timings, and work with saved projects and imported packs.
  Overlapping lines are shown together; source video and exports are not changed.
- Keeps the decoded video frame visible when seeking while playback is stopped.
- Cues playback to a segment's In point when you select or click it in the list or timeline,
  including another click on the selected segment. Scrub the playhead or click the waveform
  to seek to an exact point instead.
- Automatically selects and scrolls to each active segment during video playback, keeping the
  Selected Segment editor in sync without interrupting playback. Gaps keep the current selection;
  overlapping lines follow the most recently started segment. Paused editing and single-segment
  previews keep their selection.
- Extracts and displays a zoomable waveform.
- Marks precise In/Out points in seconds.
- Adds, previews, splits, combines, duplicates, deletes, and re-times segments.
  Press **Backspace** (or **Ctrl+Delete**) to delete the selected segment after confirmation;
  Backspace still works normally when editing text or numbers.
- Resizes or collapses Pack Details, Segments, and Selected Segment so the segment list can use
	most of the sidebar when needed.
- Freely shrinks or fully collapses the video/timeline pane; drag the thin divider back to reopen it.
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
- Reopens the last 10 saved or opened projects from **File > Open Recent**, even after restarting.
- Atomically exports a game-ready folder and sharing ZIP.
- Validates metadata, references, inventory, PNG signatures, timestamps, codecs, complete media decoding, and ZIP CRC before publishing.
- Lets an MCP-compatible assistant work with a live visible editor, or an explicitly headless
	project, using local stdio tools for media review, editing, saving, and validated export.
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
finished editor and console MCP executables, including an official-SDK stdio handshake.
It does not require a system FFmpeg installation. Use
`-ResetBuildEnvironment` if the isolated environment ever needs to be recreated.

The finished outputs are:

```text
dist/v1.0.0/portable-<build-id>/Choicer Voicer Pack Creator/
dist/v1.0.0/Choicer-Voicer-Pack-Creator-1.0.0-Windows-x64.zip
```

The script prints the exact generated application-folder path. Each rebuild uses a new path to avoid
a Windows executable/DLL cache issue that can affect tools replaced repeatedly at the same location;
older `portable-*` generation folders can be deleted when no copy of the app is running.

This is a **portable application folder**, not an installer. To use or share it:

1. Extract the complete ZIP; do not run the executable from inside the ZIP viewer.
2. Keep the extracted directory together, including both EXEs and its `bin` and `_internal`
	directories.
3. Run `Choicer Voicer Pack Creator.exe`.

`Choicer Voicer MCP.exe` is the separate **console** entry point for assistant clients; the normal
editor EXE stays windowed. Both share one bundled runtime. Let an MCP client launch the console
executable rather than double-clicking it.

The receiving computer does not need Python, FFmpeg, FFprobe, Godot, administrator access, or an
installation step. The folder can be moved or deleted as a unit. The app stores recent-project,
recent-directory, and layout preferences in the current Windows user's settings. While edits are unsaved, local
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
on startup. **Prereleases are included by default**, alongside stable releases.
The **Help** menu provides **Check for Updates**, **Check for Updates on Startup**, and
**Include Prereleases**. An offline or rate-limited automatic check reports its failure in the
status bar without interrupting your work. Checks contact GitHub, not your media or project files.

When a newer compatible release is found, you can decline it or download its Windows x64 ZIP.
Downloads are cancelable and checked against the release's SHA-256 checksum and GitHub asset
digest when available. After the download is verified, a separate confirmation offers a restart.
Save / Discard / Cancel decisions protect every dirty project, not just the selected tab.
Active tasks must finish or stop cooperatively before exit. The app restarts in the same folder
and restores its workspace list, including independent unsaved recovery records.

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

## Use an LLM / MCP assistant

Open **Help → LLM / MCP Help** for a copyable client configuration and an offline safety guide.
See [docs/MCP.md](docs/MCP.md) for portable/source configuration, VS Code's configuration shape,
tool examples, and troubleshooting.

- The client starts/stops a **local stdio** server; there is no HTTP port or separate daemon.
- **Live editor is the default.** The server opens a visible editor automatically. Save and close
	an already-running editor before connecting: it does not attach to that window, and the
	single-instance lock rejects a second visible editor.
- Opt into `--headless` for an independent in-memory project with no QApplication/window.
	Save explicitly before disconnecting, and never edit the same project file concurrently.
- Source entry points are `python -m choicer_voicer_pack_creator --mcp` and
	`choicer-voicer-mcp`; packaged clients use the sibling `Choicer Voicer MCP.exe`.
- **Preview audio/images and other tool results may be sent to your client's model provider.**
	Local stdio is not a local-only AI guarantee. Unlike optional local ASR, assistant previews
	can leave the machine.

Assistant output is review evidence, not authoritative captions, speaker identity, or timing.
Review against the source and respect media permissions and author credits. The editor supports
YouTube import and local backing-track separation, but these MCP tools do not expose those
workflows yet. Use the editor for those operations, then save and open the project through MCP,
or reference prepared local assets. The MCP tools also do not provide OCR or wiki/dialogue search.

## Create a pack

1. Choose **File → New from Video**.
2. Once the source is ready, start editing immediately. Waveform, backing generation, and local
   analysis run as independent background tasks. A shared nonmodal prompt combines requests for
   missing runtime/model versions across projects. Declining keeps your media and drafts; other
   workflows continue. Review and explicitly use only the suggestions you want to add.
3. Assign a speaker and verify every suggested caption and boundary against the source video.
4. Scrub the video or click the waveform to find any remaining line.
5. Set **In** at the beginning of the spoken line and **Out** after its final phoneme.
6. Select **Add Segment**.
7. Enter the speaker name and the exact line in the right panel.
8. Repeat for the whole video. Drag segment edges on the timeline for fine adjustments.
9. Review the generated backing, or choose a custom clean backing track and icon.
10. Save the editable project.
11. Choose **Export Pack + ZIP** and select an output directory.

**File > Open Recent** remembers the last 10 successfully opened or saved projects, newest first.
Opening or saving the same project moves it to the top without duplicates; **Save Project As**
also remembers the new copy. Entries show the filename and folder so similarly named projects
can be distinguished. Unsaved new projects, source videos, and imported packs are not added until
saved as an editable project. **Clear Recent Projects** clears only the list, not any files.
If a project has moved or is unavailable, reopening it reports the usual error; use **Open Project**
to find its new location. An already-open project focuses its tab. Opening another project never
asks you to discard unrelated edits.

### Project workspace and background tasks

Each tab owns its project, selected segment, playback position, range, zoom, analysis drafts, and
dirty state. Switching tabs pauses the previous audible preview without cancelling processing.
The tab's `*`, `[working]`, and `[!]` indicators show unsaved edits, work in progress, and errors.
Completion does not select a different tab.

**Tools > Tasks** shows the shared dock. Filter it to all projects or the current project, inspect
stage progress and elapsed time, cancel supported work, reopen review/details, or open a successful
output. CPU, I/O, and network budgets bound concurrent work; jobs sharing output files or inference
components wait rather than overwrite each other. You can edit or save another project while a
pack exports, a source opens, or analysis runs.

**Retry** is enabled for failed/canceled analysis, refinement, YouTube imports, exports, and failed
backing generation when the originating document/source and review are still current. Analysis
retries retain draft-replacement confirmation; export retries reopen destination and overwrite
confirmation rather than blindly replaying publication. Successful tasks, retired documents,
superseded sources/backing choices, and workflows without a safe retry are not replayed.

Setup consent is shared for the session and keyed by exact component checksums. Closing one
requesting project does not dismiss another project's prompt. Setup/download progress is reported
within its processing task; it is not a separate permanent global job.

Closing a tab with active work offers **Keep processing**, **Cancel tasks and close**, or
**Keep open**. Keep processing retains a hidden document and its recovery data; **Show project**
in Tasks restores that same tab. Processing does not survive application exit.

Export opens a progress dialog with the current operation, total and current-step elapsed
time, and a scrollable activity history without repetitive timestamp prefixes. Video conversion
processes the **full video before extracting prompts**. Its live status shows encoded video
position, frame count, percentage, and encoding speed when FFmpeg supplies those measurements.
It identifies the prompt at that position (number, speaker,
source range, and caption), or the gap between prompts. If no newer frame is reported for 15
seconds, the dialog says it is waiting for a newer frame report rather than implying progress.
Live updates replace the current history entry instead of filling the log.

Each prompt's audio, image, and metadata steps show its identity and source range. The dialog
also reports staged and published media validation, ZIP creation, and publication.
The **current-step time remaining** and **whole-export time remaining** start with rough workload
estimates, then adapt to measured video throughput and completed prompt/validation timings.
The separate **estimated overall progress** bar includes all remaining steps, including ZIP
creation (when requested), final validation, and cleanup. Estimates can move backward as timings
change; an unmeasured step that outlasts its estimate shows **re-estimating** instead of a false
zero-second countdown. Only a successful export reaches 100%. The current-step bar shows measured
video progress separately and stays indeterminate for operations without measurable progress.
The details window is nonmodal and can be closed while the export continues in Tasks. It retains
output locations, cleanup notes, and failures for later inspection. Export uses a snapshot; edits
made during export affect the next export, not the one already running.

### Combine segments

In the **Segments** list, use **Ctrl-click** to select individual rows or **Shift-click** to select
a range, then click **Combine** (or **Segments > Combine Selected Segments**, `Ctrl+Shift+M`).
The selected segments become one segment from the earliest In to the latest Out, including any
gaps. Their nonempty lines are joined with spaces in timeline order, and their speakers are
combined without duplicates. Unselected segments are left unchanged.

The combined segment uses source-video audio. Preserved recordings must first be given precise
source-video In/Out values using **Apply Range** and explicitly switched to regenerated audio.
A custom still image is retained; if the selection contains different custom stills, the app
asks before keeping the first one in timeline order. No source media files are deleted.
Playback keeps a multi-selection intact so you can combine segments while listening.
Select a single row again to edit or preview an individual segment and resume playback following.

### Analyze and transcribe a video

The nonmodal analysis window opens after **New from Video** and can be reopened with **Tools →
Analyze Video & Suggest Segments** (`Ctrl+Shift+R`). New local-video and YouTube imports both start
Whisper automatically, subject to download consent, while reopening existing drafts does not rerun it.
Backing generation, waveform extraction, caption refinement, and transcription are independent tasks;
waiting for one does not block editing another project. The window also offers a dependency-free activity
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

Results list editable In/Out values and draft captions. Double-click a line or use **Play Selected
Whisper Line** (or **Play Selected Range** for an activity-only scan) to audition it. Uncheck unwanted
lines, correct text, then use the highlighted **Use Whisper Transcript** action. **Rerun Whisper**
is a separate, secondary action. Existing drafts stay editable during a rerun; if they change
before it finishes, the new candidate requires an explicit replacement decision. Existing segments
are never replaced. Suggested segments intentionally have no speaker; assign speakers manually.
Whisper is probabilistic and can mishear names, stylized vocalizations, non-English speech, music,
or overlapping speakers, so every result remains review evidence rather than authoritative data.
One language selection applies to an entire scan; use Auto-detect for primarily single-language
sources, or rerun selected-language passes when a video deliberately mixes languages. Long videos
also receive conservative free-disk and memory checks before analysis begins.

New Whisper and Refined YouTube drafts include **up to 0.15 seconds of source audio before**
and **0.25 seconds after** each range. These are real audio handles, not inserted silence, so
slightly early consonants and trailing syllables can survive extraction. Short gaps are shared
at their midpoint to avoid introducing overlaps; touching ranges have no room for extra audio,
and already-overlapping rows retain their boundaries. The shown, previewed, saved, and imported
In/Out values include these handles once. Existing saved drafts and manual segment edits are not
automatically repadded. Adjust In/Out by ear when a boundary still clips speech.

Whisper retains its full segment range rather than shrinking it to lexical-token offsets:
even complete, ordered token timestamps can exclude a final word, while zero-duration tokens
can lose the first word. This conservative choice may leave extra silence or music to trim
manually. Neither segment timings nor small audio handles guarantee phoneme-accurate alignment,
particularly with music. Export's
separate head/tail padding still inserts silence around the selected source range; increasing
that silence cannot recover speech already cut out of the source.

Model download is setup, not transcription completion. The window identifies download,
checksum verification, model loading, and audio processing as separate steps. During transcription
it shows Whisper's reported percentage of audio processed and elapsed time; model loading and
the first audio block show an elapsed-time status until Whisper reports measurable progress.
The scan button says **Whisper Running** while busy and is not highlighted. **Cancel Scan** remains
available. Runtime startup and transcription have generous time limits so a stalled process reports
an error rather than waiting indefinitely, without removing saved drafts.

### Collecting logs for support

Logging is automatic; users do not need a console or a debug setting. **Help → Save Diagnostic
Bundle** saves a ZIP of recent application, analysis, and native crash logs with version
information. The same action is available in the YouTube import and analysis windows, including
while a task is running. Reproduce the issue, save the bundle, and send that ZIP with the approximate
time of the problem and a description of what the app displayed. If the app crashed, reopen it
and save the bundle before repeatedly retrying. Nothing is uploaded automatically.

**Help → Open Diagnostic Logs** or **Open Logs** in the analysis window opens the storage folder.
On Windows it is normally
`%LOCALAPPDATA%\ChoicerVoicerCommunity\Choicer Voicer Pack Creator\analysis\logs`.
It is outside the portable application folder, so logs survive application upgrades. The exact
analysis log path is also shown in the analysis window and error messages. If the app cannot
start, collect the files directly from this folder.

`application.log` contains UTC timestamps, application-session and worker-operation IDs,
thread/process details, application/Python/Qt/downloader versions, startup and UI handoffs,
YouTube download/merge/caption stages, Whisper download consent, media-tool execution, project saves/recovery, pack
import/export, update activity, and exceptions with tracebacks. `analysis.log` retains focused
per-analysis evidence: setup and checksum stages, CPU/memory/disk information,
model loading, audio-processing progress, subprocess commands,
Whisper's technical stderr, elapsed-time heartbeats, exit codes (including hexadecimal Windows
codes), failures, and cancellation/termination. This distinguishes "download finished" from
"Whisper launched", "processing audio", and "result reached the review window".

Entries are flushed as they are written; canceling or restarting does not erase the logs.
Application and analysis logs each retain three rotated backups (`.1` through `.3`), with a 2 MiB
target per file. `crash.log` captures Python stacks on supported fatal native errors and retains
three previous launches; an external forced termination or power loss cannot produce a crash stack.
The last progress/heartbeat can still identify where that run stopped. Bundle snapshots include
at most the last 2 MiB of each known log file and never copy arbitrary files from the log folder.
Logging/storage failures are reported rather than silently ignored.

Logs and bundles contain no media, model downloads, project files, or normal transcript stdout.
They contain local file paths and technical errors, which can still reveal filenames or other
personal information. URL queries/fragments, URL credentials, common credential fields, and the
current user's home-directory prefix are redacted from structured logs and bundles. Native crash
files on disk can contain unredacted code paths; the bundle redacts these before sharing.
**Review the ZIP before sending it.** Logs stay local and are not added to exported game packs.

### Import a YouTube video

Choose **File → New from YouTube** and paste a video URL. The destination defaults to your
Windows **Downloads** folder unless a previous location was selected; clearing the field also
uses Downloads. You can choose a different folder. Only download material you own or have
permission to use. Each import creates a new
`YouTube-<video-id>-<unique-id>` folder without replacing existing files. Save your `.cvpack.json`
beside that folder to keep the project relocatable; downloaded media is not a temporary cache.
The downloader retrieves the best available video/audio and may merge them into MKV.
Download percentages combine the selected video and audio transfers. Estimates are labeled and
may pause when sizes change or a transfer retries, but do not move backward. Unknown-size
transfers, merging, checking, and publication show indeterminate progress. **Ready** means the
checked video has been successfully published to its media folder.

Network work runs in isolated processes so **Cancel** can stop a blocked connection, DNS
lookup, or media tool. Video-detail requests have a 60-second limit per attempt; caption
requests have a 30-second limit. A stalled or failed connection is retried once using IPv4,
which can help networks with broken IPv6 connectivity. This does not bypass YouTube access
restrictions. Waiting stages show elapsed time and are included in diagnostic bundles.
Transfers stop after 2 minutes without advancing bytes; media preparation allows up to
10 minutes without progress, and the final video check allows 60 seconds. Healthy transfers
have no overall duration limit. Failed caption requests still fall back to local Whisper,
and canceled or failed imports never publish partial media.

YouTube can provide timestamped creator captions or automatic speech-recognition captions.
The importer prefers creator captions in the selected language and otherwise uses available
automatic captions. **Original language (auto)** uses YouTube's language metadata where available;
you can also select or type a language code. YouTube-generated translations are excluded, but
creator-uploaded tracks can themselves be translations. Some videos have no accessible captions,
and caption delivery can fail independently of the video download.

The **Refined YouTube** panel stays empty until a local audio-only refinement pass finishes on a
background task, then selects the processed draft for review and use. Unprocessed YouTube
captions are never displayed or offered for import. Refinement requires no model download;
original caption evidence stays in the project for regeneration, not as another transcript choice.
Whisper starts independently, asking permission first if its runtime/model must be downloaded.
Its result appears alongside YouTube in the **Whisper Transcript**
panel without changing the selected draft. Each transcript keeps its own row count, text, and
In/Out boundaries: a longer Whisper passage is not
forced onto shorter YouTube captions or flagged as a conflict. You can edit, preview, and uncheck
rows in each draft independently.
The playback button names the chosen source: **Play Selected Refined YouTube Line** or
**Play Selected Whisper Line**.

To adjust pause-aware segmentation, use the **Refined YouTube** panel and click
**Refine YouTube Again...**. Refinement and Whisper have separate task entries and cancellation. Refinement
uses the original imported YouTube words, not edits made to either draft.
New imports retain YouTube's available word/text-fragment offsets. Refinement uses these
boundaries together with measured audio pauses to split lines and conservatively join display
fragments, rather than treating subtitle display windows as spoken phrases. **Minimum pause**
defaults to **0.40 seconds** and can be adjusted from 0.20 to 1.00 seconds before refining again.
Changing the setting alone does not rewrite a draft.

Refinement never invents equally spaced word timings or splits an untimed phrase. Rows without
usable timing metadata, including captions in older saved projects, stay as whole phrases with
the same conservative source-audio handles and a note in the **Source** column. Hover over that
column to read the full note. Review every
suggested boundary: this pass measures audio energy, not speaker identity, so music/effects can
hide pauses and two speakers without a pause can still share a row. It does not correct
misrecognized words, perform forced alignment, or separate overlapping voices.

Click **Use Refined YouTube Transcript** or **Use Whisper Transcript** directly below the
draft you want. Both buttons remain visible; you do not need to select that source first.
Each button is enabled when its draft is ready and at least one row is checked.
Click a row in a draft or its **Select** control to choose it for playback and the Enter-key
Use action; merely finishing a background Whisper run does not select it.
The checked rows from that source become editable project segments with that source's timings
and no assigned speakers. Other sources are not mixed in. Playback also uses the chosen draft's
own edited In/Out range. Choosing an available draft does not cancel another running task.
Existing project segments are never
silently replaced; adding another set requires confirmation.

If captions are unavailable, the same automatic Whisper pass drafts suggestions from the audio.
Declining Whisper setup or a failed/canceled/empty Whisper scan leaves downloaded media and all
existing drafts intact. **Whisper model (next scan)** and **Spoken language (next scan)** affect
only a new run; they do not select or change an existing transcript. The current local draft
shows the model and detected language from its last successful nonempty scan, also after
saving and reopening the project. Older drafts without model information say **model not recorded**.
If a larger model fails due to memory limits, click **Use Whisper Transcript** below the
retained draft to use it without rerunning.
If all rows are unchecked, check at least one before using the transcript.
If automatic refinement fails, is canceled, or returns no rows, no original rows are shown as a
fallback. The independent Whisper task is unaffected; either pass can be retried manually.
Any previously completed refined draft is retained.
**Cancel Scan** keeps the analysis window open. **Keep Drafts & Close** hides the review while
manager-owned work continues in **Tasks**, and
retains all available drafts, including edits, checked rows, source selection, and the pause setting, without adding
segments. Draft changes are included in recovery snapshots and **Save Project**, just like other
project edits. **Tools → Analyze Video & Suggest Segments** restores completed drafts without
rerunning analysis. If refinement has never completed, reopening resumes that pass without
starting Whisper. Legacy original-draft edits are preserved in the project file but not displayed
or imported as refined results. A successful Whisper rescan replaces only the local draft after confirmation;
refining again replaces only the Refined YouTube draft after confirmation. A failed or canceled
scan keeps its previous result. Original YouTube caption evidence, fragment timings, and the source URL
are also retained. Replacing or clearing the source video clears its caption evidence and drafts.

The portable build includes pinned yt-dlp, its JavaScript solver, and Deno, and uses its existing
FFmpeg tools; no extra end-user installation is required. YouTube import contacts YouTube and its
media servers, but local Whisper does not upload your audio or captions. Playlists, live/upcoming
streams, and age-/sign-in-restricted videos are unsupported. Downloads may fail because of
availability, rate limits, or upstream changes; the importer does not use browser cookies or
attempt access-restriction workarounds. An authorized local copy can still be opened with
**New from Video**. Downloader/solver updates ship with application updates.

### Direct waveform editing

- Drag the white playback line or its top arrow to scrub without changing any ranges. The arrow
  remains draggable when the line overlaps an In/Out handle or a segment block.
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

Experimental Silero VAD, OCR, and game-dialogue lookup tools were evaluated but
are not treated as correctness oracles. Their existing results varied with model settings, merged
speakers, missed stylized vocalizations, or produced timing that did not match the spoken cut.
Whisper was integrated only as an explicitly optional local drafting tool with pinned provenance;
it never assigns speakers or silently changes project data. Exact lines still require human review
against the decoded source.

Backing separation is used only to create music/effects audio, not to infer captions, speakers or
timings. When no backing is selected, the export dialog offers generation or an explicit
**Export without music** choice. The exporter never puts the original voices under new recordings.

The exporter stages and validates every file before replacing an existing output. It retains the
previous folder and ZIP until the newly published copies have been hash-checked and fully validated.
A failed publication restores both previous artifacts.

## Saving and recovery

**Save Project** updates the current editable project JSON. Before replacing an existing project,
the editor retains one previous version beside it with a `.previous` suffix. **File → Restore
Previous Save** opens that version in a separate unsaved tab. Neither the existing tab nor the
current saved file is replaced.

**Save Project As** writes a separate project and does not modify the original project. Project
saves store edit decisions and media references; they do not rewrite imported source packs or media.
Saving is asynchronous and revision-aware: saving revision N cannot clear unsaved edits made while
that save runs. Two open documents cannot save to the same project path.

While editing, the app also writes debounced recovery snapshots to the current Windows user's local
application-data directory, in a separate namespace for each document, including unsaved projects.
The workspace list and recoveries restore separate tabs without overwriting project files. If the
saved file changed after recovery was recorded, the recovery opens as a separate unsaved copy.
Save or explicit Discard clears only that document's snapshot. Legacy `recovery-v2.json` snapshots
remain intact unless successfully migrated after acceptance; dismissing the offer does not delete
them. If a saved project becomes unreadable, opening it offers its adjacent previous version.
Export independently stages every generated file and retains rollback copies until the new folder
and ZIP pass final validation.

## Modify an existing pack

1. Choose **File → Import Existing Pack** for a folder containing `_pack_info.ini`, or
   **File → Import Pack ZIP** for an exported archive.
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

**New from Video** and **New from YouTube** automatically generate music/effects backing before
starting transcript analysis. The first run asks permission to download a pinned, checksum-verified
HT-Demucs model (approximately 302 MiB). Processing runs locally on the CPU; audio is never uploaded.
The verified model is retained for offline reuse. Missing or damaged model data requires download
permission again.

Generation has progress and cancellation and may take several minutes. It mixes the model's drums,
bass and other stems, excluding vocals, into a full-length backing aligned with the video. Separation
is approximate: some dialogue can bleed through and some effects or singing may be removed. Listen
to the result before sharing. Prompt extraction still uses the source video's original audio.

Use **Generate backing** in the Project section or **Tools → Generate Backing Track** at any time.
If backing is already selected, regeneration asks before replacing the project's selection. It writes
a new durable audio file under per-user application data, never over the original backing or video.
Captions, speakers, segment boundaries, analysis drafts, prompt MP3s and still images are not changed.
Save the project afterward to retain its new backing reference. Keep application data/media files
when moving a project, or relink them using **Choose**.

You can still choose a custom clean backing track. Declining/canceling automatic generation keeps
the import and lets you edit normally. If no backing is selected at export, choose generation or
explicitly **Export without music**; the latter creates the required duration-matched silent MP3.
Exports also report a warning when an existing or generated backing is silent or below -60 dBFS.
The source video's original mixed dialogue is never used as automatic backing.

### Recover an older pack with missing music

1. Open the saved `.cvpack.json` project. If only the export remains, use **File → Import Pack ZIP**
   or **Import Existing Pack**. ZIP media is extracted into a unique durable application-data folder;
   the original archive is not changed.
2. Click **Generate backing** (or **Regenerate backing** for the old silent MP3) and approve model
   download/replacement if prompted. The included `dub_video.ogv` retains the original audio in
   app-generated packs, so there is no need to redownload or transcribe the video.
3. **Save Project As**, then **Export Pack + ZIP** into a new output directory.

The existing dialogue text, speakers, trigger timestamps and imported prompt media are preserved.
Do not start a new project or rerun transcription to repair backing. Imported packs cannot recover
unsaved editor drafts or original unpadded cut boundaries that were never stored in the ZIP; they
do recover the exported dialogue and recordings.

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
Portable builds also include the MCP SDK and its Python runtime dependency licenses/metadata under
`licenses/python/`, indexed in the bundle's third-party notices.
The application invokes FFmpeg as a separate program and lets users replace the `bin` directory
with another compatible pair.

## Development

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -ra
.\Build-Portable.ps1
```

The Windows application folder is written below `dist/v1.0.0/portable-<build-id>/`, with a
shareable `Choicer-Voicer-Pack-Creator-1.0.0-Windows-x64.zip` beside it. The first build downloads
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
