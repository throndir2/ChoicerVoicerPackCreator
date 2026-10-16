# Development, building, and releases

This document is for developers and release maintainers. Users do not need any of these steps:
the [Setup installer and portable ZIP](https://github.com/throndir2/ChoicerVoicerPackCreator/releases/latest)
include everything. For editor features, see the [user guide](USER_GUIDE.md). For project
conventions, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Requirements

- Windows 10/11 x86-64 for the packaged desktop build. **FFmpeg and FFprobe are included**;
	end users do not install them separately.
- Python 3.11 or newer for development.
- Source development and tests require FFmpeg/FFprobe on `PATH`, built with `libtheora`,
	`libvorbis`, and `libmp3lame`.

## Run from source

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m choicer_voicer_pack_creator
```

If `py` is unavailable, invoke your installed Python executable directly.

### Optional singing-preserving CPU backend

The core source application still requires Python **3.11 or newer**. The additional
**Keep singing; remove dialogue** CPU wheel setup targets **Windows x64 CPython 3.11/3.12**
only; portable builds use 3.12. Native source qualification on 3.11 is deferred for 1.3.9.
A base source install remains usable without this extra.
To enable it in your own virtual environment, install the exact official CPU wheels **first**,
then install the extra from public PyPI:

```powershell
# In the repository root, after creating .venv with 64-bit Python 3.11 or 3.12:
$previousPipConfig = $env:PIP_CONFIG_FILE
try {
    $env:PIP_CONFIG_FILE = "nul" # Exact os.devnull spelling; pip compares this case-sensitively.
    .\.venv\Scripts\python.exe -m pip --isolated install --no-index --no-deps --require-hashes -r .\tools\singing-cpu-wheels.txt
    if ($LASTEXITCODE -ne 0) { throw "CPU wheel installation failed" }
    .\.venv\Scripts\python.exe -m pip --isolated install --index-url https://pypi.org/simple -e ".[singing]"
    if ($LASTEXITCODE -ne 0) { throw "Singing dependencies installation failed" }
    .\.venv\Scripts\python.exe -m pip --isolated check
    if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed" }
} finally {
    $env:PIP_CONFIG_FILE = $previousPipConfig
}
```

Developers can use `".[dev,singing]"` in the second install command. The canonical requirements
select the CPython/ABI-matching CPU wheels and verify their SHA-256 hashes. The extra requires
`torch==2.8.0+cpu` and `torchaudio==2.8.0+cpu`; plain PyPI torch (which can bring CUDA dependencies)
is not a substitute. Do not replace this setup with a floating extra index or `pip install torch`.
NumPy remains 2.4.6, SoundFile 0.13.1, and ONNX Runtime 1.26.0; the new backend pins librosa
0.10.2.post1, SciPy 1.15.3, Numba 0.67.0, and llvmlite 0.49.0.
The application never invokes pip or installs a Python runtime for you. The model's separate
non-commercial license and consent still apply after installing these dependencies.
On eligible NVIDIA systems, this same source installation can offer the optional pinned CUDA
runtime described below. Keep the CPU wheels installed: the application loads CUDA files from an
isolated cache in its worker, not by changing your virtual environment. The GPU research setup in
`tools\separation-comparison` is separate and must not be installed into this environment.

## Development

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -ra
.\Build-Portable.ps1
```

The Windows application folder is written below `dist/v1.4.2/portable-<build-id>/`, with a
shareable `Choicer-Voicer-Pack-Creator-1.4.2-Portable-Windows-x64.zip` beside it. The first build downloads
about 64 MiB of pinned FFmpeg input and emits a self-contained bundle. The stable sharing ZIP is not
replaced until both the application folder and a clean ZIP extraction pass packaged smoke tests.
Startup rejects a missing/mismatched tool pair or builds lacking `libtheora`, `libvorbis`, or
`libmp3lame`.

## Build a portable Windows package locally

On a Windows x86-64 computer, double-click `Build-Portable.cmd`. From PowerShell, the equivalent is:

```powershell
.\Build-Portable.ps1
```

The build computer needs **64-bit Python 3.12** and an internet connection for the first build. The
script creates an isolated environment under the current user's local application data, installs
SHA-256-pinned official **CPU-only** PyTorch/TorchAudio wheels before the `singing` extra
and pinned Python packaging dependencies, securely
downloads and verifies the pinned LGPL FFmpeg runtime, builds the application, and smoke-tests the
finished editor and console MCP executables, including an official-SDK stdio handshake.
It does not require a system FFmpeg installation. Both entrypoints include the singing backend;
recipients do not install Python, pip, CUDA, or a separate runtime executable.
BandIt can separately offer a checksum-verified CUDA acceleration download at runtime on an
eligible NVIDIA system. That optional application-data cache is not part of the ZIP and never
replaces the bundled CPU runtime; see [automatic BandIt acceleration](USER_GUIDE.md#automatic-bandit-acceleration).

For concurrent development, give each task its own build environment instead of modifying the
shared default:

```powershell
.\Build-Portable.ps1 -BuildEnvironment .\build\environments\singing-cpu
```

Use `-ResetBuildEnvironment` only with an environment created and marked by this script.
Reset refuses unmarked directories, repository roots/ancestors, and links/junctions. For a legacy
unmarked environment, choose a new path instead of deleting it through the build script.
The build uses isolated pip configuration, hash-verified direct CPU wheel URLs, then public
PyPI for the other dependencies; it checks exact CPU versions and rejects GPU binaries.

The offline smoke exercises tiny synthetic BandIt CPU inference from **both** EXEs, as well as
the existing ONNX, caption timing, speaker, MCP, FFmpeg, and updater checks. It runs on the
candidate and a clean ZIP extraction before publication. It never downloads a checkpoint.
Tiny synthetic smoke is a packaging check, not a substitute for real-checkpoint CPU/parity tests.
Dependency metadata and recursive Python/native license notices are included in `licenses/`.
Each backend retains its own wheel's OpenMP DLL; `licenses/singing-runtime.json` records their
SHA-256 hashes, checked against the installed pinned wheels. No global OpenMP replacement or
duplicate-runtime suppression is applied.

Allow roughly **3.5–5 GiB extra build-environment storage** for the singing dependencies, plus
candidate/ZIP/extraction and updater staging space. The torch wheel contains over 2 GiB of static
development libraries that are not shipped. Estimated added portable size is **150–300 MiB ZIP**
and **0.6–1.0 GiB extracted**, excluding model weights; these are allowances, not measured final
sizes. The build prints the actual assembled and compressed sizes. The optional combined BandIt
checkpoint is approximately **426 MiB**, downloaded only with consent, and carries **CC BY-NC 4.0**
non-commercial restrictions; the inference source is Apache-2.0. See About and the bundled notices.

The finished outputs are:

```text
dist/v1.4.2/portable-<build-id>/Choicer Voicer Pack Creator/
dist/v1.4.2/Choicer-Voicer-Pack-Creator-1.4.2-Portable-Windows-x64.zip
```

The script prints the exact generated application-folder path. Each rebuild uses a new path to avoid
a Windows executable/DLL cache issue that can affect tools replaced repeatedly at the same location;
older `portable-*` generation folders can be deleted when no copy of the app is running.

This is a **portable application folder**, not an installer. To build the Setup installer from it,
see [Build the Setup installer](#build-the-setup-installer). To use or share the portable folder:

1. Extract the complete ZIP; do not run the executable from inside the ZIP viewer.
2. Keep the extracted directory together, including its `MCP`, `bin`, and `_internal`
	directories.
3. Run `Choicer Voicer Pack Creator.exe`.

The optional assistant integration is in the `MCP` folder, with its executable and `README.md`.
It is not needed for normal editor use. Let an MCP client launch that executable rather than
double-clicking it.

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

### Build the Setup installer

After `Build-Portable.ps1` succeeds, build the Setup installer from the same validated folder:

```powershell
.\Build-Installer.ps1
```

The build computer needs [Inno Setup 6](https://jrsoftware.org/isinfo.php). To install it for the
current user, run `winget install --id JRSoftware.InnoSetup -e --scope user`. The script compiles
`installer\ChoicerVoicerPackCreator.iss` and writes these files beside the ZIP:

```text
dist/v1.4.2/Choicer-Voicer-Pack-Creator-1.4.2-Setup.exe
dist/v1.4.2/Choicer-Voicer-Pack-Creator-1.4.2-Setup.exe.sha256
```

The installer contains the same files as the ZIP, plus a `setup-install.json` marker. The marker
tells the editor to update itself with the Setup installer instead of the portable updater.

## Release validation

Release maintainers can additionally run Godot's real `ConfigFile` parser and Xiph reference Ogg,
Vorbis, and Theora decoders:

```powershell
.\.venv\Scripts\python.exe scripts\create_validation_fixture.py build\runtime-validation
.\.venv\Scripts\python.exe scripts\validate_external.py "build\runtime-validation\output\Pack Creator Validation Fixture" --require-godot --require-xiph
```

Godot and Docker are only required for these independent release gates, not normal GUI use.

## GitHub releases

See [the 1.3.9 release notes](RELEASE_NOTES_1.3.9.md) for the singing-preserving backing
workflow, licensing, and deferred broader CPU qualification.

This repository intentionally has **no automatic push or pull-request CI**. Pushing commits does not
start an Actions runner. Tests and validation are run locally during development.

The only GitHub Actions workflow is **Create GitHub Release**, and it runs solely when a maintainer
manually selects **Actions → Create GitHub Release → Run workflow**. It reads the version from
`pyproject.toml`, builds and smoke-tests the portable Windows package, builds the Setup installer,
creates tag `v<version>`, and publishes a GitHub Release. The release contains the Setup EXE, the
portable ZIP, and a SHA-256 checksum file for each. Keep both asset names unchanged: installed
copies update with the Setup EXE, and portable copies update with the `-Portable-Windows-x64.zip`.
Only release 1.4.1 uses the old `-Windows-x64.zip` name, so that portable copies of 1.4.0 and older
always update to 1.4.1. Select **Required update** when you run the workflow to make a release
that all older copies must install before a newer release.
Running it again without
bumping the project version fails before the expensive build rather than replacing an existing
release or tag. Releases are restricted to the canonical `main` branch.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for project conventions.

## Why Qt, FFmpeg, and Godot?

- **PySide6/Qt is the application framework.** It provides the native desktop window, controls,
	multimedia preview, background workers, and custom waveform/timeline rendering.
- **FFmpeg performs media work.** It probes source files, generates waveform samples, extracts and
	pads prompt audio, captures stills, and writes the Theora/Vorbis video required by the game.
	Windows builds bundle a pinned FFmpeg 9.0.2 x86-64 LGPL shared distribution, so users do not
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
upstream LGPL text, build-provenance manifest, and [third-party notices](../THIRD_PARTY_NOTICES.md).
Portable builds also include the MCP SDK and its Python runtime dependency licenses/metadata under
`licenses/python/`, indexed in the bundle's third-party notices.
The application invokes FFmpeg as a separate program and lets users replace the `bin` directory
with another compatible pair.
