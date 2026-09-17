from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
WHEELS = ROOT / "tools" / "singing-cpu-wheels.txt"
BUILD = ROOT / "Build-Portable.ps1"
HASHES = {
    ("torch", "3.11"): "7631ef49fbd38d382909525b83696dc12a55d68492ade4ace3883c62b9fc140f",
    ("torch", "3.12"): "2be20b2c05a0cce10430cc25f32b689259640d273232b2de357c35729132256d",
    ("torchaudio", "3.11"): "db37df7eee906f8fe0a639fdc673f3541cb2e173169b16d4133447eb922d1938",
    ("torchaudio", "3.12"): "9b302192b570657c1cc787a4d487ae4bbb7f2aab1c01b1fcc46757e7f86f391e",
}


def wheel_requirements():
    lines = WHEELS.read_text(encoding="utf-8").replace("\\\n", "").splitlines()
    return [
        (Requirement(line.split("--hash=")[0].strip()), line.split("--hash=sha256:")[1].strip())
        for line in lines if line.strip() and not line.startswith("#")
    ]


@pytest.mark.parametrize("python", ["3.11", "3.12"])
def test_official_cpu_wheels_have_exact_abi_urls_and_hashes(python):
    selected = [
        (requirement, digest) for requirement, digest in wheel_requirements()
        if requirement.marker.evaluate({
            "python_version": python, "sys_platform": "win32", "platform_machine": "AMD64",
            "implementation_name": "cpython",
        })
    ]
    assert len(selected) == 2
    assert {requirement.name for requirement, _digest in selected} == {"torch", "torchaudio"}
    for requirement, digest in selected:
        abi = "cp" + python.replace(".", "")
        url = urlparse(requirement.url)
        assert url.scheme == "https" and url.netloc == "download.pytorch.org"
        assert unquote(url.path) == (
            f"/whl/cpu/{requirement.name}-2.8.0+cpu-{abi}-{abi}-win_amd64.whl"
        )
        assert digest == HASHES[(requirement.name, python)]


@pytest.mark.parametrize("overrides", [
    {"python_version": "3.13"}, {"sys_platform": "linux"},
    {"platform_machine": "ARM64"}, {"implementation_name": "pypy"},
])
def test_unsupported_source_backends_do_not_select_wrong_cpu_wheels(overrides):
    environment = {
        "python_version": "3.12", "sys_platform": "win32", "platform_machine": "AMD64",
        "implementation_name": "cpython", **overrides,
    }
    assert not any(requirement.marker.evaluate(environment)
                   for requirement, _digest in wheel_requirements())


def test_extra_keeps_core_compatibility_and_requires_exact_cpu_versions():
    assert PROJECT["project"]["requires-python"] == ">=3.11"
    dependencies = PROJECT["project"]["dependencies"]
    assert {"numpy==2.4.6", "soundfile==0.13.1", "onnxruntime==1.26.0"} <= set(dependencies)
    extra = PROJECT["project"]["optional-dependencies"]["singing"]
    assert set(extra) == {
        "torch==2.8.0+cpu", "torchaudio==2.8.0+cpu", "librosa==0.10.2.post1",
        "scipy==1.15.3", "numba==0.67.0", "llvmlite==0.49.0",
    }
    for name in ("torch", "torchaudio"):
        requirement = next(Requirement(item) for item in extra if item.startswith(name + "=="))
        assert not requirement.specifier.contains("2.8.0")
        assert not requirement.specifier.contains("2.8.0+cu128")
        assert requirement.specifier.contains("2.8.0+cpu")
    assert PROJECT["tool"]["setuptools"]["package-data"]["choicer_voicer_pack_creator._bandit"] == [
        "LICENSE", "provenance.json",
    ]


def test_installed_source_contains_vendor_license_and_provenance_without_torch_import():
    completed = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import json,sys; from importlib.resources import files; "
            "root=files('choicer_voicer_pack_creator._bandit'); "
            "assert 'Apache' in root.joinpath('LICENSE').read_text(encoding='utf-8'); "
            "assert json.loads(root.joinpath('provenance.json').read_text(encoding='utf-8'))"
            "['revision']=='d5563d9031e95fdaa3e5a73d5020b9a0df61adb6'; "
            "assert not any(name.startswith(('torch','PySide6')) for name in sys.modules)",
        ],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_build_installs_cpu_wheels_before_extra_with_isolated_public_sources():
    source = BUILD.read_text(encoding="utf-8")
    assert '[string] $BuildEnvironment' in source
    assert '"--no-index", "--no-deps", "--require-hashes"' in source
    assert '"--index-url", "https://pypi.org/simple"' in source
    assert source.index('"tools\\singing-cpu-wheels.txt"') < source.index('".[build,singing]"')
    assert source.count('"-m", "pip", "--isolated", "install"') == 2
    assert '"-m", "pip", "--isolated", "check"' in source
    assert "$env:PIP_CONFIG_FILE = $previousPipConfig" in source
    assert "GetTempPath" not in source
    assert "--extra-index-url" not in source


