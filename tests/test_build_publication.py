from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from importlib.resources import files
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build.py"
SPEC = importlib.util.spec_from_file_location("build_script_under_test", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
sys.path.insert(0, str(SCRIPT_PATH.parent))
try:
    BUILD_SCRIPT = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(BUILD_SCRIPT)
finally:
    sys.path.pop(0)


def test_separation_dependency_licenses_are_copied_from_installed_wheels(tmp_path):
    BUILD_SCRIPT.copy_separation_licenses(tmp_path)
    for package in BUILD_SCRIPT.SEPARATION_PACKAGES:
        assert any(path.is_file() for path in (tmp_path / "licenses" / package).rglob("*"))
    assert any("thirdpartynotices" in path.name.casefold()
               for path in (tmp_path / "licenses" / "onnxruntime").rglob("*"))
    assert any("copying" in path.name.casefold()
               for path in (tmp_path / "licenses" / "soundfile").rglob("*"))
    native_notices = (
        tmp_path / "licenses" / "kaldi-native-fbank" / "KaldiNativeFbank-ThirdParty.txt"
    ).read_text()
    assert "Mark Borgerding" in native_notices and "Wenzel Jakob" in native_notices


def _prepare_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    distribution = tmp_path / "dist" / "vtest"
    application = distribution / "portable-build" / "Choicer Voicer Pack Creator"
    application.mkdir(parents=True)
    executable = application / "Choicer Voicer Pack Creator.exe"
    executable.write_bytes(b"application")
    mcp_executable = application / "MCP" / "Choicer Voicer MCP.exe"
    mcp_executable.parent.mkdir()
    mcp_executable.write_bytes(b"console application")
    (mcp_executable.parent / "README.md").write_text("MCP setup", encoding="utf-8")
    (application / "_internal").mkdir()
    (application / "_internal" / mcp_executable.name).write_bytes(b"console payload")
    candidate = distribution / "portable-build" / ".candidate.zip"
    candidate.write_bytes(b"validated candidate")
    stable = distribution / "share.zip"
    pending = distribution / "pending-portable.json"
    latest = distribution / "latest-portable.json"
    pending.write_text(
        json.dumps(
            {
                "version": "test",
                "build_id": "build-id",
                "application_directory": application.relative_to(tmp_path).as_posix(),
                "executable": executable.relative_to(tmp_path).as_posix(),
                "mcp_executable": mcp_executable.relative_to(tmp_path).as_posix(),
                "candidate_archive": candidate.relative_to(tmp_path).as_posix(),
                "archive": stable.relative_to(tmp_path).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(BUILD_SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(BUILD_SCRIPT, "DIST", distribution)
    monkeypatch.setattr(BUILD_SCRIPT, "APP_VERSION", "test")
    monkeypatch.setattr(BUILD_SCRIPT, "PENDING_BUILD_MANIFEST", pending)
    monkeypatch.setattr(BUILD_SCRIPT, "LATEST_BUILD_MANIFEST", latest)
    return candidate, stable, latest


def test_promote_candidate_replaces_stable_only_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")

    assert BUILD_SCRIPT.promote_candidate("build-id") == 0

    assert stable.read_bytes() == b"validated candidate"
    assert not candidate.exists()
    manifest = json.loads(latest.read_text(encoding="utf-8"))
    assert manifest["build_id"] == "build-id"
    assert Path(tmp_path / manifest["mcp_executable"]).is_file()
    assert "candidate_archive" not in manifest
    assert not BUILD_SCRIPT.PENDING_BUILD_MANIFEST.exists()


def test_failed_manifest_promotion_restores_previous_stable_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    latest.write_text('{"build_id":"previous"}\n', encoding="utf-8")

    def fail_manifest(_path: Path, _value: dict[str, str]) -> None:
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(BUILD_SCRIPT, "_write_manifest_atomic", fail_manifest)
    with pytest.raises(OSError, match="manifest publication"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"
    assert json.loads(latest.read_text(encoding="utf-8"))["build_id"] == "previous"
    assert BUILD_SCRIPT.PENDING_BUILD_MANIFEST.is_file()


def test_missing_mcp_executable_prevents_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, _latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    (candidate.parent / BUILD_SCRIPT.APP_NAME / "MCP" / f"{BUILD_SCRIPT.MCP_NAME}.exe").unlink()

    with pytest.raises(RuntimeError, match="incomplete"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"


def test_mcp_executable_must_be_in_mcp_subfolder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_candidate(tmp_path, monkeypatch)
    pending = BUILD_SCRIPT.PENDING_BUILD_MANIFEST
    manifest = json.loads(pending.read_text(encoding="utf-8"))
    other = BUILD_SCRIPT.DIST / f"{BUILD_SCRIPT.MCP_NAME}.exe"
    other.write_bytes(b"wrong location")
    manifest["mcp_executable"] = other.relative_to(tmp_path).as_posix()
    pending.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="MCP subfolder"):
        BUILD_SCRIPT.promote_candidate("build-id")


@pytest.mark.parametrize("missing", ["MCP/README.md", "_internal/Choicer Voicer MCP.exe"])
def test_missing_mcp_launcher_support_files_prevent_promotion(tmp_path, monkeypatch, missing):
    candidate, stable, _latest = _prepare_candidate(tmp_path, monkeypatch)
    (candidate.parent / BUILD_SCRIPT.APP_NAME / missing).unlink()
    with pytest.raises(RuntimeError, match="incomplete MCP launcher layout"):
        BUILD_SCRIPT.promote_candidate("build-id")
    assert candidate.is_file() and not stable.exists()


def test_extra_root_mcp_executable_prevents_promotion(tmp_path, monkeypatch):
    candidate, stable, _latest = _prepare_candidate(tmp_path, monkeypatch)
    (candidate.parent / BUILD_SCRIPT.APP_NAME / f"{BUILD_SCRIPT.MCP_NAME}.exe").touch()
    with pytest.raises(RuntimeError, match="incomplete MCP launcher layout"):
        BUILD_SCRIPT.promote_candidate("build-id")
    assert candidate.is_file() and not stable.exists()


def test_mcp_assembly_keeps_one_runtime_and_copies_the_complete_guide(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    (root / "MCP").mkdir(parents=True)
    (root / "MCP" / "README.md").write_text("Complete MCP setup guide", encoding="utf-8")
    application = tmp_path / "Portable app ü"
    (application / "_internal").mkdir(parents=True)
    payload = application / f"{BUILD_SCRIPT.MCP_NAME}.exe"
    payload.write_bytes(b"frozen console payload")
    monkeypatch.setattr(BUILD_SCRIPT, "ROOT", root)

    BUILD_SCRIPT._assemble_mcp(application)

    assert not payload.exists()
    assert (application / "_internal" / payload.name).read_bytes() == b"frozen console payload"
    launcher = application / "MCP" / payload.name
    data = launcher.read_bytes()
    assert data.startswith(b"MZ")
    assert b'#!"<launcher_dir>\\..\\_internal\\Choicer Voicer MCP.exe" --cvpc-mcp-launcher\n' in data
    assert str(application).encode() not in data
    with BUILD_SCRIPT.zipfile.ZipFile(launcher) as archive:
        assert archive.namelist() == ["__main__.py"]
    assert (application / "MCP" / "README.md").read_text() == "Complete MCP setup guide"
    assert sorted(path.name for path in application.iterdir()) == ["MCP", "_internal"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native console launcher")
def test_native_mcp_launcher_forwards_arguments_stdio_and_exit_code(tmp_path):
    application = tmp_path / "Portable app ü"
    (application / "_internal").mkdir(parents=True)
    # A second stock launcher serves as an observable payload without running a full freeze.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("__main__.py", (
            "import json, sys\n"
            "print(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
            "print('payload diagnostic', file=sys.stderr)\n"
            "sys.exit(17)\n"
        ))
    (application / f"{BUILD_SCRIPT.MCP_NAME}.exe").write_bytes(
        files("distlib").joinpath("t64.exe").read_bytes()
        + f'#!"{sys.executable}"\n'.encode()
        + archive.getvalue()
    )
    BUILD_SCRIPT._assemble_mcp(application)
    launcher = application / "MCP" / f"{BUILD_SCRIPT.MCP_NAME}.exe"
    arguments = ["--headless", "--data-root", str(tmp_path / "MCP data ü")]
    completed = subprocess.run(
        [str(launcher), *arguments], input="client request\n", capture_output=True,
        text=True, cwd=tmp_path, timeout=15, check=False,
    )
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == {
        "args": ["--cvpc-mcp-launcher", str(launcher), *arguments],
        "stdin": "client request\n",
    }
    assert completed.stderr.strip() == "payload diagnostic"


def test_spec_builds_two_entrypoints_from_one_shared_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository with ' quotes"
    monkeypatch.setattr(BUILD_SCRIPT, "ROOT", root)
    monkeypatch.setattr(BUILD_SCRIPT, "BUILD", root / "build")
    monkeypatch.setattr(BUILD_SCRIPT, "_mcp_distribution_names", lambda: ["mcp", "cryptography"])
    deno = root / "Deno's runtime" / "deno.exe"
    monkeypatch.setattr(BUILD_SCRIPT.deno, "find_deno_bin", lambda: deno)
    analyses = []
    executables = []
    collections = []
    hook_calls = []
    for name in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    hooks = sys.modules["PyInstaller.utils.hooks"]

    def collect_data_files(name):
        hook_calls.append(("data", name))
        return [(f"{name}-data", name)]

    def collect_all(name):
        hook_calls.append(("all", name))
        return (
            [(f"{name}-data", name)],
            [(f"{name}-binary", name)] + (
                [("cudnn64_9.dll", name)] if name == "ctranslate2" else []
            ),
            [f"{name}.dynamic_module"],
        )

    def collect_submodules(name, *, filter):
        hook_calls.append(("submodules", name))
        assert not filter("mcp.cli")
        assert not filter("mcp.cli.cli")
        assert filter("mcp.client.stdio")
        assert filter("mcp.server.fastmcp")
        return ["mcp.dynamic_module"]

    def copy_metadata(name):
        hook_calls.append(("metadata", name))
        return [("sdk-metadata", "mcp.dist-info")]

    hooks.collect_data_files = collect_data_files
    hooks.collect_all = collect_all
    hooks.collect_submodules = collect_submodules
    hooks.copy_metadata = copy_metadata

    def analysis(paths, **kwargs):
        result = SimpleNamespace(
            scripts=[
                ("runtime_hook", str(root / "runtime_hook.py"), "PYSOURCE"),
                *[(Path(path).stem, path, "PYSOURCE") for path in paths],
            ],
            pure=object(),
            binaries=kwargs["binaries"],
            datas=kwargs["datas"],
            hiddenimports=kwargs["hiddenimports"],
            runtime_hooks=kwargs["runtime_hooks"],
            pathex=kwargs["pathex"],
        )
        analyses.append(result)
        return result

    def executable(*args, **kwargs):
        result = SimpleNamespace(args=args, **kwargs)
        executables.append(result)
        return result

    def collect(*args, **kwargs):
        collections.append((args, kwargs))

    spec = BUILD_SCRIPT._write_spec()
    exec(
        compile(spec.read_text(encoding="utf-8"), str(spec), "exec"),
        {"Analysis": analysis, "PYZ": lambda pure: pure, "EXE": executable, "COLLECT": collect},
    )

    assert len(analyses) == 1
    assert len(executables) == 2
    editor, mcp = executables
    assert editor.name == BUILD_SCRIPT.APP_NAME and editor.console is False
    assert mcp.name == BUILD_SCRIPT.MCP_NAME and mcp.console is True
    assert mcp.contents_directory == "."
    assert editor.exclude_binaries and mcp.exclude_binaries
    assert editor.args[0] is mcp.args[0] is analyses[0].pure
    assert [entry[0] for entry in editor.args[1]] == ["runtime_hook", "__main__"]
    assert [entry[0] for entry in mcp.args[1]] == ["runtime_hook", "mcp_entry"]
    assert collections == [
        (
            (editor, mcp, analyses[0].binaries, analyses[0].datas),
            {"strip": False, "upx": False, "name": BUILD_SCRIPT.APP_NAME},
        )
    ]
    assert ("data", "mcp") in hook_calls
    assert ("data", "yt_dlp_ejs") in hook_calls
    assert ("metadata", "mcp") in hook_calls
    assert ("metadata", "cryptography") in hook_calls
    assert "mcp.dynamic_module" in analyses[0].hiddenimports
    assert "anyio._backends._asyncio" in analyses[0].hiddenimports
    assert "yt_dlp_ejs.yt.solver" in analyses[0].hiddenimports
    assert "PySide6.QtMultimedia" in analyses[0].hiddenimports
    assert "PySide6.QtMultimediaWidgets" in analyses[0].hiddenimports
    assert (str(deno), "runtime/deno") in analyses[0].binaries
    assert (str(root / "assets"), "assets") in analyses[0].datas
    assert (
        str(root / "src" / "choicer_voicer_pack_creator" / "resources"),
        "choicer_voicer_pack_creator/resources",
    ) in analyses[0].datas
    assert ("yt_dlp_ejs-data", "yt_dlp_ejs") in analyses[0].datas
    assert analyses[0].runtime_hooks == [
        str(BUILD_SCRIPT.BUILD / "speaker_runtime_hook.py"),
        str(root / "scripts" / "separation_runtime_hook.py"),
    ]
    speaker_hook = (BUILD_SCRIPT.BUILD / "speaker_runtime_hook.py").read_text()
    assert speaker_hook.index("--cvpc-mcp-launcher") < speaker_hook.index(
        "multiprocessing.freeze_support()"
    )
    assert speaker_hook.index("multiprocessing.freeze_support()") < speaker_hook.index(
        "--speaker-matching-smoke"
    )
    assert "_kaldi_native_fbank" in analyses[0].hiddenimports
    assert "choicer_voicer_pack_creator.speaker_worker" in analyses[0].hiddenimports
    assert "choicer_voicer_pack_creator.caption_timing_worker" in analyses[0].hiddenimports
    assert "--caption-timing-smoke" in speaker_hook
    assert "timing_smoke(Path(sys.argv[2]))" in speaker_hook
    assert ("cudnn64_9.dll", "ctranslate2") not in analyses[0].binaries
    assert any(
        Path(source).name == "kaldi-native-fbank-core.dll" and destination == "."
        for source, destination in analyses[0].binaries
    )
    assert analyses[0].pathex == [str(root / "src")]
    for name in ("onnxruntime", "_soundfile_data", "kaldi_native_fbank", "ctranslate2", "tokenizers"):
        assert ("all", name) in hook_calls
        assert (f"{name}-data", name) in analyses[0].datas
        assert (f"{name}-binary", name) in analyses[0].binaries
        assert f"{name}.dynamic_module" in analyses[0].hiddenimports


@pytest.mark.parametrize("native_launcher", [False, True])
def test_launcher_arguments_are_normalized_before_multiprocessing(
    tmp_path, monkeypatch, native_launcher,
):
    import multiprocessing

    monkeypatch.setattr(BUILD_SCRIPT, "BUILD", tmp_path)
    BUILD_SCRIPT._write_spec()
    arguments = ["console.exe", "--multiprocessing-fork", "parent_pid=123"]
    if native_launcher:
        arguments[1:1] = ["--cvpc-mcp-launcher", r"D:\Portable app ü\MCP\launcher.exe"]
    monkeypatch.setattr(sys, "argv", arguments)
    calls = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append(sys.argv[:]))
    hook = tmp_path / "speaker_runtime_hook.py"
    exec(compile(hook.read_text(encoding="utf-8"), str(hook), "exec"), {})
    assert calls == [["console.exe", "--multiprocessing-fork", "parent_pid=123"]]


def test_caption_timing_dependencies_retain_metadata_and_missing_wheel_licenses(tmp_path):
    application = tmp_path / "app"
    application.mkdir()
    BUILD_SCRIPT._copy_caption_timing_licenses(application)
    for name, (_, filenames) in BUILD_SCRIPT.CAPTION_TIMING_LICENSES.items():
        destination = application / "licenses" / "python" / name
        assert (destination / "METADATA.txt").is_file()
        for filename in filenames:
            assert (destination / filename).is_file()
    assert "huggingface-hub" in BUILD_SCRIPT._caption_timing_distribution_names()
    assert "faster-whisper" not in BUILD_SCRIPT._caption_timing_distribution_names()
    assert "av" not in BUILD_SCRIPT._caption_timing_distribution_names()


def test_mcp_distribution_licenses_and_dependency_notices_are_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    distributions = {}
    for name, requirements in (
        ("mcp", ["anyio>=4", "pyjwt[crypto]>=2", "not-installed; extra == 'optional'"]),
        ("anyio", []),
        ("pyjwt", ["cryptography>=3; extra == 'crypto'"]),
        ("cryptography", []),
        ("distlib", []),
    ):
        license_path = Path(f"{name}-1.dist-info") / "licenses" / "LICENSE"
        (installed / license_path).parent.mkdir(parents=True)
        (installed / license_path).write_text(f"{name} license", encoding="utf-8")
        distributions[name] = SimpleNamespace(
            metadata={"Name": name},
            version="1",
            files=[license_path, Path("..") / "LICENSE"],
            requires=requirements,
            locate_file=lambda file: installed / file,
            read_text=lambda _file, name=name: f"Name: {name}\nLicense: MIT\n",
        )
    monkeypatch.setattr(BUILD_SCRIPT.metadata, "distribution", distributions.__getitem__)
    application = tmp_path / "app"
    application.mkdir()
    notices = application / "THIRD_PARTY_NOTICES.md"
    notices.write_text("# Existing FFmpeg notices\n", encoding="utf-8")

    BUILD_SCRIPT._copy_mcp_licenses(application)

    for name in distributions:
        bundled = application / "licenses" / "python" / name
        assert (bundled / f"{name}-1.dist-info" / "licenses" / "LICENSE").read_text() == (
            f"{name} license"
        )
        assert f"Name: {name}" in (bundled / "METADATA.txt").read_text()
        assert f"licenses/python/{name}/" in notices.read_text()
    assert notices.read_text().startswith("# Existing FFmpeg notices")
    assert "not-installed" not in notices.read_text()


def test_missing_mcp_license_fails_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        BUILD_SCRIPT.metadata,
        "distribution",
        lambda _name: SimpleNamespace(
            metadata={"Name": "mcp"},
            version="1",
            files=[],
            requires=[],
            read_text=lambda _file: "Name: mcp",
        ),
    )
    with pytest.raises(RuntimeError, match="license file"):
        BUILD_SCRIPT._copy_mcp_licenses(tmp_path)


def test_failed_candidate_replace_never_removes_stable_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, _latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    real_replace = BUILD_SCRIPT.os.replace

    def fail_candidate_replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).resolve() == candidate.resolve() and Path(destination).resolve() == stable.resolve():
            raise OSError("injected candidate replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(BUILD_SCRIPT.os, "replace", fail_candidate_replace)
    with pytest.raises(OSError, match="candidate replacement"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"
    assert not list(stable.parent.glob("*.previous-*"))


def test_manifest_and_rollback_failure_retains_exact_previous_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    latest.write_text('{"build_id":"previous"}\n', encoding="utf-8")
    real_replace = BUILD_SCRIPT.os.replace

    def fail_manifest(_path: Path, _value: dict[str, str]) -> None:
        raise OSError("injected manifest publication failure")

    def fail_backup_restore(source: str | Path, destination: str | Path) -> None:
        if ".previous-build-id" in Path(source).name and Path(destination).resolve() == stable.resolve():
            raise OSError("injected stable rollback failure")
        real_replace(source, destination)

    monkeypatch.setattr(BUILD_SCRIPT, "_write_manifest_atomic", fail_manifest)
    monkeypatch.setattr(BUILD_SCRIPT.os, "replace", fail_backup_restore)
    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        BUILD_SCRIPT.promote_candidate("build-id")

    backups = list(stable.parent.glob("*.previous-build-id"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous stable"
    assert stable.read_bytes() == b"validated candidate"
    assert candidate.read_bytes() == b"validated candidate"
    assert json.loads(latest.read_text(encoding="utf-8"))["build_id"] == "previous"
