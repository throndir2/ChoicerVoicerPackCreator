# Choicer Voicer Pack Creator

A visual desktop editor for creating and modifying dub packs for *The Choicer Voicer*.

> **Unofficial community project.** This project is not affiliated with, endorsed by, or sponsored by the creators of *The Choicer Voicer*. Do not redistribute video, audio, or artwork unless you have permission to do so.

## What it does

- Creates a new project from MP4, MKV, MOV, WebM, OGV, or AVI video.
- Plays the source video inside the editor.
- Extracts and displays a zoomable waveform.
- Marks precise In/Out points in seconds.
- Adds, previews, splits, duplicates, deletes, and re-times segments.
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

- Windows 10/11 for the currently tested desktop build. The Python source is designed to remain portable to Linux.
- Python 3.11 or newer for development.
- FFmpeg and FFprobe on `PATH`, built with `libtheora`, `libvorbis`, and `libmp3lame`.

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

When no backing track is selected, the exporter creates a duration-matched silent backing track;
it never places the source video's original voices under new recordings.

The exporter stages and validates every file before replacing an existing output. It retains the
previous folder and ZIP until the newly published copies have been hash-checked and fully validated.
A failed publication restores both previous artifacts.

## Modify an existing pack

1. Choose **File → Import Existing Pack** and select the folder containing `_pack_info.ini`.
2. Existing MP3 and PNG assets are preserved by default.
3. Edit captions, speakers, or timeline positions.
4. Existing packs store a trigger timestamp and padded recording, not the original spoken cut. To
	regenerate one safely, mark the exact source-video In/Out range, click **Apply Range**, and choose
	**No** when asked whether to keep the imported audio.
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

## Development

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -ra
.\.venv\Scripts\python.exe scripts\build.py
```

The Windows application bundle is written to `dist/v0.1.0/Choicer Voicer Pack Creator/`. FFmpeg is not
redistributed by this repository; place a matched `ffmpeg.exe` and `ffprobe.exe` pair on `PATH` or
beside the packaged application. Startup rejects builds missing `libtheora`, `libvorbis`, or
`libmp3lame`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for project conventions.

## License

MIT. See [LICENSE](LICENSE).