@pytest.mark.skipif(os.name != "nt", reason="Windows pip null-device spelling")
def test_pip_setup_really_ignores_global_site_and_environment_configuration(tmp_path, monkeypatch):
    from pip._internal import configuration

    source = BUILD.read_text(encoding="utf-8")
    null_file = re.search(r'\$env:PIP_CONFIG_FILE = "([^"]+)"', source).group(1)
    assert null_file == os.devnull
    config_file = tmp_path / "pip.ini"
    config_file.write_text("[global]\nextra-index-url = https://invalid.example/simple\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", null_file)
    monkeypatch.setenv("PIP_INDEX_URL", "https://invalid.example/simple")
    monkeypatch.setattr(configuration, "get_configuration_files", lambda: {
        configuration.kinds.GLOBAL: [str(config_file)],
        configuration.kinds.USER: [str(config_file)],
        configuration.kinds.SITE: [str(config_file)],
    })
    config = configuration.Configuration(isolated=True)
    config.load()
    assert dict(config.items()) == {}


def _validate_environment(path: Path, repository: Path, *, reset: bool):
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    script = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {quote(BUILD)}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw ($parseErrors | Out-String) }}
$function = $ast.Find({{ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Assert-SafeBuildEnvironment'
}}, $true)
Invoke-Expression $function.Extent.Text
$buildEnvironment = {quote(path)}
$repositoryRoot = {quote(repository)}
$environmentMarker = Join-Path $buildEnvironment '.cvpc-build-environment.json'
$ResetBuildEnvironment = ${str(reset).lower()}
Assert-SafeBuildEnvironment
"""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows build entrypoint")
@pytest.mark.parametrize("path_kind", ["repository", "ancestor", "drive", "git"])
def test_build_environment_rejects_dangerous_paths_before_mutation(tmp_path, path_kind):
    repository = tmp_path / "repository"
    repository.mkdir()
    path = {
        "repository": repository, "ancestor": tmp_path, "drive": Path(tmp_path.anchor),
        "git": repository / ".git" / "environment",
    }[path_kind]
    result = _validate_environment(path, repository, reset=True)
    assert result.returncode != 0 and "Unsafe build environment path" in result.stderr
    assert repository.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows build entrypoint")
@pytest.mark.parametrize("marker_kind", ["missing", "wrong-path", "wrong-repository", "valid"])
def test_build_environment_reset_requires_valid_task_owned_venv(tmp_path, marker_kind):
    repository = tmp_path / "repository"
    path = repository / "build" / "environment"
    path.mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = Python")
    if marker_kind != "missing":
        (path / ".cvpc-build-environment.json").write_text(json.dumps({
            "version": 1,
            "path": str(tmp_path if marker_kind == "wrong-path" else path),
            "repository": str(tmp_path if marker_kind == "wrong-repository" else repository),
        }))
    result = _validate_environment(path, repository, reset=True)
    assert (result.returncode == 0) == (marker_kind == "valid"), result.stderr
    assert (path / "pyvenv.cfg").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows build entrypoint")
def test_new_task_owned_build_environment_path_is_accepted(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    path = repository / "build" / "environment"
    result = _validate_environment(path, repository, reset=False)
    assert result.returncode == 0, result.stderr
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows frozen native loader")
def test_frozen_separation_hook_does_not_preload_native_libraries(tmp_path, monkeypatch):
    import ctypes

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "argv", ["editor.exe"])
    before = set(sys.modules)

    def forbid_native_setup(*_args):
        raise AssertionError("The separation dispatch hook must not preload native libraries")

    monkeypatch.setattr(os, "add_dll_directory", forbid_native_setup)
    monkeypatch.setattr(ctypes, "WinDLL", forbid_native_setup)
    values = runpy.run_path(str(ROOT / "scripts" / "separation_runtime_hook.py"))
    assert "_shared_dll_directory" not in values and "_shared_openmp" not in values
    assert not any(
        name.startswith(("torch", "ctranslate2", "PySide6")) for name in set(sys.modules) - before
    )
