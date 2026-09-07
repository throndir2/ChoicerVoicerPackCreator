from __future__ import annotations

import json
import os
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import export_cache
from choicer_voicer_pack_creator import exporter as exporter_module
from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    check_cancelled,
)
from choicer_voicer_pack_creator.validation import PackValidator


class FakeMedia:
    def __init__(self):
        self.calls = Counter()
        self.output_size = (1280, 720)

    def probe(self, path):
        check_cancelled()
        width, height = self.output_size if path.name == "dub_video.ogv" else (1280, 720)
        return SimpleNamespace(
            duration=2.0, width=width, height=height, fps=30, has_audio=True,
            video_codec="theora", audio_codec="vorbis", pixel_format="yuv420p",
            audio_sample_rate=48000, audio_channels=2,
        )

    def convert_video(self, source, destination, height, fps, *, encoding_progress=None):
        self.calls["video"] += 1
        height = min(720, height)
        self.output_size = (int(1280 * height / 720) // 2 * 2, height)
        destination.write_bytes(source.read_bytes() + f" video {height} {fps}".encode())

    def make_icon(self, _source, destination, *, is_video):
        destination.write_bytes(b"icon")

    def create_silent_backing(self, destination, _duration):
        destination.write_bytes(b"backing")

    def audio_peak_dbfs(self, _path):
        return float("-inf")

    def extract_prompt(self, source, start, end, head, tail, destination):
        self.calls["audio"] += 1
        self.calls["source_activity"] += 1
        actual_head = min(start, head)
        destination.write_text(json.dumps({
            "source": source.read_text(), "start": start, "end": end,
            "head": actual_head, "tail": tail, "duration": end - start + actual_head + tail,
        }), encoding="utf-8")
        return start - actual_head

    def decoded_audio_stats(self, path):
        self.calls["stats"] += 1
        data = json.loads(path.read_bytes())
        return SimpleNamespace(
            duration=data["duration"], leading_quiet=data["head"],
            trailing_quiet=data["tail"], has_activity=True,
        )

    def extract_frame(self, source, timestamp, destination, *, size):
        self.calls["image"] += 1
        destination.write_text(
            json.dumps([source.read_text(), timestamp, size]), encoding="utf-8",
        )

    def probe_audio(self, _path):
        self.calls["import_audio_probe"] += 1
        return SimpleNamespace(codec="mp3", sample_rate=48000, channels=1)

    def probe_image_dimensions(self, _path):
        self.calls["custom_image_probe"] += 1
        return self.output_size

    def convert_audio(self, source, destination, mono):
        self.calls["import_audio_conversion"] += 1
        destination.write_bytes(source.read_bytes())

    def convert_image(self, source, destination, width, height):
        self.calls["custom_image_conversion"] += 1
        destination.write_bytes(source.read_bytes())


class FakeValidator:
    def __init__(self):
        self.folders = []
        self.zips = []
        self.fail = ""

    def validate_folder(self, folder, expected_clips, *, progress=None):
        phase = "staged" if ".staging-" in str(folder.parent) else "published"
        self.folders.append(phase)
        if self.fail == phase:
            raise RuntimeError(f"injected {phase} validation failure")
        check_cancelled()
        assert len(list(folder.iterdir())) == 4 + 3 * expected_clips
        return {"status": "passed", "clip_count": expected_clips}

    def validate_zip(self, path, folder_name, inventory):
        self.zips.append(path)
        PackValidator.validate_zip(path, folder_name, inventory)


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "source.ogv"
    source.write_text("synthetic source bytes", encoding="utf-8")
    project = PackProject(
        title="Pack", authors=["Author"], video_path=str(source), video_duration=2.0,
        segments=[
            Segment(0.2, 0.8, "First", ["Alice"]),
            Segment(1.2, 1.8, "Second", ["Bob"]),
        ],
    )
    media = FakeMedia()
    exporter = PackExporter(media, cache_root=tmp_path / "cache", prompt_workers=1)
    exporter.validator = FakeValidator()
    return exporter, project, tmp_path / "output"


def prompt_receipt(exporter):
    return next(exporter.prompt_cache.root.glob("*.json"))


def generation_counts(exporter):
    return exporter.media.calls["audio"], exporter.media.calls["image"]


def test_repeat_export_skips_generation_across_instances_but_retains_all_checks(fixture):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent)
    assert generation_counts(exporter) == (2, 2)
    other = PackExporter(exporter.media, cache_root=exporter.prompt_cache.root.parent, prompt_workers=1)
    other.validator = exporter.validator
    warm = other.export(project, parent)
    assert generation_counts(other) == (2, 2)
    assert exporter.media.calls["source_activity"] == 2
    assert exporter.media.calls["stats"] == 4
    assert warm.file_hashes == cold.file_hashes
    assert exporter.validator.folders == ["staged", "published"] * 2
    assert len(exporter.validator.zips) == 4
    assert len(warm.file_hashes) == 10


