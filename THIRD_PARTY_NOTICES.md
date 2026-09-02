# Third-party notices

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
