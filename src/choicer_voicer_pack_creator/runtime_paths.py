from __future__ import annotations

import sys
from pathlib import Path

MCP_EXECUTABLE = "Choicer Voicer MCP.exe"


def application_directory(executable: Path | None = None) -> Path:
    if executable is None:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            # Both PyInstaller entry points load the same root-level _internal directory.
            return Path(sys._MEIPASS).resolve().parent
        executable = Path(sys.executable)
    executable = executable.resolve()
    if executable.name == MCP_EXECUTABLE and executable.parent.name in {"MCP", "_internal"}:
        return executable.parent.parent
    return executable.parent