def test_renaming_reindexing_duplicate_ranges_and_metadata_use_content_identity(fixture):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    old_audio = (cold.pack_path / "001_Alice.mp3").read_bytes()
    old_image = (cold.pack_path / "001_Alice.png").read_bytes()
    first, second = project.segments
    duplicate = first.clone()
    duplicate.characters = ["Duplicate"]
    first.characters = ["Renamed"]
    first.caption = "Edited caption"
    project.segments = [
        second, duplicate, first, Segment(0, 0.1, "Inserted before existing prompts", ["New"]),
    ]
    warm = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (3, 3)
    for base in ("002_Duplicate", "003_Renamed"):
        assert (warm.pack_path / f"{base}.mp3").read_bytes() == old_audio
        assert (warm.pack_path / f"{base}.png").read_bytes() == old_image
    metadata = read_config(warm.pack_path / "003_Renamed.txt")["data"]
    assert metadata["caption"] == "Edited caption"
    assert metadata["dub_characters"] == ["Renamed"]
    assert metadata["dub_timestamps"] == [0.05]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("audio-range-same-midpoint", (3, 2)), ("range", (3, 3)),
        ("head", (4, 2)), ("tail", (4, 2)), ("quality", (2, 4)),
        ("fps", (2, 2)), ("audio-recipe", (4, 2)), ("image-recipe", (2, 4)),
        ("source-bytes-same-stat", (4, 4)), ("same-bytes-new-path", (2, 2)),
        ("destination", (4, 4)),
    ],
)
def test_independent_invalidation(fixture, monkeypatch, change, expected):
    exporter, project, parent = fixture
    exporter.export(project, parent, create_zip=False)
    first = project.segments[0]
    if change == "audio-range-same-midpoint":
        first.start, first.end = 0.1, 0.9
    elif change == "range":
        first.end = 0.9
    elif change == "head":
        project.head_padding = 0.05
    elif change == "tail":
        project.tail_padding = 0.05
    elif change == "quality":
        project.video_height = 720
    elif change == "fps":
        project.video_fps = 24
    elif change.endswith("-recipe"):
        monkeypatch.setattr(export_cache, f"PROMPT_{change.split('-')[0].upper()}_RECIPE", 2)
    elif change == "source-bytes-same-stat":
        source = Path(project.video_path)
        before = source.stat()
        source.write_bytes(b"x" * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "same-bytes-new-path":
        source = Path(project.video_path)
        other = source.with_name("other-source.ogv")
        other.write_bytes(source.read_bytes())
        project.video_path = str(other)
    elif change == "destination":
        parent = parent / "another"
    exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == expected


def test_effective_padding_clamps_preserve_hits_and_update_timestamps(fixture):
    exporter, project, parent = fixture
    project.segments = [Segment(0.05, 1.95, "Both boundaries", ["Speaker"])]
    exporter.export(project, parent, create_zip=False)
    project.head_padding = project.tail_padding = 2
    result = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (1, 1)
    assert read_config(result.pack_path / "001_Speaker.txt")["data"]["dub_timestamps"] == [0.0]
    project.head_padding = 0.01
    result = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 1)
    assert read_config(result.pack_path / "001_Speaker.txt")["data"]["dub_timestamps"] == [0.04]


