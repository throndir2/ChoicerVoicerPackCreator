from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from textwrap import dedent

import deno
from ffmpeg_bundle import prepare_bundle

from choicer_voicer_pack_creator.updates import write_portable_manifest

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    APP_VERSION = str(tomllib.load(project_file)["project"]["version"])
DIST = ROOT / "dist" / f"v{APP_VERSION}"
BUILD = ROOT / "build" / f"pyinstaller-v{APP_VERSION}"
APP_NAME = "Choicer Voicer Pack Creator"
MCP_NAME = "Choicer Voicer MCP"
MCP_DIRECTORY = "MCP"
FFMPEG_STAGE = ROOT / "build" / "ffmpeg-windows-x64-562ea50b4f2d213e"
LATEST_BUILD_MANIFEST = DIST / "latest-portable.json"
PENDING_BUILD_MANIFEST = DIST / "pending-portable.json"
SEPARATION_PACKAGES = ("onnxruntime", "numpy", "soundfile", "cffi", "pycparser",
                       "flatbuffers", "protobuf", "packaging", "kaldi-native-fbank")
CAPTION_TIMING_LICENSES = {
    "ctranslate2": ("4.8.1", ("CTranslate2-MIT.txt",)),
    "tokenizers": ("0.23.1", ("Tokenizers-Attribution.txt", "Flatbuffers-Apache-2.0.txt")),
}


