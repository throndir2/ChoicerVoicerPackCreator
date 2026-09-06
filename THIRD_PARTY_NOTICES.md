# Third-party notices

## Buy Me a Coffee button

The support link in **Help > About** uses Buy Me a Coffee's
[official yellow button image](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png).
An unmodified copy is bundled at `resources/buy-me-a-coffee.png` so the app can display it
without contacting the image host. Clicking the button opens the support page in the user's browser.
The Buy Me a Coffee name and artwork belong to their respective owner.

## FFmpeg

Windows application bundles include an unmodified **FFmpeg 9.0.1 LGPL shared build** supplied by the [BtbN FFmpeg-Builds project](https://github.com/BtbN/FFmpeg-Builds). FFmpeg is a separate command-line program invoked by Choicer Voicer Pack Creator; the application does not link to FFmpeg libraries.

FFmpeg is licensed under the **GNU Lesser General Public License, version 3 or later** in the bundled configuration. The exact license text from the upstream binary archive is included in each Windows bundle at `licenses/FFmpeg-LGPL-3.0.txt`.

Bundled binary provenance:

- Version: `n9.0.1-11-ge47273f4d9-20260901`
- Variant: Windows x86-64, LGPL, shared libraries
- Upstream release: `autobuild-2026-09-01-13-13`
- Binary archive SHA-256: `562ea50b4f2d213e3883a8fbef581ca2ccf6c7ab647c0e0b93d55294c5a0ae7a`
- FFmpeg source revision: [`e47273f4d9227152dcbf543cebaf9e2430ddbcc4`](https://github.com/FFmpeg/FFmpeg/tree/e47273f4d9227152dcbf543cebaf9e2430ddbcc4)
- Build recipe revision: [`8267213e26c1031621e6e1210fe3aa4867214f6a`](https://github.com/BtbN/FFmpeg-Builds/tree/8267213e26c1031621e6e1210fe3aa4867214f6a)
- Exact FFmpeg source archive: <https://github.com/FFmpeg/FFmpeg/archive/e47273f4d9227152dcbf543cebaf9e2430ddbcc4.tar.gz>
- Exact build-recipe archive: <https://github.com/BtbN/FFmpeg-Builds/archive/8267213e26c1031621e6e1210fe3aa4867214f6a.tar.gz>

The build recipe records the configure command and exact source recipes for FFmpeg's enabled dependencies. Users may replace the files in the application's `bin` directory with another compatible `ffmpeg.exe`/`ffprobe.exe` pair. The application requires `libtheora`, `libvorbis`, and `libmp3lame` encoders and checks them at startup.

FFmpeg is a trademark of Fabrice Bellard. Choicer Voicer Pack Creator is not affiliated with the FFmpeg project or BtbN.

## Optional whisper.cpp analysis

The optional **Analyze Video** feature can download an unmodified CPU build of
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) and a converted OpenAI Whisper model into
the current user's local application-data directory. These files are not included in the base
application ZIP. YouTube import starts local transcription automatically, but downloading missing
components still requires the user's permission.

- whisper.cpp version: `1.9.3`, release/build `b4938`
- whisper.cpp source revision: `371b5a7561823ab2bb32142d2751e35e7534727b`
- Windows x64 runtime archive SHA-256: `c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d`
- Model repository revision: `5359861c739e955e79d9a303bcbc70fb988958b1`
- Tiny multilingual model SHA-256: `be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21`
- Base multilingual model SHA-256: `60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe`

Both whisper.cpp and OpenAI Whisper are licensed under the MIT License. Exact license texts are
stored in `src/choicer_voicer_pack_creator/resources/WhisperCpp-MIT.txt` and
`src/choicer_voicer_pack_creator/resources/OpenAI-Whisper-MIT.txt` and are copied
beside downloaded analysis components. Full immutable URLs and file inventory are recorded in
`src/choicer_voicer_pack_creator/resources/whisper-analysis-windows-x64.json`.

Whisper output is probabilistic review assistance. It is not represented as an exact transcript,
speaker detector, or authoritative source boundary.

## YouTube import

The application includes the pinned Python packages **yt-dlp 2026.8.19** (Unlicense),
**yt-dlp-ejs 0.8.0** (Unlicense, MIT, ISC), and **Deno 2.9.6** (MIT).
The portable build includes Deno's Windows x64 executable and the EJS JavaScript solver files;
it does not download remote solver components at runtime.

Upstream projects and corresponding source:

- <https://github.com/yt-dlp/yt-dlp/tree/2026.08.19>
- <https://github.com/yt-dlp/ejs/tree/0.8.0>
- <https://github.com/denoland/deno/tree/v2.9.6>
- <https://github.com/denoland/deno_pypi>

Wheel license notices are copied to `licenses/yt-dlp`, `licenses/yt-dlp-ejs`, and `licenses/deno`.
The bundled EJS `yt_dlp_ejs/yt/solver/lib.min.js` retains the MIT astring and ISC meriyah
copyright and permission notices. yt-dlp is embedded from its Python wheel rather than its
standalone executable or optional `default` dependency set.

## Optional local backing-track separation

The application includes **ONNX Runtime 1.26.0** (MIT), **NumPy 2.4.6** (BSD-3-Clause
and bundled numerical-library notices), and **SoundFile 0.13.1** (BSD-3-Clause).
SoundFile's Windows wheel includes the shared **libsndfile** library under LGPL-2.1-or-later
and its bundled codec-library notices. These unmodified libraries remain separate shared
libraries in the portable application's `_internal` directory.
Their installed wheel license texts, including ONNX Runtime's third-party notices and
SoundFile's libsndfile COPYING text, are copied to `licenses/onnxruntime`, `licenses/numpy`,
and `licenses/soundfile`. The build also preserves license texts for their runtime
dependencies cffi, pycparser, flatbuffers, protobuf, and packaging.
Flatbuffers is pinned to 25.12.19 because that wheel omits its Apache-2.0 license file;
the exact [upstream license](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE)
is retained in `resources/Flatbuffers-Apache-2.0.txt` and copied to `licenses/flatbuffers`.

Sources and licensing:

- <https://github.com/microsoft/onnxruntime/tree/v1.26.0>
- <https://github.com/numpy/numpy/tree/v2.4.6>
- <https://github.com/bastibe/python-soundfile/tree/0.13.1>
- <https://github.com/libsndfile/libsndfile>

Generating a backing track optionally downloads the **HTDemucs** four-stem ONNX model
from `StemSplitio/htdemucs-onnx`, revision `d54ed9eb60e258ea82131c6ee14578628816456a`.
The model is not bundled. Its 316,446,953-byte download requires explicit permission,
including repairs to an invalid cache; verified cached models work offline. The SHA-256 is
`68d0bf16428ef66e692cdff8a9ccf28f1ef3f69440d57e58605a4cc55fcc5e74`.
Audio processing runs locally; no audio is uploaded.

HTDemucs is MIT-licensed, copyright Meta Platforms, Inc. and affiliates.
The ONNX export and reference inference are MIT-licensed, copyright 2026 StemSplit.
The application's disk-backed streaming overlap-add implementation adapts the
[pinned reference inference](https://huggingface.co/StemSplitio/htdemucs-onnx/blob/d54ed9eb60e258ea82131c6ee14578628816456a/infer.py),
corrects its zero-weight endpoint handling, and sums drums, bass, and other rather than vocals.
It preserves the video's timeline and applies one global gain only when needed for clipping safety.
The exact MIT notices and immutable model provenance are in `resources/Demucs-MIT.txt`,
`resources/StemSplit-MIT.txt`, and `resources/backing-separation.json`; the portable build
and downloaded model cache both retain these files.

Separation is probabilistic and can leave voice remnants or remove desired music/effects.
It is not an authoritative recovery of an original instrumental mix.

## Qt for Python / PySide6

The Windows application uses Qt for Python (PySide6 6.11.2) under the LGPL v3 option. The generic
LGPL v3 text is included at `licenses/LGPL-3.0.txt`. Qt/PySide source and licensing information is
available from <https://code.qt.io/cgit/pyside/pyside-setup.git/> and
<https://www.qt.io/licensing/open-source-lgpl-obligations>. The application uses Qt through its
unmodified shared libraries.

## Python

The packaged application embeds Python 3.12. Its PSF license is included at
`licenses/Python-3.12.txt`. Source is available from <https://www.python.org/downloads/source/>.

## PyInstaller bootloader

The executable uses the PyInstaller bootloader. Its GPL license and special bootloader exception
are included at `licenses/PyInstaller-bootloader.txt`. Source is available from
<https://github.com/pyinstaller/pyinstaller>.
