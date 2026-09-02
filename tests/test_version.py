from __future__ import annotations

import tomllib
from pathlib import Path

from choicer_voicer_pack_creator import __version__


def test_runtime_version_matches_project_metadata() -> None:
    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_path.open("rb") as project_file:
        project_version = str(tomllib.load(project_file)["project"]["version"])
    assert __version__ == project_version