@pytest.mark.parametrize("preserve", [False, True])
def test_source_hash_is_once_per_export_including_preserved_ogv(fixture, monkeypatch, preserve):
    exporter, project, parent = fixture
    project.preserve_source_video = preserve
    project.video_height = 720
    source = Path(project.video_path)
    hashes = 0
    original = exporter_module.sha256

    def tracked(path):
        nonlocal hashes
        if path == source:
            hashes += 1
        return original(path)

    monkeypatch.setattr(exporter_module, "sha256", tracked)
    exporter.export(project, parent, create_zip=False)
    exporter.export(project, parent, create_zip=False)
    assert hashes == 2
    assert generation_counts(exporter) == (2, 2)
    assert exporter.media.calls["video"] == (0 if preserve else 1)
    assert len(list(exporter.video_cache.root.glob("*.json"))) == (0 if preserve else 1)


def test_imported_assets_bypass_generated_reuse_independently(fixture, tmp_path):
    exporter, project, parent = fixture
    exporter.export(project, parent, create_zip=False)
    audio = tmp_path / "custom.mp3"
    image = tmp_path / "custom.png"
    audio.write_bytes(b"chosen audio")
    image.write_bytes(b"chosen still")
    project.segments[0].audio_mode = "file"
    project.segments[0].audio_path = str(audio)
    project.segments[1].image_path = str(image)
    result = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 2)
    assert (result.pack_path / "001_Alice.mp3").read_bytes() == b"chosen audio"
    assert (result.pack_path / "002_Bob.png").read_bytes() == b"chosen still"
    audio.write_bytes(b"changed audio")
    image.write_bytes(b"changed still")
    result = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 2)
    assert (result.pack_path / "001_Alice.mp3").read_bytes() == b"changed audio"
    assert (result.pack_path / "002_Bob.png").read_bytes() == b"changed still"
    assert exporter.media.calls["import_audio_probe"] == 2
    assert exporter.media.calls["custom_image_probe"] == 2


@pytest.mark.parametrize("kind", ["audio", "image"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_previous_asset_regenerates_only_that_asset(fixture, kind, damage):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    path = cold.pack_path / f"001_Alice.{'mp3' if kind == 'audio' else 'png'}"
    if damage == "missing":
        path.unlink()
    else:
        original = path.stat()
        path.write_bytes(b"x" * original.st_size)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    warm = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == ((3, 2) if kind == "audio" else (2, 3))
    assert warm.file_hashes == cold.file_hashes


@pytest.mark.parametrize("phase", ["staged", "published"])
def test_validation_failure_preserves_previous_output_and_receipt(fixture, phase):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent)
    receipt = prompt_receipt(exporter)
    before = receipt.read_bytes()
    project.segments[0].caption = "Unpublished caption"
    exporter.validator.fail = phase
    with pytest.raises(RuntimeError, match=f"injected {phase}"):
        exporter.export(project, parent)
    assert receipt.read_bytes() == before
    assert {
        path.name: exporter_module.sha256(path) for path in cold.pack_path.iterdir()
    } == cold.file_hashes
    assert set(path.name for path in parent.iterdir()) == {"Pack", "Pack.zip"}


@pytest.mark.parametrize("operation", ["lookup", "remember", "_prune"])
def test_cache_io_errors_are_reported_without_rolling_back_success(fixture, monkeypatch, operation):
    exporter, project, parent = fixture
    exporter.export(project, parent, create_zip=False)
    project.segments[0].caption = "New caption"

    def fail(*_args):
        raise PermissionError("receipt inaccessible")

    monkeypatch.setattr(exporter.prompt_cache, operation, fail)
    result = exporter.export(project, parent, create_zip=False)
    assert result.validation["status"] == "passed"
    assert any("receipt inaccessible" in warning for warning in result.warnings)
    assert read_config(result.pack_path / "001_Alice.txt")["data"]["caption"] == "New caption"


@pytest.mark.parametrize("phase", ["old-hash", "copy", "staged-hash"])
def test_cancellation_during_reuse_preserves_outputs_and_receipt(fixture, monkeypatch, phase):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent)
    before = prompt_receipt(exporter).read_bytes()
    stopped = threading.Event()
    original_hash = exporter_module.sha256
    original_copy = exporter_module._copy_stream

    def tracked_hash(path):
        is_old = path.parent == cold.pack_path
        if path.name == "001_Alice.mp3" and (
            (phase == "old-hash" and is_old) or (phase == "staged-hash" and not is_old)
        ):
            stopped.set()
        return original_hash(path)

    def tracked_copy(source, destination):
        original_copy(source, destination)
        if phase == "copy" and Path(source.name) == cold.pack_path / "001_Alice.mp3":
            stopped.set()
            check_cancelled()

    monkeypatch.setattr(exporter_module, "sha256", tracked_hash)
    monkeypatch.setattr(exporter_module, "_copy_stream", tracked_copy)
    with pytest.raises(OperationCancelled):
        exporter.export(project, parent, cancelled=stopped.is_set)
    assert prompt_receipt(exporter).read_bytes() == before
    assert {p.name: original_hash(p) for p in cold.pack_path.iterdir()} == cold.file_hashes
    assert set(p.name for p in parent.iterdir()) == {"Pack", "Pack.zip"}


