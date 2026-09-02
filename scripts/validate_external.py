from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GODOT_PROJECT = ROOT / "tools" / "godot-validator"
XIPH_IMAGE = "choicer-voicer-pack-creator-ogg-validator:1.0"


def find_godot() -> str | None:
    direct = shutil.which("godot_console") or shutil.which("godot")
    if direct:
        return direct
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        matches = sorted(local.glob("GodotEngine.GodotEngine_*\\**\\*console*.exe"))
        if not matches:
            matches = sorted(local.glob("GodotEngine.GodotEngine_*\\**\\Godot*.exe"))
        if matches:
            return str(matches[-1])
    return None


def run(command: list[str], description: str) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{description} failed (exit {completed.returncode}):\n{output}")
    return output


def validate_godot(pack: Path, required: bool) -> str:
    godot = find_godot()
    if not godot:
        if required:
            raise RuntimeError("Godot was not found")
        return "SKIPPED: Godot was not found"
    output = run(
        [
            godot,
            "--headless",
            "--path",
            str(GODOT_PROJECT),
            "--script",
            str(GODOT_PROJECT / "validate_pack.gd"),
            "--",
            str(pack),
        ],
        "Godot ConfigFile validation",
    )
    if "GODOT CONFIGFILE VALIDATION PASSED" not in output:
        raise RuntimeError(f"Godot did not emit its pass marker:\n{output}")
    return output


def validate_xiph(pack: Path, required: bool) -> str:
    docker = shutil.which("docker")
    if not docker:
        if required:
            raise RuntimeError("Docker was not found")
        return "SKIPPED: Docker was not found"
    run(
        [
            docker,
            "build",
            "--pull",
            "-t",
            XIPH_IMAGE,
            str(ROOT / "tools" / "ogg-validator"),
        ],
        "Building Xiph validator image",
    )
    output = run(
        [
            docker,
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={pack},target=/pack,readonly",
            XIPH_IMAGE,
            "/pack/dub_video.ogv",
        ],
        "Xiph Ogg/Theora/Vorbis validation",
    )
    if "XIPH OGV VALIDATION PASSED" not in output:
        raise RuntimeError(f"Xiph tools did not emit their pass marker:\n{output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run runtime-equivalent Godot and Xiph validators against an exported pack."
    )
    parser.add_argument("pack", type=Path)
    parser.add_argument("--require-godot", action="store_true")
    parser.add_argument("--require-xiph", action="store_true")
    args = parser.parse_args()
    pack = args.pack.resolve()
    if not pack.is_dir():
        parser.error(f"pack directory does not exist: {pack}")
    print(validate_godot(pack, args.require_godot))
    print(validate_xiph(pack, args.require_xiph))
    print("EXTERNAL PACK VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
