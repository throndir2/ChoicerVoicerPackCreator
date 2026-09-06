from __future__ import annotations

import io
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import exporter as exporter_module
from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.export_cache import ExportVideoCache
from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    check_cancelled,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.validation import PackValidator


class FakeMedia:
    def probe(self, _path: Path) -> SimpleNamespace:
        check_cancelled()
        return SimpleNamespace(
            duration=3.0, width=640, height=360, fps=30, has_audio=True,
            video_codec="theora", audio_codec="vorbis", pixel_format="yuv420p",
            audio_sample_rate=48000, audio_channels=2,
        )

    def make_icon(self, _source: Path, destination: Path, *, is_video: bool) -> None:
        check_cancelled()
        destination.write_bytes(b"icon")

    def create_silent_backing(self, destination: Path, _duration: float) -> None:
        check_cancelled()
        destination.write_bytes(b"backing")

    def audio_peak_dbfs(self, _path: Path) -> float:
        check_cancelled()
        return float("-inf")

    def probe_audio(self, _path: Path) -> SimpleNamespace:
        check_cancelled()
        return SimpleNamespace(codec="mp3", sample_rate=48000, channels=1)

    def probe_image_dimensions(self, _path: Path) -> tuple[int, int]:
        check_cancelled()
        return 640, 360


class FakeValidator:
    validate_zip = staticmethod(PackValidator.validate_zip)

    def validate_folder(self, folder: Path, expected_clips: int, *, progress=None) -> dict:
        if progress:
            progress("fully decoding Ogg video and audio")
        check_cancelled()
        return {"status": "passed", "clip_count": expected_clips}