@pytest.mark.parametrize("mutation", ["source", "copy"])
def test_changes_during_reuse_abort_before_publication(fixture, monkeypatch, mutation):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    before = prompt_receipt(exporter).read_bytes()
    original = exporter_module._copy_file

    def changed(source, destination):
        original(source, destination)
        if source == cold.pack_path / "001_Alice.mp3":
            if mutation == "source":
                Path(project.video_path).write_bytes(b"changed original source during reuse")
            else:
                destination.write_bytes(b"corrupted copied bytes")

    monkeypatch.setattr(exporter_module, "_copy_file", changed)
    expected = SourceChangedError if mutation == "source" else RuntimeError
    with pytest.raises(expected, match="changed"):
        exporter.export(project, parent, create_zip=False)
    assert prompt_receipt(exporter).read_bytes() == before
    assert {p.name: exporter_module.sha256(p) for p in cold.pack_path.iterdir()} == cold.file_hashes


@pytest.mark.parametrize("check", ["duration", "leading_quiet", "trailing_quiet", "has_activity"])
def test_reused_audio_still_requires_all_prompt_checks(fixture, monkeypatch, check):
    exporter, project, parent = fixture
    exporter.export(project, parent, create_zip=False)
    before = prompt_receipt(exporter).read_bytes()
    original = exporter.media.decoded_audio_stats

    def invalid(path):
        stats = original(path)
        setattr(stats, check, 10.0 if check == "duration" else 0)
        return stats

    monkeypatch.setattr(exporter.media, "decoded_audio_stats", invalid)
    with pytest.raises(RuntimeError):
        exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 2)
    assert prompt_receipt(exporter).read_bytes() == before


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_cache_capacity_does_not_limit_valid_exports(fixture, monkeypatch, limit):
    exporter, project, parent = fixture
    if limit == "entries":
        monkeypatch.setattr(export_cache, "_MAX_PROMPT_ASSETS", 1)
    else:
        monkeypatch.setattr(export_cache, "_MAX_PROMPT_RECEIPT_BYTES", 512)
    cold = exporter.export(project, parent)
    assert len(exporter.prompt_cache.lookup(cold.pack_path)) == 1
    warm = exporter.export(project, parent)
    assert generation_counts(exporter) == (3, 4)
    assert warm.file_hashes == cold.file_hashes


def test_cache_cannot_write_inside_pack(fixture):
    exporter, project, parent = fixture
    unsafe = PackExporter(exporter.media, cache_root=parent / "Pack" / "receipts")
    with pytest.raises(ValueError, match="cache must be outside"):
        unsafe.export(project, parent)
    assert not parent.exists()


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_receipt_loss_regenerates_prompts_but_not_verified_video(fixture, damage):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    receipt = prompt_receipt(exporter)
    if damage == "missing":
        receipt.unlink()
    else:
        receipt.write_bytes(b'{"schema":1,')
    warm = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (4, 4)
    assert exporter.media.calls["video"] == 1
    assert warm.file_hashes == cold.file_hashes


def test_old_asset_read_errors_report_bounded_warning_and_regenerate(fixture, monkeypatch):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    original = exporter_module.sha256

    def unreadable(path):
        if path.parent == cold.pack_path and path.suffix in {".mp3", ".png"}:
            raise PermissionError("previous prompt unreadable")
        return original(path)

    monkeypatch.setattr(exporter_module, "sha256", unreadable)
    # Published checks must remain readable; end the injection before publication.
    def progress(update):
        if update.step == "publish":
            monkeypatch.setattr(exporter_module, "sha256", original)

    result = exporter.export(project, parent, create_zip=False, progress=progress)
    assert generation_counts(exporter) == (4, 4)
    assert sum("previous prompt unreadable" in warning for warning in result.warnings) == 1
    assert result.file_hashes == cold.file_hashes