def copy_separation_licenses(app_dir: Path) -> None:
    for package in SEPARATION_PACKAGES:
        distribution = importlib.metadata.distribution(package)
        licenses = [
            path for path in distribution.files or []
            if Path(path).name.casefold().startswith(("license", "copying", "notice",
                                                      "thirdpartynotices"))
        ]
        if not licenses:
            if package == "flatbuffers" and distribution.version == "25.12.19":
                target = app_dir / "licenses" / package
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    ROOT / "src" / "choicer_voicer_pack_creator" / "resources"
                    / "Flatbuffers-Apache-2.0.txt", target / "LICENSE.txt",
                )
                continue
            raise RuntimeError(f"Installed {package} wheel is missing its license notices")
        for license_file in licenses:
            relative = str(license_file).replace("\\", "/")
            if ".dist-info/licenses/" in relative:
                relative = relative.split(".dist-info/licenses/", 1)[1]
            elif ".dist-info/" in relative:
                relative = relative.split(".dist-info/", 1)[1]
            target = app_dir / "licenses" / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(license_file), target)
    shutil.copy2(
        ROOT / "src" / "choicer_voicer_pack_creator" / "resources"
        / "KaldiNativeFbank-ThirdParty.txt",
        app_dir / "licenses" / "kaldi-native-fbank",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_fsynced(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if _sha256(temporary) != _sha256(source):
            raise RuntimeError(f"Copied file failed verification: {destination}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_tree_streamed(source: Path, destination: Path) -> None:
    """Copy files through userspace to avoid Windows block-cloned executable images."""
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as input_stream, destination_path.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        shutil.copystat(source_path, destination_path)


def _write_manifest_atomic(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.{value['build_id']}.partial")
    try:
        with temporary.open("wb") as stream:
            stream.write((json.dumps(value, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dist_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    try:
        path.relative_to(DIST.resolve())
    except ValueError as error:
        raise RuntimeError(f"Build manifest path escapes the distribution folder: {value}") from error
    return path


def _mcp_distribution_names() -> list[str]:
    return _distribution_names(("mcp",))


def _caption_timing_distribution_names() -> list[str]:
    return _distribution_names(("ctranslate2", "tokenizers"))


def _distribution_names(roots: tuple[str, ...]) -> list[str]:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [Requirement(root) for root in roots]
    visited: set[tuple[str, str]] = set()
    names: set[str] = set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        extras = {
            extra for extra in {"", *requirement.extras} if (name, extra) not in visited
        }
        if not extras:
            continue
        visited.update((name, extra) for extra in extras)
        names.add(name)
        for dependency in metadata.distribution(name).requires or []:
            child = Requirement(dependency)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in extras
            ):
                pending.append(child)
    return sorted(names)


def _write_spec() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    speaker_hook = BUILD / "speaker_runtime_hook.py"
    speaker_hook.write_text(dedent("""\
        import multiprocessing
        import sys
        from pathlib import Path

        # The native MCP launcher adds its own archive path before the user's arguments.
        # Normalize these before freeze_support and the Qt-free worker dispatchers.
        if sys.argv[1:2] == ["--cvpc-mcp-launcher"]:
            del sys.argv[1:3]

        # Spawned inference must enter its target before automatic Qt runtime hooks.
        multiprocessing.freeze_support()

        if len(sys.argv) == 3 and sys.argv[1] == "--speaker-matching-smoke":
            from choicer_voicer_pack_creator.caption_timing_worker import smoke_main as timing_smoke
            from choicer_voicer_pack_creator.speaker_worker import smoke_main

            # The existing portable smoke harness exercises both optional native runtimes.
            if timing_smoke(Path(sys.argv[2])):
                raise SystemExit(1)
            raise SystemExit(smoke_main(Path(sys.argv[2])))
        if len(sys.argv) == 3 and sys.argv[1] == "--caption-timing-smoke":
            from choicer_voicer_pack_creator.caption_timing_worker import smoke_main

            raise SystemExit(smoke_main(Path(sys.argv[2])))
        """), encoding="utf-8")
    spec = BUILD / f"{APP_NAME}.spec"
    spec.write_text(
        dedent(
            f"""\
            from pathlib import Path
            from PyInstaller.utils.hooks import (
                collect_all, collect_data_files, collect_submodules, copy_metadata,
            )

            root = Path({str(ROOT)!r})
            source = root / "src" / "choicer_voicer_pack_creator"
            entrypoints = [source / "__main__.py", source / "mcp_entry.py"]
            data = [
                (str(root / "assets"), "assets"),
                (str(source / "resources"), "choicer_voicer_pack_creator/resources"),
            ]
            data += collect_data_files("mcp")
            data += collect_data_files("yt_dlp_ejs")
            binaries = [({str(deno.find_deno_bin())!r}, "runtime/deno")]
            hiddenimports = [
                "PySide6.QtMultimedia",
                "PySide6.QtMultimediaWidgets",
                "yt_dlp_ejs.yt.solver",
                "anyio._backends._asyncio",
                "_kaldi_native_fbank",
                "choicer_voicer_pack_creator.speaker_worker",
                "choicer_voicer_pack_creator.caption_timing_worker",
                *collect_submodules(
                    "mcp",
                    filter=lambda name: name != "mcp.cli" and not name.startswith("mcp.cli."),
                ),
            ]
            for package in (
                "onnxruntime", "_soundfile_data", "kaldi_native_fbank", "ctranslate2", "tokenizers",
            ):
                package_data, package_binaries, package_imports = collect_all(package)
                if package == "ctranslate2":
                    # CPU inference needs ctranslate2.dll and libiomp5md.dll, not the
                    # wheel's unused NVIDIA cuDNN loader (the build disables cuDNN).
                    package_binaries = [
                        item for item in package_binaries
                        if not Path(item[0]).name.lower().startswith("cudnn")
                    ]
                data += package_data
                binaries += package_binaries
                hiddenimports += package_imports
            # The extension is top-level; its companion DLL must also be at the root.
            from importlib.metadata import distribution
            fbank = distribution("kaldi-native-fbank")
            binaries += [
                (str(fbank.locate_file("kaldi-native-fbank-core.dll")), "."),
            ]
            # Include activated dependency extras too (e.g. PyJWT's crypto extra).
            for package in {sorted(set(_mcp_distribution_names() + _caption_timing_distribution_names()))!r}:
                data += copy_metadata(package)
            analysis = Analysis(
                [str(path) for path in entrypoints],
                pathex=[str(root / "src")],
                binaries=binaries,
                datas=data,
                hiddenimports=hiddenimports,
                hookspath=[],
                hooksconfig={{}},
                runtime_hooks=[
                    {str(speaker_hook)!r},
                    str(root / "scripts" / "separation_runtime_hook.py"),
                ],
                # Converted local models need neither training/conversion frameworks nor
                # a second media decoder. Do not capture them from a developer installation.
                excludes=["av", "faster_whisper", "torch", "tensorflow", "transformers", "fairseq"],
                noarchive=False,
            )
            pyz = PYZ(analysis.pure)

            def scripts_for(entrypoint):
                # Keep shared runtime hooks, but never execute the other application entrypoint.
                return [
                    script for script in analysis.scripts
                    if Path(script[1]) not in entrypoints or Path(script[1]) == entrypoint
                ]

            editor = EXE(
                pyz, scripts_for(entrypoints[0]), [],
                exclude_binaries=True,
                name={APP_NAME!r},
                console=False,
                debug=False,
                strip=False,
                upx=False,
            )
            mcp = EXE(
                pyz, scripts_for(entrypoints[1]), [],
                exclude_binaries=True,
                name={MCP_NAME!r},
                # This console payload is installed inside the shared runtime directory.
                contents_directory=".",
                console=True,
                debug=False,
                strip=False,
                upx=False,
            )
            collection = COLLECT(
                editor, mcp, analysis.binaries, analysis.datas,
                strip=False,
                upx=False,
                name={APP_NAME!r},
            )
            """
        ),
        encoding="utf-8",
    )
    return spec


def _assemble_mcp(app_dir: Path) -> None:
    """Use distlib's portable native launcher, not an unsupported parent contents directory."""
    payload = app_dir / f"{MCP_NAME}.exe"
    payload.replace(app_dir / "_internal" / payload.name)
    destination = app_dir / MCP_DIRECTORY
    destination.mkdir()
    # distlib resolves <launcher_dir> against the EXE, not the client's working directory.
    # Its standard Windows launcher preserves stdio, waits, and forwards the exit status.
    shebang = (
        f'#!"<launcher_dir>\\..\\_internal\\{MCP_NAME}.exe" --cvpc-mcp-launcher\n'
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("__main__.py", "")
    (destination / payload.name).write_bytes(
        files("distlib").joinpath("t64.exe").read_bytes() + shebang + archive.getvalue()
    )
    shutil.copy2(ROOT / MCP_DIRECTORY / "README.md", destination / "README.md")


def _copy_mcp_licenses(app_dir: Path) -> None:
    _copy_python_licenses(
        app_dir, _mcp_distribution_names(), "MCP SDK and Python dependencies",
        "The local stdio server uses the official MCP Python SDK.",
    )
    _copy_python_licenses(
        app_dir, ["distlib"], "MCP native launcher",
        "The portable MCP entry point uses distlib's native Windows console launcher.",
    )


def _copy_caption_timing_licenses(app_dir: Path) -> None:
    _copy_python_licenses(
        app_dir, _caption_timing_distribution_names(), "Local caption timing runtime",
        "Optional caption word alignment uses CTranslate2 and Tokenizers, without PyAV "
        "or faster-whisper. Model weights are not bundled.",
    )


def _copy_python_licenses(
    app_dir: Path, names: list[str], heading: str, description: str,
) -> None:
    notices = [
        f"\n## {heading}\n",
        description + " Installed runtime dependencies are listed below. "
        "Their supplied license/notice files and full "
        "package metadata (including author and source information) are bundled under "
        "`licenses/python/`.\n",
        "| Distribution | Version | Bundled notices |",
        "| --- | --- | --- |",
    ]
    for name in names:
        distribution = metadata.distribution(name)
        destination = app_dir / "licenses" / "python" / name
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "METADATA.txt").write_text(
            distribution.read_text("METADATA") or str(distribution.metadata),
            encoding="utf-8",
        )
        copied = 0
        for file in distribution.files or []:
            relative = Path(str(file))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            if not (
                relative.name.casefold().startswith(("license", "licence", "copying", "notice"))
                or any(part.casefold() == "licenses" for part in relative.parts[:-1])
            ):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
        fallback = CAPTION_TIMING_LICENSES.get(name)
        if not copied and fallback and distribution.version == fallback[0]:
            for filename in fallback[1]:
                shutil.copy2(
                    ROOT / "src" / "choicer_voicer_pack_creator" / "resources" / filename,
                    destination / filename,
                )
                copied += 1
        if name in {"mcp", "distlib", *CAPTION_TIMING_LICENSES} and not copied:
            raise RuntimeError(f"The installed {name} package does not provide its license file")
        notices.append(
            f"| {distribution.metadata['Name']} | {distribution.version} | "
            f"[License files and metadata](licenses/python/{name}/) |"
        )
    with (app_dir / "THIRD_PARTY_NOTICES.md").open("a", encoding="utf-8") as stream:
        stream.write("\n".join(notices) + "\n")


def build_candidate() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(parents=True, exist_ok=True)
    build_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    portable_root = DIST / f"portable-{build_id}"
    print("Preparing pinned LGPL FFmpeg runtime…", flush=True)
    prepare_bundle(FFMPEG_STAGE)
    spec = _write_spec()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(portable_root),
        "--workpath",
        str(BUILD),
        str(spec),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        return completed.returncode

    app_dir = portable_root / APP_NAME
    for name in (APP_NAME, MCP_NAME):
        if not (app_dir / f"{name}.exe").is_file():
            raise RuntimeError(f"PyInstaller did not produce {name}.exe")
    _assemble_mcp(app_dir)
    _copy_tree_streamed(FFMPEG_STAGE / "bin", app_dir / "bin")
    shutil.copytree(FFMPEG_STAGE / "licenses", app_dir / "licenses", dirs_exist_ok=True)
    shutil.copy2(FFMPEG_STAGE / "THIRD_PARTY_NOTICES.md", app_dir / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "LICENSE", app_dir / "LICENSE.txt")
    shutil.copy2(ROOT / "README.md", app_dir / "README.md")
    _copy_mcp_licenses(app_dir)
    _copy_caption_timing_licenses(app_dir)
    shutil.copy2(
        FFMPEG_STAGE / "licenses" / "FFmpeg-LGPL-3.0.txt",
        app_dir / "licenses" / "LGPL-3.0.txt",
    )
    resource_dir = ROOT / "src" / "choicer_voicer_pack_creator" / "resources"
    shutil.copy2(resource_dir / "WhisperCpp-MIT.txt", app_dir / "licenses")
    shutil.copy2(resource_dir / "OpenAI-Whisper-MIT.txt", app_dir / "licenses")
    for filename in (
        "Demucs-MIT.txt", "StemSplit-MIT.txt", "backing-separation.json",
        "WeSpeaker-Attribution.txt", "WeSpeaker-CC-BY-4.0.txt", "speaker-matching.json",
        "caption-timing.json", "FasterWhisper-MIT.txt",
    ):
        shutil.copy2(resource_dir / filename, app_dir / "licenses")
    copy_separation_licenses(app_dir)
    for package in ("yt-dlp", "yt-dlp-ejs", "deno"):
        distribution = importlib.metadata.distribution(package)
        license_files = [
            path for path in distribution.files or []
            if ".dist-info/licenses/" in str(path).replace("\\", "/")
        ]
        if not license_files:
            raise RuntimeError(f"Installed {package} wheel is missing its license notices")
        for license_file in license_files:
            relative = str(license_file).replace("\\", "/").split(".dist-info/licenses/", 1)[1]
            target = app_dir / "licenses" / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(license_file), target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        print(f"Python license was not found: {python_license}", file=sys.stderr)
        return 1
    shutil.copy2(python_license, app_dir / "licenses" / "Python-3.12.txt")
    pyinstaller_licenses = sorted(
        (Path(sys.prefix) / "Lib" / "site-packages").glob(
            "pyinstaller-*.dist-info/licenses/COPYING.txt"
        )
    )
    if len(pyinstaller_licenses) != 1:
        print("Could not uniquely locate the PyInstaller license", file=sys.stderr)
        return 1
    shutil.copy2(
        pyinstaller_licenses[0], app_dir / "licenses" / "PyInstaller-bootloader.txt"
    )

    ffmpeg = app_dir / "bin" / "ffmpeg.exe"
    ffprobe = app_dir / "bin" / "ffprobe.exe"
    for executable in (ffmpeg, ffprobe):
        result = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"Bundled tool failed to start: {executable}", file=sys.stderr)
            return 1
    write_portable_manifest(app_dir, APP_VERSION)
    stable_archive = DIST / f"Choicer-Voicer-Pack-Creator-{APP_VERSION}-Windows-x64.zip"
    candidate_archive = portable_root / f".{stable_archive.name}.candidate"
    partial_archive = portable_root / f".{stable_archive.name}.partial"
    try:
        with zipfile.ZipFile(
            partial_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as package:
            for path in sorted(app_dir.rglob("*")):
                if path.is_file():
                    package.write(
                        path, f"{APP_NAME}/{path.relative_to(app_dir).as_posix()}"
                    )
        with zipfile.ZipFile(partial_archive) as package:
            bad_member = package.testzip()
            if bad_member:
                print(
                    f"Generated ZIP failed CRC validation: {bad_member}",
                    file=sys.stderr,
                )
                return 1
        os.replace(partial_archive, candidate_archive)
    finally:
        partial_archive.unlink(missing_ok=True)

    manifest: dict[str, str] = {
        "version": APP_VERSION,
        "build_id": build_id,
        "application_directory": app_dir.relative_to(ROOT).as_posix(),
        "executable": (app_dir / f"{APP_NAME}.exe").relative_to(ROOT).as_posix(),
        "mcp_executable": (
            app_dir / MCP_DIRECTORY / f"{MCP_NAME}.exe"
        ).relative_to(ROOT).as_posix(),
        "candidate_archive": candidate_archive.relative_to(ROOT).as_posix(),
        "archive": stable_archive.relative_to(ROOT).as_posix(),
    }
    _write_manifest_atomic(PENDING_BUILD_MANIFEST, manifest)
    print(f"Bundled application with FFmpeg: {app_dir}", flush=True)
    print(f"Unpromoted ZIP candidate: {candidate_archive}", flush=True)
    return 0


def promote_candidate(expected_build_id: str) -> int:
    if not PENDING_BUILD_MANIFEST.is_file():
        raise FileNotFoundError(f"Pending portable-build manifest not found: {PENDING_BUILD_MANIFEST}")
    value = json.loads(PENDING_BUILD_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Pending portable-build manifest must contain a JSON object")
    manifest = {str(key): str(item) for key, item in value.items()}
    if manifest.get("version") != APP_VERSION:
        raise RuntimeError("Pending portable-build manifest has the wrong version")
    if manifest.get("build_id") != expected_build_id:
        raise RuntimeError("Pending portable-build manifest has an unexpected build identifier")
    candidate_archive = _dist_path(manifest["candidate_archive"])
    stable_archive = _dist_path(manifest["archive"])
    executable = _dist_path(manifest["executable"])
    mcp_executable = _dist_path(manifest["mcp_executable"])
    if (
        not candidate_archive.is_file()
        or not executable.is_file()
        or not mcp_executable.is_file()
    ):
        raise RuntimeError("Pending portable build is incomplete")
    if mcp_executable != executable.parent / MCP_DIRECTORY / f"{MCP_NAME}.exe":
        raise RuntimeError("The MCP executable must be in the editor's MCP subfolder")
    if (
        not (mcp_executable.parent / "README.md").is_file()
        or not (executable.parent / "_internal" / f"{MCP_NAME}.exe").is_file()
        or (executable.parent / f"{MCP_NAME}.exe").exists()
    ):
        raise RuntimeError("Pending portable build has an incomplete MCP launcher layout")

    backup = DIST / f".{stable_archive.name}.previous-{expected_build_id}"
    stable_existed = stable_archive.is_file()
    stable_backed_up = False
    candidate_promoted = False
    try:
        if stable_existed:
            _copy_file_fsynced(stable_archive, backup)
            stable_backed_up = True
        os.replace(candidate_archive, stable_archive)
        candidate_promoted = True
        latest_manifest = dict(manifest)
        latest_manifest.pop("candidate_archive", None)
        _write_manifest_atomic(LATEST_BUILD_MANIFEST, latest_manifest)
    except Exception as promotion_error:
        rollback_errors: list[str] = []
        if candidate_promoted and stable_archive.is_file():
            if stable_existed:
                try:
                    _copy_file_fsynced(stable_archive, candidate_archive)
                except Exception as error:
                    rollback_errors.append(f"could not retain validated candidate: {error}")
            else:
                try:
                    os.replace(stable_archive, candidate_archive)
                except OSError as error:
                    rollback_errors.append(f"could not retract candidate ZIP: {error}")
        if stable_backed_up and backup.is_file():
            try:
                os.replace(backup, stable_archive)
            except OSError as error:
                rollback_errors.append(
                    f"could not restore previous stable ZIP; backup retained at {backup}: {error}"
                )
        elif not candidate_promoted and backup.is_file():
            try:
                backup.unlink()
            except OSError as error:
                rollback_errors.append(f"could not remove unused ZIP backup {backup}: {error}")
        if rollback_errors:
            raise RuntimeError(
                f"Portable ZIP promotion failed ({promotion_error}); rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from promotion_error
        raise

    try:
        PENDING_BUILD_MANIFEST.unlink(missing_ok=True)
    except OSError as error:
        print(f"Warning: could not remove pending-build manifest: {error}", file=sys.stderr)
    try:
        backup.unlink(missing_ok=True)
    except OSError as error:
        print(f"Warning: could not remove previous portable ZIP: {error}", file=sys.stderr)
    print(f"Validated distributable ZIP promoted: {stable_archive}", flush=True)
    return 0


def main() -> int:
    if len(sys.argv) == 1:
        return build_candidate()
    if len(sys.argv) == 3 and sys.argv[1] == "--promote":
        return promote_candidate(sys.argv[2])
    print("Usage: build.py [--promote BUILD_ID]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