def _fixture(tmp_path: Path) -> tuple[PackExporter, PackProject, Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("video.ogv", "prompt.mp3", "frame.png"):
        (inputs / name).write_bytes(b"original source content")
    project = PackProject(
        title="Pack", authors=["Author"], video_path=str(inputs / "video.ogv"),
        video_duration=3, video_height=360, video_fps=30, preserve_source_video=True,
        segments=[Segment(
            0.5, 1.5, "Original caption", ["Speaker"], audio_mode="file",
            audio_path=str(inputs / "prompt.mp3"), image_path=str(inputs / "frame.png"),
        )],
    )
    parent = tmp_path / "output"
    (parent / "Pack").mkdir(parents=True)
    (parent / "Pack" / "old.txt").write_bytes(b"previous pack")
    (parent / "Pack.zip").write_bytes(b"previous zip")
    exporter = PackExporter(FakeMedia())  # type: ignore[arg-type]
    exporter.validator = FakeValidator()  # type: ignore[assignment]
    return exporter, project, parent


def _assert_unchanged(parent: Path) -> None:
    assert (parent / "Pack" / "old.txt").read_bytes() == b"previous pack"
    assert (parent / "Pack.zip").read_bytes() == b"previous zip"
    assert set(path.name for path in parent.iterdir()) == {"Pack", "Pack.zip"}


@pytest.mark.parametrize("create_zip", [False, True])
@pytest.mark.parametrize("step", ["video-conversion", "staged-validation", "hashing", "publish"])
def test_cancelled_export_preserves_previous_outputs_and_cleans_staging(
    tmp_path: Path, step: str, create_zip: bool,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = False

    def progress(update) -> None:
        nonlocal stopped
        stopped |= update.step == step

    with pytest.raises(OperationCancelled):
        exporter.export(
            project, parent, create_zip=create_zip, progress=progress, cancelled=lambda: stopped,
        )
    _assert_unchanged(parent)
    assert Path(project.video_path).read_bytes() == b"original source content"


def test_zip_compression_is_cancellable_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = False
    original_write = zipfile._ZipWriteFile.write

    def write(output, data):
        nonlocal stopped
        result = original_write(output, data)
        stopped = True
        return result

    monkeypatch.setattr(zipfile._ZipWriteFile, "write", write)
    with pytest.raises(OperationCancelled):
        exporter.export(project, parent, cancelled=lambda: stopped)
    _assert_unchanged(parent)


def test_staged_zip_validation_is_cancellable_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = False
    original_read = zipfile.ZipExtFile.read

    def read(stream, size=-1):
        nonlocal stopped
        result = original_read(stream, size)
        stopped = True
        return result

    monkeypatch.setattr(zipfile.ZipExtFile, "read", read)
    with pytest.raises(OperationCancelled):
        exporter.export(project, parent, cancelled=lambda: stopped)
    _assert_unchanged(parent)


@pytest.mark.parametrize("phase", ["Publishing validated", "Revalidating published", "Cleaning up"])
def test_cancellation_during_publication_finishes_successfully(tmp_path: Path, phase: str) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = False

    def progress(update) -> None:
        nonlocal stopped
        stopped |= update.message.startswith(phase)

    result = exporter.export(project, parent, progress=progress, cancelled=lambda: stopped)
    assert stopped
    assert result.validation["status"] == "passed"
    assert not (result.pack_path / "old.txt").exists()
    assert result.zip_path and zipfile.is_zipfile(result.zip_path)
    assert set(path.name for path in parent.iterdir()) == {"Pack", "Pack.zip"}


def test_cancel_request_does_not_interrupt_failed_publish_rollback(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = False

    def progress(update) -> None:
        nonlocal stopped
        if update.message.startswith("Revalidating published pack..."):
            stopped = True
            raise RuntimeError("injected validation failure")

    with pytest.raises(RuntimeError, match="injected validation failure"):
        exporter.export(project, parent, progress=progress, cancelled=lambda: stopped)
    _assert_unchanged(parent)


def test_export_uses_detached_project_metadata(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)

    def progress(update) -> None:
        project.title = "Edited while exporting"
        project.authors.append("Other author")
        project.segments[0].caption = "New caption"

    result = exporter.export(project, parent, progress=progress)
    assert result.pack_path.name == "Pack"
    assert read_config(result.pack_path / "_pack_info.ini")["data"]["authors"] == ["Author"]
    assert read_config(result.pack_path / "001_Speaker.txt")["data"]["caption"] == "Original caption"
    assert project.segments[0].caption == "New caption"


@pytest.mark.parametrize("directory_change", [False, True])
def test_changed_source_prevents_publication(tmp_path: Path, directory_change: bool) -> None:
    exporter, project, parent = _fixture(tmp_path)
    if directory_change:
        project.source_pack_path = str(Path(project.video_path).parent)

    def progress(update) -> None:
        if update.step == "publish":
            path = Path(project.video_path)
            if directory_change:
                path = path.with_name("new-source-file.txt")
            path.write_bytes(b"external source modification")

    with pytest.raises(SourceChangedError):
        exporter.export(project, parent, progress=progress)
    _assert_unchanged(parent)


def test_export_zip_must_not_replace_a_source_asset(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    project.icon_path = str(parent / "Pack.zip")
    with pytest.raises(ValueError, match="source or project assets"):
        exporter.export(project, parent)
    _assert_unchanged(parent)


def test_source_rechecked_after_final_zip_hash_before_replacement(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)

    def progress(update) -> None:
        if update.message == "Publishing: retaining existing output as rollback backups...":
            Path(project.video_path).write_bytes(b"external source modification")

    with pytest.raises(SourceChangedError):
        exporter.export(project, parent, progress=progress)
    _assert_unchanged(parent)


def test_export_must_not_write_inside_source_pack(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    source_root = Path(project.video_path).parent
    project.source_pack_path = str(source_root)
    with pytest.raises(ValueError, match="write inside a source pack"):
        exporter.export(project, source_root / "nested-output")
    assert not (source_root / "nested-output").exists()
    _assert_unchanged(parent)


def test_stream_copy_checks_cancellation_between_chunks() -> None:
    stopped = False

    class Output(io.BytesIO):
        def write(self, value):
            nonlocal stopped
            stopped = True
            return super().write(value)

    source = io.BytesIO(b"x" * (3 * 1024 * 1024))
    output = Output()
    with operation_scope(cancelled=lambda: stopped), pytest.raises(OperationCancelled):
        exporter_module._copy_stream(source, output)
    assert len(output.getvalue()) == 1024 * 1024


def test_hash_checks_cancellation_between_chunks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    original_sha256 = exporter_module.hashlib.sha256
    stopped = False

    class Digest:
        def __init__(self) -> None:
            self.digest = original_sha256()

        def update(self, chunk) -> None:
            nonlocal stopped
            self.digest.update(chunk)
            stopped = True

    monkeypatch.setattr(exporter_module.hashlib, "sha256", Digest)
    with operation_scope(cancelled=lambda: stopped), pytest.raises(OperationCancelled):
        exporter_module.sha256(source)


@pytest.mark.parametrize("resource", ["video", "audio", "image", "folder", "zip"])
def test_export_waits_cancellably_for_source_and_destination_leases(
    tmp_path: Path, resource: str,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    paths = {
        "video": Path(project.video_path),
        "audio": Path(project.segments[0].audio_path),
        "image": Path(project.segments[0].image_path),
        "folder": parent / "Pack",
        "zip": parent / "Pack.zip",
    }
    source = resource in {"video", "audio", "image"}
    waiting = threading.Event()
    stopped = threading.Event()

    def progress(message: str, fraction: float | None) -> None:
        if message.startswith("Waiting for another task"):
            waiting.set()

    def run():
        with operation_scope(progress=progress):
            return exporter.export(project, parent, cancelled=stopped.is_set)

    with (
        path_leases(
            read_paths=[] if source else [paths[resource]],
            write_paths=[paths[resource]] if source else [],
        ),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(run)
        try:
            assert waiting.wait(3)
        finally:
            stopped.set()
        with pytest.raises(OperationCancelled):
            future.result(timeout=3)
    _assert_unchanged(parent)


class ConvertingMedia(FakeMedia):
    def __init__(self) -> None:
        self.conversions = 0

    def convert_video(self, source, destination, height, fps, *, encoding_progress=None):
        check_cancelled()
        self.conversions += 1
        destination.write_bytes(source.read_bytes() + f"{height}/{fps}".encode())


def test_archive_accepts_preserved_media_with_pre_zip_timestamps(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    for path in (
        project.video_path, project.segments[0].audio_path, project.segments[0].image_path,
    ):
        os.utime(path, (0, 0))
    result = exporter.export(project, parent)
    with zipfile.ZipFile(result.zip_path) as archive:
        for name in ("dub_video.ogv", "001_Speaker.mp3", "001_Speaker.png"):
            entry = archive.getinfo(f"Pack/{name}")
            assert entry.compress_type == zipfile.ZIP_STORED
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)


def test_repeat_export_reuses_only_matching_verified_video(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    media = ConvertingMedia()
    exporter.media = media
    exporter.video_cache = ExportVideoCache(tmp_path / "receipts")
    project.preserve_source_video = False
    first = exporter.export(project, parent)
    assert media.conversions == 1
    project.segments[0].caption = "Edited caption"
    updates = []
    repeated = exporter.export(project, parent, progress=updates.append)
    assert media.conversions == 1
    assert first.file_hashes["dub_video.ogv"] == repeated.file_hashes["dub_video.ogv"]
    assert read_config(repeated.pack_path / "001_Speaker.txt")["data"]["caption"] == "Edited caption"
    assert any("Reusing verified previous video" in update.message for update in updates)
    assert not any(step.kind == "video-encode" for update in updates for step in update.plan)

    # Hash identity detects edits even when the source's length and timestamp stay the same.
    source = Path(project.video_path)
    info = source.stat()
    source.write_bytes(b"x" * info.st_size)
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    exporter.export(project, parent)
    assert media.conversions == 2
    project.video_height = 480
    exporter.export(project, parent)
    assert media.conversions == 3
    project.video_fps = 24
    exporter.export(project, parent)
    assert media.conversions == 4
    (parent / "Pack" / "dub_video.ogv").write_bytes(b"tampered video")
    exporter.export(project, parent)
    assert media.conversions == 5


@pytest.mark.parametrize("operation", ["lookup", "remember"])
def test_unavailable_video_receipt_reports_warning_without_losing_export(
    tmp_path: Path, monkeypatch, operation: str,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    exporter.media = ConvertingMedia()
    exporter.video_cache = ExportVideoCache(tmp_path / "receipts")
    project.preserve_source_video = False

    def fail(*_args):
        raise PermissionError("receipt inaccessible")

    monkeypatch.setattr(exporter.video_cache, operation, fail)
    result = exporter.export(project, parent)
    assert result.validation["status"] == "passed"
    assert any("receipt inaccessible" in warning for warning in result.warnings)


def test_failed_export_does_not_replace_video_reuse_receipt(tmp_path: Path) -> None:
    exporter, project, parent = _fixture(tmp_path)
    exporter.media = ConvertingMedia()
    exporter.video_cache = ExportVideoCache(tmp_path / "receipts")
    project.preserve_source_video = False
    first = exporter.export(project, parent)
    receipt = next((tmp_path / "receipts").glob("*.json"))
    previous_receipt = receipt.read_bytes()
    project.video_fps = 24

    def fail(update):
        if update.step == "staged-validation":
            raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        exporter.export(project, parent, progress=fail)
    assert receipt.read_bytes() == previous_receipt
    assert exporter_module.sha256(first.pack_path / "dub_video.ogv") == first.file_hashes["dub_video.ogv"]


def test_parallel_prompts_are_bounded_with_serial_progress_and_identical_outputs(
    tmp_path: Path, monkeypatch,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    project.segments = [project.segments[0].clone() for _ in range(5)]
    exporter.prompt_workers = 2
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = peak = started = 0
    original_write = exporter._write_audio
    coordinator = threading.get_ident()
    updates = []

    def write(*args):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            current = started
            peak = max(peak, active)
        try:
            if current <= 2:
                barrier.wait()
            return original_write(*args)
        finally:
            with lock:
                active -= 1

    def progress(update):
        assert threading.get_ident() == coordinator
        updates.append(update)

    monkeypatch.setattr(exporter, "_write_audio", write)
    parallel = exporter.export(project, parent, progress=progress)
    assert peak == 2
    assert active == 0
    fractions = [update.fraction for update in updates if update.step == "prompts"]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0 and fractions[-1] == 1
    assert all(any(f"Prompt {index}/5" in update.message for update in updates) for index in range(1, 6))
    monkeypatch.setattr(exporter, "_write_audio", original_write)
    exporter.prompt_workers = 1
    serial = exporter.export(project, tmp_path / "serial")
    assert serial.file_hashes == parallel.file_hashes


@pytest.mark.parametrize("failure", ["cancel", "callback", "worker"])
def test_parallel_prompt_interruption_stops_all_workers_before_cleaning_staging(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    exporter, project, parent = _fixture(tmp_path)
    project.segments = [project.segments[0].clone() for _ in range(4)]
    exporter.prompt_workers = 2
    barrier = threading.Barrier(2, timeout=5)
    stopped = threading.Event()
    lock = threading.Lock()
    started = []
    finished = []

    def write(_project, _segment, index, _source, _stage, _duration, _width, _height, notify):
        with lock:
            started.append(index)
        try:
            barrier.wait()
            if failure == "worker" and index == 1:
                raise RuntimeError("prompt failed")
            notify("Worker started")
            while True:
                check_cancelled()
                threading.Event().wait(0.01)
        finally:
            with lock:
                finished.append(index)

    def progress(update):
        if update.message.startswith("Worker started"):
            if failure == "cancel":
                stopped.set()
            elif failure == "callback":
                raise ValueError("progress failed")

    monkeypatch.setattr(exporter, "_write_prompt", write)
    error = {"cancel": OperationCancelled, "callback": ValueError, "worker": RuntimeError}[failure]
    with pytest.raises(error):
        exporter.export(project, parent, progress=progress, cancelled=stopped.is_set)
    assert sorted(started) == sorted(finished) == [1, 2]
    _assert_unchanged(parent)


def test_worker_cleanup_failure_is_not_hidden_by_cancellation(tmp_path: Path, monkeypatch) -> None:
    exporter, project, parent = _fixture(tmp_path)
    stopped = threading.Event()

    def write(*args):
        notify = args[-1]
        notify("Worker started")
        try:
            while True:
                check_cancelled()
                stopped.wait(0.01)
        finally:
            raise OSError("worker cleanup failed")

    def progress(update):
        if update.message.startswith("Worker started"):
            stopped.set()

    monkeypatch.setattr(exporter, "_write_prompt", write)
    with pytest.raises(OSError, match="worker cleanup failed"):
        exporter.export(project, parent, progress=progress, cancelled=stopped.is_set)
    _assert_unchanged(parent)
