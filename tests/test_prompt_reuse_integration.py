from __future__ import annotations

import shutil
import time
from collections import Counter
from functools import wraps
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.exporter import PackExporter, sha256
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment


@pytest.mark.integration
def test_verified_prompt_bytes_survive_renaming_duplicates_and_reindexing(
    tmp_path: Path, monkeypatch, record_property,
) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    media = MediaTools()
    source = tmp_path / "synthetic.mp4"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=0x14283d:s=320x180:r=12:d=2", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=2", "-shortest",
        "-c:v", "mpeg4", "-threads", "1", "-c:a", "aac", str(source),
    ], "Creating small synthetic prompt reuse fixture")
    source_hash = sha256(source)
    calls = Counter()

    def track(name, original):
        @wraps(original)
        def run(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        return run

    for name in ("convert_video", "extract_prompt", "has_audio_activity", "extract_frame"):
        monkeypatch.setattr(media, name, track(name, getattr(media, name)))
    project = PackProject(
        title="Prompt Reuse", authors=["Synthetic fixture"], video_path=str(source),
        video_duration=2.0,
        segments=[Segment(0.2, 0.8, "First", ["Alice"]), Segment(1.2, 1.8, "Second", ["Bob"])],
    )
    cache_root = tmp_path / "cache"
    exporter = PackExporter(media, cache_root=cache_root, prompt_workers=1)
    for name in ("validate_folder", "validate_zip"):
        monkeypatch.setattr(exporter.validator, name, track(name, getattr(exporter.validator, name)))
    started = time.perf_counter()
    cold = exporter.export(project, tmp_path / "output")
    record_property("cold_seconds", time.perf_counter() - started)
    assert calls["extract_prompt"] == calls["has_audio_activity"] == calls["extract_frame"] == 2

    first, second = project.segments
    first.caption = "Current caption"
    first.characters = ["Renamed"]
    duplicate = first.clone()
    duplicate.characters = ["Duplicate"]
    project.segments = [second, duplicate, first]
    repeated_exporter = PackExporter(media, cache_root=cache_root, prompt_workers=1)
    repeated_exporter.validator = exporter.validator
    started = time.perf_counter()
    warm = repeated_exporter.export(project, tmp_path / "output")
    record_property("warm_seconds", time.perf_counter() - started)
    record_property("warm_generation_subprocesses", 0)
    assert calls["extract_prompt"] == calls["has_audio_activity"] == calls["extract_frame"] == 2
    assert calls["convert_video"] == 1
    assert calls["validate_folder"] == calls["validate_zip"] == 4
    for new, old in (
        ("001_Duplicate", "001_Alice"), ("002_Renamed", "001_Alice"), ("003_Bob", "002_Bob"),
    ):
        for extension in ("mp3", "png"):
            assert warm.file_hashes[f"{new}.{extension}"] == cold.file_hashes[f"{old}.{extension}"]
    metadata = read_config(warm.pack_path / "002_Renamed.txt")["data"]
    assert metadata["caption"] == "Current caption"
    assert metadata["dub_characters"] == ["Renamed"]
    assert metadata["dub_timestamps"] == [0.05]
    assert warm.validation["status"] == "passed"
    assert sha256(source) == source_hash
