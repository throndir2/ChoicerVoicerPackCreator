from __future__ import annotations

import importlib.util
import json
import sys
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


def _prepare_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    distribution = tmp_path / "dist" / "vtest"
    application = distribution / "portable-build" / "Choicer Voicer Pack Creator"
    application.mkdir(parents=True)
    executable = application / "Choicer Voicer Pack Creator.exe"
    executable.write_bytes(b"application")
    mcp_executable = application / "Choicer Voicer MCP.exe"
    mcp_executable.write_bytes(b"console application")
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
    (candidate.parent / BUILD_SCRIPT.APP_NAME / f"{BUILD_SCRIPT.MCP_NAME}.exe").unlink()

    with pytest.raises(RuntimeError, match="incomplete"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"


def test_mcp_executable_must_share_editor_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_candidate(tmp_path, monkeypatch)
    pending = BUILD_SCRIPT.PENDING_BUILD_MANIFEST
    manifest = json.loads(pending.read_text(encoding="utf-8"))
    other = BUILD_SCRIPT.DIST / f"{BUILD_SCRIPT.MCP_NAME}.exe"
    other.write_bytes(b"wrong location")
    manifest["mcp_executable"] = other.relative_to(tmp_path).as_posix()
    pending.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="share the editor"):
        BUILD_SCRIPT.promote_candidate("build-id")


def test_spec_builds_two_entrypoints_from_one_shared_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository with ' quotes"
    monkeypatch.setattr(BUILD_SCRIPT, "ROOT", root)
    monkeypatch.setattr(BUILD_SCRIPT, "BUILD", root / "build")
    monkeypatch.setattr(BUILD_SCRIPT, "_mcp_distribution_names", lambda: ["mcp", "cryptography"])
    analyses = []
    executables = []
    collections = []
    hook_calls = []
    for name in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    hooks = sys.modules["PyInstaller.utils.hooks"]

    def collect_data_files(name):
        hook_calls.append(("data", name))
        return [("sdk-data", "mcp")]

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
    hooks.collect_submodules = collect_submodules
    hooks.copy_metadata = copy_metadata

    def analysis(paths, **kwargs):
        result = SimpleNamespace(
            scripts=[
                ("runtime_hook", str(root / "runtime_hook.py"), "PYSOURCE"),
                *[(Path(path).stem, path, "PYSOURCE") for path in paths],
            ],
            pure=object(),
            binaries=object(),
            datas=kwargs["datas"],
            hiddenimports=kwargs["hiddenimports"],
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
    assert ("metadata", "mcp") in hook_calls
    assert ("metadata", "cryptography") in hook_calls
    assert "mcp.dynamic_module" in analyses[0].hiddenimports
    assert "anyio._backends._asyncio" in analyses[0].hiddenimports


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


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("headless", [False, True])
def test_help_configuration_selects_source_or_console_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen: bool, headless: bool
) -> None:
    from choicer_voicer_pack_creator.ui.mcp_help_dialog import mcp_client_configuration

    executable = tmp_path / "Unicode ü path" / (
        "Choicer Voicer Pack Creator.exe" if frozen else "python.exe"
    )
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    configuration = mcp_client_configuration(headless=headless)
    entry = configuration["mcpServers"]["choicer-voicer"]
    assert entry["command"] == str(
        executable.with_name("Choicer Voicer MCP.exe") if frozen else executable
    )
    expected = [] if frozen else ["-m", "choicer_voicer_pack_creator", "--mcp"]
    if headless:
        expected.append("--headless")
    assert entry["args"] == expected
    assert json.loads(json.dumps(configuration)) == configuration


def test_help_dialog_reads_bundled_guide_and_copies_configuration(qtbot, qapp) -> None:
    from choicer_voicer_pack_creator.ui.mcp_help_dialog import McpHelpDialog

    dialog = McpHelpDialog()
    qtbot.addWidget(dialog)
    assert dialog.help_browser.isReadOnly()
    assert "model provider" in dialog.help_browser.toPlainText()
    assert dialog.configuration.isReadOnly()
    assert not dialog.headless_check.isChecked()
    assert "Live editor" in dialog.mode_label.text()
    dialog.copy_button.click()
    assert qapp.clipboard().text() == dialog.configuration.toPlainText()
    assert dialog.copy_status.text() == "Configuration copied."
    dialog.headless_check.setChecked(True)
    entry = json.loads(dialog.configuration.toPlainText())["mcpServers"]["choicer-voicer"]
    assert entry["args"][-1] == "--headless"
    assert "Headless" in dialog.mode_label.text()
    assert dialog.copy_status.text() == ""
    dialog.copy_button.click()
    assert qapp.clipboard().text() == dialog.configuration.toPlainText()


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
