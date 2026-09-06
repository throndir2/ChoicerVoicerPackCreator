"""Dispatch the headless worker before PyInstaller's automatic Qt runtime hook."""
import sys
from pathlib import Path

if len(sys.argv) == 3 and sys.argv[1] == "--separate-audio":
    from choicer_voicer_pack_creator.separation_worker import worker_main

    raise SystemExit(worker_main(Path(sys.argv[2])))
