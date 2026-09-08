from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows setup profile")
SETUP = Path(__file__).resolve().parents[1] / "tools" / "separation-comparison" / "Setup-Gpu.ps1"


def ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("probe_exit", [0, 17])
def test_setup_uses_selected_interpreter_and_stops_on_failure(tmp_path, explicit, probe_exit):
    launcher = tmp_path / "Python interpreter.ps1"
    launcher.write_text(
        "Write-Output ('CALL:' + (ConvertTo-Json -Compress -InputObject @($args)))\n"
        f"if ($args -contains '-c') {{ $global:LASTEXITCODE = {probe_exit} }}\n"
        "else { $global:LASTEXITCODE = 23 }\n",
    )
    environment = tmp_path / "new environment"
    command = f"Set-Alias py {ps_quote(launcher)}; & {ps_quote(SETUP)}"
    command += f" -Backend BandIt -Environment {ps_quote(environment)}"
    if explicit:
        command += f" -PythonExecutable {ps_quote(launcher)}"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    calls = [
        json.loads(line.removeprefix("CALL:"))
        for line in result.stdout.splitlines() if line.startswith("CALL:")
    ]
    prefix = [] if explicit else ["-3.11"]
    assert calls[0][:-1] == [*prefix, "-c"]
    assert len(calls) == (1 if probe_exit else 2)
    if not probe_exit:
        assert calls[1] == [*prefix, "-m", "venv", str(environment)]
    assert not environment.exists()

    probe = calls[0][-1]
    for version, size, expected in [((3, 11), 8, 0), ((3, 12), 8, 1), ((3, 11), 4, 1)]:
        checked = subprocess.run(
            [sys.executable, "-c",
             f"import sys, struct; sys.version_info = {version}; "
             f"struct.calcsize = lambda _: {size}; {probe}"],
            capture_output=True, text=True, check=False,
        )
        assert checked.returncode == expected
        if expected:
            assert "64-bit Python 3.11" in checked.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_setup_rejects_unsafe_environment_before_invoking_python(tmp_path, existing):
    environment = tmp_path if existing else Path("relative-environment")
    command = f"& {ps_quote(SETUP)} -Backend SamAudio -Environment {ps_quote(environment)}"
    command += " -PythonExecutable deliberately-missing-python"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert (
        "Existing environments are never modified" if existing else "must be an absolute path"
    ) in result.stderr
