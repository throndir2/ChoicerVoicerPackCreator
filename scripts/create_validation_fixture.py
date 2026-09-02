from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment

PACK_TITLE = "Pack Creator Validation Fixture"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a small synthetic exported pack for release validation."
    )
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = args.destination.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    media = MediaTools()
    source = root / "source.mp4"
    media.run(
        [
            media.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=24:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=523.25:sample_rate=48000:duration=3",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        "Creating release fixture source",
    )
    project = PackProject(
        title=PACK_TITLE,
        authors=["Automated Validation"],
        readme="Synthetic media generated locally for release validation.",
        video_path=str(source),
        video_duration=3.0,
        video_height=360,
        video_fps=24,
        segments=[
            Segment(0.25, 1.15, "First test line.", ["Narrator"]),
            Segment(1.35, 2.45, "Together!", ["Fischl"]),
            Segment(1.35, 2.45, "Together!", ["Diluc"]),
        ],
    )
    result = PackExporter(media).export(project, root / "output")
    print(result.pack_path)
    print(result.zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
