from pathlib import Path

import pytest

from choicer_voicer_pack_creator import runtime_paths, updates


@pytest.mark.parametrize("entrypoint", [
    "Choicer Voicer Pack Creator.exe",
    r"MCP\Choicer Voicer MCP.exe",
    r"_internal\Choicer Voicer MCP.exe",
])
def test_portable_root_is_shared_by_both_entrypoints(tmp_path, monkeypatch, entrypoint):
    root = tmp_path / "Portable app ü"
    executable = root / Path(entrypoint.replace("\\", "/"))
    monkeypatch.setattr(runtime_paths.sys, "platform", "win32")
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(root / "_internal"), raising=False)
    monkeypatch.setattr(runtime_paths.sys, "executable", str(executable))
    assert runtime_paths.application_directory() == root
    assert runtime_paths.application_directory(executable) == root
    assert updates.installation_directory() == root


def test_source_installation_has_no_self_update_directory(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    monkeypatch.setattr(runtime_paths.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "executable", str(executable))
    assert runtime_paths.application_directory() == tmp_path
    assert updates.installation_directory() is None