@pytest.mark.parametrize("escape", ["symlink", "resolved-parent"])
def test_previous_asset_path_escape_is_never_read(fixture, monkeypatch, escape):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    previous = cold.pack_path / "001_Alice.mp3"
    original_hash = exporter_module.sha256

    def guarded_hash(path):
        assert path != previous, "Unsafe previous asset must not be opened"
        return original_hash(path)

    if escape == "symlink":
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == previous or original(path))
    else:
        original = Path.resolve
        monkeypatch.setattr(
            Path, "resolve",
            lambda path, *args, **kwargs: (
                parent / "elsewhere" / path.name
                if path == previous else original(path, *args, **kwargs)
            ),
        )
    monkeypatch.setattr(exporter_module, "sha256", guarded_hash)

    def progress(update):
        if update.step == "publish":
            monkeypatch.undo()

    warm = exporter.export(project, parent, create_zip=False, progress=progress)
    assert generation_counts(exporter) == (3, 2)
    assert warm.file_hashes == cold.file_hashes


def test_disabling_cache_keeps_existing_generation_without_extra_source_hash(fixture, monkeypatch):
    exporter, project, parent = fixture
    exporter.video_cache = exporter.prompt_cache = None
    project.preserve_source_video = True
    project.video_height = 720
    original = exporter_module.sha256

    def output_hashes_only(path):
        assert path != Path(project.video_path)
        return original(path)

    monkeypatch.setattr(exporter_module, "sha256", output_hashes_only)
    cold = exporter.export(project, parent, create_zip=False)
    warm = exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (4, 4)
    assert warm.file_hashes == cold.file_hashes


def test_returning_from_custom_assets_regenerates_only_newly_eligible_assets(fixture, tmp_path):
    exporter, project, parent = fixture
    exporter.export(project, parent, create_zip=False)
    audio = tmp_path / "chosen.mp3"
    image = tmp_path / "chosen.png"
    audio.write_bytes(b"chosen recording")
    image.write_bytes(b"chosen still")
    project.segments[0].audio_mode = "file"
    project.segments[0].audio_path = str(audio)
    project.segments[1].image_path = str(image)
    exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 2)
    project.segments[0].audio_mode = "video"
    project.segments[0].audio_path = ""
    project.segments[1].image_path = ""
    exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (3, 3)


def test_reuse_staging_write_failure_is_not_hidden_as_a_generation_miss(fixture, monkeypatch):
    exporter, project, parent = fixture
    cold = exporter.export(project, parent, create_zip=False)
    before = prompt_receipt(exporter).read_bytes()
    original = exporter_module._copy_file

    def fail(source, destination):
        if source == cold.pack_path / "001_Alice.mp3":
            raise OSError("staging disk failure")
        original(source, destination)

    monkeypatch.setattr(exporter_module, "_copy_file", fail)
    with pytest.raises(OSError, match="staging disk failure"):
        exporter.export(project, parent, create_zip=False)
    assert generation_counts(exporter) == (2, 2)
    assert prompt_receipt(exporter).read_bytes() == before
    assert {p.name: exporter_module.sha256(p) for p in cold.pack_path.iterdir()} == cold.file_hashes


def test_late_cancellation_can_remember_only_a_successfully_published_reuse(fixture):
    exporter, project, parent = fixture
    exporter.export(project, parent)
    project.segments[0].characters = ["Renamed"]
    stopped = threading.Event()

    def progress(update):
        if update.message == "Revalidating published pack...":
            stopped.set()

    result = exporter.export(project, parent, progress=progress, cancelled=stopped.is_set)
    assert stopped.is_set()
    assert result.validation["status"] == "passed"
    assert generation_counts(exporter) == (2, 2)
    filenames = {item.filename for item in exporter.prompt_cache.lookup(result.pack_path).values()}
    assert filenames == {"001_Renamed.mp3", "001_Renamed.png", "002_Bob.mp3", "002_Bob.png"}
