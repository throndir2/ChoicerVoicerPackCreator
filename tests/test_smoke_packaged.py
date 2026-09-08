from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_packaged.py"
SPEC = importlib.util.spec_from_file_location("smoke_packaged_under_test", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


@pytest.mark.parametrize("path_key,root_key", [("PATH", "SystemRoot"), ("Path", "SYSTEMROOT")])
def test_mcp_environment_removes_source_python_and_developer_path(
    tmp_path: Path, path_key: str, root_key: str
) -> None:
    executable = tmp_path / "portable app" / SMOKE.MCP_NAME
    environment = {
        path_key: r"C:\Source\.venv\Scripts;C:\Developer\ffmpeg",
        "PYTHONPATH": r"C:\Source\src",
        "PYTHONHOME": r"C:\Python",
        "VIRTUAL_ENV": r"C:\Source\.venv",
        "KMP_DUPLICATE_LIB_OK": "TRUE",
        "PIP_INDEX_URL": "https://invalid.example",
        "NUMBA_CACHE_DIR": r"C:\Source\cache",
        "PYTHONUSERBASE": r"C:\Source\user",
        "CHOICER_VOICER_SMOKE_REPORT": "report.json",
        root_key: r"D:\Windows",
        "UNCHANGED": "value",
    }

    isolated = SMOKE.mcp_environment(environment, executable)

    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & isolated.keys()
    assert "CHOICER_VOICER_SMOKE_REPORT" not in isolated
    assert not {
        "KMP_DUPLICATE_LIB_OK", "PIP_INDEX_URL", "NUMBA_CACHE_DIR", "PYTHONUSERBASE",
    } & isolated.keys()
    assert isolated["PATH"].split(os.pathsep) == [
        str(executable.parent / "bin"),
        r"D:\Windows\System32",
        r"D:\Windows",
    ]
    assert "Path" not in isolated
    assert isolated["UNCHANGED"] == "value"
    assert environment["PYTHONPATH"] == r"C:\Source\src"


@pytest.mark.parametrize(
    "failure",
    [None, "missing-tool", "tool-error", "empty-help", "empty-guide", "invalid-help", "wrong-mode", "wrong-version"],
)
def test_packaged_mcp_smoke_initializes_lists_and_calls_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    executable = tmp_path / SMOKE.MCP_NAME
    calls = []

    @asynccontextmanager
    async def stdio_client(parameters):
        calls.append("start")
        assert parameters.command == str(executable)
        assert parameters.args == ["--headless"]
        assert parameters.cwd == str(executable.parent)
        assert "PYTHONPATH" not in parameters.env
        try:
            yield ("read", "write")
        finally:
            calls.append("stop")

    class Session:
        def __init__(self, read_stream, write_stream):
            assert (read_stream, write_stream) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            calls.append("close")

        async def initialize(self):
            calls.append("initialize")
            return SimpleNamespace(
                serverInfo=SimpleNamespace(name="Choicer Voicer"),
                protocolVersion="test-protocol",
            )

        async def list_tools(self):
            calls.append("list")
            names = SMOKE.REQUIRED_MCP_TOOLS - (
                {"get_help"} if failure == "missing-tool" else set()
            )
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        async def call_tool(self, name, arguments):
            calls.append("help")
            assert (name, arguments) == ("get_help", {})
            return SimpleNamespace(
                isError=failure == "tool-error",
                structuredContent=None if failure == "invalid-help" else {
                    "version": "wrong" if failure == "wrong-version" else SMOKE.APP_VERSION,
                    "mode": "live" if failure == "wrong-mode" else "headless",
                    "help": "" if failure == "empty-guide" else "The bundled help guide",
                },
                content=[
                    SimpleNamespace(
                        type="text",
                        text="" if failure == "empty-help" else "The bundled help guide",
                    )
                ],
            )

    monkeypatch.setattr(SMOKE, "stdio_client", stdio_client)
    monkeypatch.setattr(SMOKE, "ClientSession", Session)
    if failure:
        with pytest.raises(
            RuntimeError,
            match="missing tools|help failed|empty help|structured help|headless mode|application version",
        ):
            asyncio.run(SMOKE.smoke_mcp(executable, {"PYTHONPATH": "source"}))
    else:
        result = asyncio.run(SMOKE.smoke_mcp(executable, {}))
        assert result["server"] == "Choicer Voicer"
        assert result["tools"] == sorted(SMOKE.REQUIRED_MCP_TOOLS)
        assert result["mode"] == "headless"
        assert result["help_characters"] > 0
    assert calls[:3] == ["start", "initialize", "list"]
    assert calls[-2:] == ["close", "stop"]


@pytest.mark.parametrize("update_smoke", [False, True])
def test_main_checks_editor_youtube_mcp_both_separation_entrypoints_and_optional_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update_smoke: bool
) -> None:
    application = tmp_path / "portable app"
    executable = application / SMOKE.EXECUTABLE
    mcp_executable = application / SMOKE.MCP_NAME
    resources = application / "_internal" / "choicer_voicer_pack_creator" / "resources"
    for path in (
        executable,
        mcp_executable,
        application / "bin" / "ffmpeg.exe",
        application / "bin" / "ffprobe.exe",
        resources / "mcp-help.md",
        application / "licenses" / "python" / "mcp" / "METADATA.txt",
        *[
            application / "licenses" / package / "LICENSE"
            for package in ("yt-dlp", "yt-dlp-ejs", "deno")
        ],
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("smoke fixture", encoding="utf-8")
    report = {
        "ffmpeg": str(application / "bin" / "ffmpeg.exe"),
        "ffprobe": str(application / "bin" / "ffprobe.exe"),
        "version": "n9.0.1-11-ge47273f4d9-20260901",
        "analysis_manifest": str(resources / "whisper-analysis-windows-x64.json"),
        "analysis_manifest_present": True,
        "analysis_licenses_present": True,
        "whisper_runtime_build": "b4938",
        "whisper_models": ["base", "tiny"],
        "analysis_cpu_threads": 1,
        "activity_scan_regions": 1,
        "activity_scan_threshold_db": -40,
        "youtube_runtime": str(application / "_internal" / "runtime" / "deno" / "deno.exe"),
        "youtube_runtime_version": "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)",
        "youtube_ejs_present": True,
        "youtube_worker_probe_duration": 1.0,
    }
    calls = []

    def smoke_editor(command, *, env, timeout, check):
        assert command == [str(executable), "--smoke-test"]
        assert timeout == 30 and check is False
        calls.append(("editor", executable))
        Path(env["CHOICER_VOICER_SMOKE_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    async def smoke_mcp(path, environment):
        assert path == mcp_executable
        assert "CHOICER_VOICER_SMOKE_REPORT" in environment
        calls.append(("mcp", path))
        return {"mode": "headless"}

    monkeypatch.setattr(SMOKE, "ROOT", tmp_path)
    monkeypatch.setattr(
        SMOKE.sys, "argv",
        [str(SCRIPT_PATH), str(executable), *(["--update-smoke"] if update_smoke else [])],
    )
    monkeypatch.setattr(
        SMOKE, "verify_installation",
        lambda directory, version: calls.append(("verify", directory, version)),
    )
    monkeypatch.setattr(SMOKE.subprocess, "run", smoke_editor)
    monkeypatch.setattr(SMOKE, "smoke_mcp", smoke_mcp)
    monkeypatch.setattr(SMOKE, "smoke_separation", lambda path: calls.append(("separation", path)))
    monkeypatch.setattr(SMOKE, "smoke_bandit", lambda path: calls.append(("bandit", path)))
    monkeypatch.setattr(
        SMOKE, "smoke_speaker_matching", lambda path: calls.append(("speaker", path)),
    )
    monkeypatch.setattr(SMOKE, "smoke_update", lambda path: calls.append(("update", path)))

    assert SMOKE.main() == 0
    assert calls == [
        ("verify", application, SMOKE.APP_VERSION),
        ("editor", executable),
        ("mcp", mcp_executable),
        ("separation", executable),
        ("separation", mcp_executable),
        ("bandit", executable),
        ("bandit", mcp_executable),
        ("speaker", executable),
        ("speaker", mcp_executable),
        *([("update", executable)] if update_smoke else []),
    ]


@pytest.mark.parametrize("failure", ["", "runtime", "provenance", "license"])
def test_speaker_smoke_checks_native_worker_provenance_and_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    executable = tmp_path / "app" / SMOKE.EXECUTABLE
    resources = executable.parent / "_internal" / "choicer_voicer_pack_creator" / "resources"
    notices = executable.parent / "licenses"
    resources.mkdir(parents=True)
    (notices / "kaldi-native-fbank").mkdir(parents=True)
    for name in ("WeSpeaker-Attribution.txt", "WeSpeaker-CC-BY-4.0.txt", "speaker-matching.json"):
        (resources / name).write_text("notice")
        (notices / name).write_text("notice")
    (notices / "kaldi-native-fbank" / "LICENSE").write_text("Apache-2.0")
    (notices / "kaldi-native-fbank" / "KaldiNativeFbank-ThirdParty.txt").write_text("BSD notices")
    (resources / "speaker-matching.json").write_text(json.dumps({
        "model": {
            "bytes": 26530550,
            "sha256": "wrong" if failure == "provenance" else
            "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        },
    }))
    if failure == "license":
        (notices / "WeSpeaker-CC-BY-4.0.txt").unlink()

    def invoke(command, *, env, check, timeout):
        assert command[:2] == [str(executable), "--speaker-matching-smoke"]
        assert check is False and timeout == 60
        assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env
        Path(command[2]).write_text(json.dumps({
            "features": [1, 198, 80], "kaldi_native_fbank": "1.22.3",
            "numpy": "2.4.6", "onnxruntime": "1.26.0", "soundfile": "0.13.1",
            "qt_imported": failure == "runtime",
        }))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(SMOKE, "ROOT", tmp_path)
    monkeypatch.setattr(SMOKE.subprocess, "run", invoke)
    if failure:
        with pytest.raises(RuntimeError):
            SMOKE.smoke_speaker_matching(executable)
    else:
        SMOKE.smoke_speaker_matching(executable)
    assert not list((tmp_path / "build" / "speaker-smoke").iterdir())


def _bandit_fixture(root: Path) -> Path:
    application = root / "app"
    resources = application / "_internal" / "choicer_voicer_pack_creator" / "resources"
    resources.mkdir(parents=True)
    notices = application / "licenses"
    notices.mkdir()
    source = SCRIPT_PATH.parents[1] / "src" / "choicer_voicer_pack_creator"
    for filename in (
        "backing-separation-bandit.json", "BandIt-Apache-2.0.txt",
        "BandIt-CC-BY-NC-4.0.txt", "BandIt-Attribution.txt",
    ):
        contents = (source / "resources" / filename).read_bytes()
        (resources / filename).write_bytes(contents)
        (notices / filename).write_bytes(contents)
    for directory in (notices / "bandit", resources.parent / "_bandit"):
        directory.mkdir()
        for filename in ("LICENSE", "provenance.json"):
            (directory / filename).write_bytes((source / "_bandit" / filename).read_bytes())
    (notices / "BandIt-provenance.json").write_bytes(
        (source / "_bandit" / "provenance.json").read_bytes(),
    )
    openmp_libraries = []
    for name, (version, relative) in SMOKE.OPENMP_LIBRARIES.items():
        source = Path(SMOKE.metadata.distribution(name).locate_file(relative))
        target = application / "_internal" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        openmp_libraries.append({
            "distribution": name, "version": version, "path": relative.as_posix(),
            "sha256": SMOKE.sha256(source),
        })
    for name, version in SMOKE.SINGING_VERSIONS.items():
        directory = notices / "python" / name
        directory.mkdir(parents=True)
        (directory / "METADATA.txt").write_text(f"Name: {name}\nVersion: {version}\n")
        (directory / "LICENSE").write_text("license fixture")
    (notices / "singing-runtime.json").write_text(json.dumps({
        "version": 1,
        "dependencies": [{"name": name, "version": version}
                         for name, version in SMOKE.SINGING_VERSIONS.items()],
        "openmp_libraries": openmp_libraries,
    }))
    return application


@pytest.mark.parametrize("entrypoint", [SMOKE.EXECUTABLE, SMOKE.MCP_NAME])
@pytest.mark.parametrize("failure", [
    "", "runtime", "cuda", "status", "exit", "timeout", "model", "license", "source-provenance",
    "named-provenance",
    "dependency-license", "metadata-version", "transitive-missing", "checkpoint", "gpu-binary",
    "substituted-openmp", "missing-openmp", "openmp-provenance",
    "channels", "stems", "nonfinite", "other-backend",
])
def test_bandit_smoke_uses_offline_native_worker_and_requires_exact_runtime_and_notices(
    tmp_path, monkeypatch, entrypoint, failure,
):
    application = _bandit_fixture(tmp_path)
    executable = application / entrypoint
    notices = application / "licenses"
    resources = application / "_internal" / "choicer_voicer_pack_creator" / "resources"
    if failure == "model":
        path = resources / "backing-separation-bandit.json"
        manifest = json.loads(path.read_text())
        manifest["model"]["sha256"] = "unverified"
        path.write_text(json.dumps(manifest))
    elif failure == "license":
        (notices / "BandIt-CC-BY-NC-4.0.txt").unlink()
    elif failure == "source-provenance":
        (notices / "bandit" / "provenance.json").write_text("{}")
    elif failure == "named-provenance":
        (notices / "BandIt-provenance.json").write_text("{}")
    elif failure == "dependency-license":
        (notices / "python" / "librosa" / "LICENSE").unlink()
    elif failure == "metadata-version":
        (notices / "python" / "torch" / "METADATA.txt").write_text("Name: torch\nVersion: 2.8.0\n")
    elif failure == "transitive-missing":
        with (notices / "python" / "librosa" / "METADATA.txt").open("a") as stream:
            stream.write("Requires-Dist: missing-child>=1\n")
    elif failure == "checkpoint":
        (application / "bandit-combined.ckpt").write_bytes(b"must not ship")
    elif failure == "gpu-binary":
        path = application / "_internal" / "torch" / "lib" / "torch_cuda.dll"
        path.write_bytes(b"unexpected")
    elif failure == "substituted-openmp":
        (application / "_internal" / "ctranslate2" / "libiomp5md.dll").write_bytes(
            (application / "_internal" / "torch" / "lib" / "libiomp5md.dll").read_bytes(),
        )
    elif failure == "missing-openmp":
        (application / "_internal" / "torch" / "lib" / "libiomp5md.dll").unlink()
    elif failure == "openmp-provenance":
        path = notices / "singing-runtime.json"
        inventory = json.loads(path.read_text())
        inventory["openmp_libraries"][0]["sha256"] = "unverified"
        path.write_text(json.dumps(inventory))

    def invoke(command, *, cwd, env, check, timeout):
        assert command[:2] == [str(executable), "--separate-audio"]
        assert cwd == executable.parent and check is False
        assert timeout == SMOKE.BANDIT_SMOKE_TIMEOUT == 180
        assert not {"PYTHONPATH", "VIRTUAL_ENV", "KMP_DUPLICATE_LIB_OK"} & env.keys()
        assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "1"
        request = Path(command[2])
        job = request.parent
        assert Path(env["TORCH_HOME"]).parent == job
        assert json.loads(request.read_text()) == {
            "version": 1, "job_id": job.name, "smoke_test": True, "mode": "keep_singing",
        }
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        (job / "status.json").write_text(json.dumps({
            "state": "failed" if failure == "status" else "succeeded",
        }))
        report = {
            "frames": 4097, "sample_rate": 48000,
            "torch": "2.8.0+cpu", "torchaudio": "2.8.0+cpu", "cuda": None,
            "threads": 1, "interop_threads": 1, "numpy": "2.4.6", "qt_imported": False,
            "channels": 2, "stems": ["speech", "music", "sfx"], "finite": True,
            "ctranslate2_imported": False,
        }
        if failure == "runtime":
            report["qt_imported"] = True
        elif failure == "cuda":
            report["cuda"] = "12.8"
        elif failure == "channels":
            report["channels"] = 1
        elif failure == "stems":
            report["stems"] = ["speech", "music"]
        elif failure == "nonfinite":
            report["finite"] = False
        elif failure == "other-backend":
            report["ctranslate2_imported"] = True
        (job / "smoke.json").write_text(json.dumps(report))
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)

    monkeypatch.setattr(SMOKE, "ROOT", tmp_path)
    monkeypatch.setattr(SMOKE.subprocess, "run", invoke)
    if failure:
        with pytest.raises(RuntimeError):
            SMOKE.smoke_bandit(executable)
    else:
        SMOKE.smoke_bandit(executable)
    assert not list((tmp_path / "build" / "bandit-smoke").iterdir())


@pytest.mark.parametrize("automatic_root_copy", [False, True])
def test_openmp_smoke_audits_original_package_libraries_without_altering_root_copy(
    tmp_path, automatic_root_copy,
):
    application = _bandit_fixture(tmp_path)
    root_copy = application / "_internal" / "libiomp5md.dll"
    if automatic_root_copy:
        root_copy.write_bytes(b"automatically collected root copy")
    SMOKE._check_openmp_libraries(application)
    assert root_copy.exists() == automatic_root_copy
    if automatic_root_copy:
        assert root_copy.read_bytes() == b"automatically collected root copy"


def test_openmp_smoke_rejects_modified_installed_wheel_bytes(tmp_path, monkeypatch):
    application = _bandit_fixture(tmp_path)
    monkeypatch.setattr(SMOKE, "sha256", lambda _path: "0" * 64)
    with pytest.raises(RuntimeError, match="Installed OpenMP DLL fails the torch wheel RECORD hash"):
        SMOKE._check_openmp_libraries(application)


def test_singing_license_closure_includes_activated_dependency_extras(tmp_path):
    application = _bandit_fixture(tmp_path)
    root = application / "licenses"
    with (root / "python" / "torch" / "METADATA.txt").open("a") as stream:
        stream.write("Requires-Dist: helper[native]>=1\nRequires-Dist: absent; extra == 'training'\n")
    manifest_path = root / "singing-runtime.json"
    manifest = json.loads(manifest_path.read_text())
    for name, dependency in (
        ("helper", "Requires-Dist: native-helper; extra == 'native'\n"), ("native-helper", ""),
    ):
        directory = root / "python" / name
        directory.mkdir()
        (directory / "METADATA.txt").write_text(f"Name: {name}\nVersion: 1\n{dependency}")
        (directory / "LICENSE").write_text("notice")
        manifest["dependencies"].append({"name": name, "version": "1"})
    manifest_path.write_text(json.dumps(manifest))
    SMOKE._check_singing_licenses(application)
    manifest["dependencies"] = [
        entry for entry in manifest["dependencies"] if entry["name"] != "native-helper"
    ]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Missing recursive singing dependency: native-helper"):
        SMOKE._check_singing_licenses(application)


@pytest.mark.parametrize("wrong_numpy", [False, True])
def test_existing_htdemucs_smoke_keeps_exact_version_assertions_and_sanitizes_child(
    tmp_path, monkeypatch, wrong_numpy,
):
    executable = tmp_path / "app" / SMOKE.EXECUTABLE
    resources = executable.parent / "_internal" / "choicer_voicer_pack_creator" / "resources"
    resources.mkdir(parents=True)
    (resources / "backing-separation.json").write_text(json.dumps({
        "model": {"sha256": "68d0bf16428ef66e692cdff8a9ccf28f1ef3f69440d57e58605a4cc55fcc5e74"},
    }))
    notices = executable.parent / "licenses"
    notices.mkdir()
    for name in ("StemSplit-MIT.txt", "Demucs-MIT.txt"):
        (resources / name).write_text("notice")
        (notices / name).write_text("notice")
    for name in ("onnxruntime", "numpy", "soundfile", "cffi", "pycparser",
                 "flatbuffers", "protobuf", "packaging"):
        (notices / name).mkdir()
        (notices / name / "LICENSE").write_text("notice")

    def invoke(command, *, env, check, timeout):
        assert check is False and timeout == 60
        assert "PYTHONPATH" not in env
        job = Path(command[2]).parent
        (job / "status.json").write_text('{"state": "succeeded"}')
        (job / "smoke.json").write_text(json.dumps({
            "frames": 83, "sample_rate": 44100, "numpy": "1.26.4" if wrong_numpy else "2.4.6",
            "onnxruntime": "1.26.0", "soundfile": "0.13.1", "qt_imported": False,
        }))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(SMOKE, "ROOT", tmp_path)
    monkeypatch.setattr(SMOKE.subprocess, "run", invoke)
    if wrong_numpy:
        with pytest.raises(RuntimeError, match="Unexpected packaged separation runtime"):
            SMOKE.smoke_separation(executable)
    else:
        SMOKE.smoke_separation(executable)
    assert not list((tmp_path / "build" / "separation-smoke").iterdir())
