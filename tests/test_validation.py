from __future__ import annotations

import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import validation as validation_module
from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
from choicer_voicer_pack_creator.media import AudioInfo, DecodedAudioStats, MediaError, MediaInfo
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    critical_stage,
    operation_scope,
)
from choicer_voicer_pack_creator.validation import PackValidationError, PackValidator


@pytest.fixture
def folder_validation(tmp_path, monkeypatch):
    (tmp_path / "_pack_info.ini").write_bytes(render_pack_info("Fixture", "icon.png", ["Tester"], ""))
    for name in ("icon.png", "001.png", "002.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    for name in ("dub_video.ogv", "_backing_track.mp3", "001.mp3", "002.mp3"):
        (tmp_path / name).write_bytes(b"fixture")
    for index in (1, 2):
        (tmp_path / f"{index:03d}.txt").write_bytes(
            render_clip_metadata("Synthetic prompt", f"{index:03d}.png", index / 2, ["Tester"]),
        )
    state = SimpleNamespace(active=0, estimates=[], decoded=[], statistics=[], error=None, silent=False)
    owner = threading.get_ident()

    @contextmanager
    def admitted(estimate):
        assert state.active == 0
        state.active += 1
        state.estimates.append(estimate)
        try:
            yield
        finally:
            state.active -= 1

    monkeypatch.setattr(validation_module, "export_resources", SimpleNamespace(acquire=admitted))

    class Media:
        def probe(self, path):
            return MediaInfo(3, 640, 360, 30, True, "theora", "vorbis", "yuv420p", 48000, 2)

        def probe_audio(self, path):
            if path.name == "_backing_track.mp3":
                return AudioInfo(3, "mp3", 44100, 2)
            return AudioInfo(0.5, "mp3", 48000, 1)

        def probe_image_dimensions(self, path):
            return (660, 364) if path.name == "icon.png" else (640, 360)

        def decode(self, path):
            assert state.active == 1 and threading.get_ident() == owner
            assert path.name not in {"001.mp3", "002.mp3"}, "Prompt audio was decoded twice"
            state.decoded.append(path.name)

        def validated_audio_stats(self, path):
            assert state.active == 1 and threading.get_ident() == owner
            state.statistics.append(path.name)
            if state.error:
                raise state.error
            return DecodedAudioStats(0.5, 0, 0, not state.silent)

        def decoded_audio_stats(self, path):
            pytest.fail("Validation must include every stream, not only first-audio statistics")

    return PackValidator(Media()), tmp_path, state


def test_folder_validation_remains_sequential_and_repeats_every_complete_pass(folder_validation):
    validator, folder, state = folder_validation
    messages = []
    owner = threading.get_ident()

    def progress(message):
        assert threading.get_ident() == owner
        messages.append(message)

    first = validator.validate_folder(folder, 2, progress=progress)
    second = validator.validate_folder(folder, 2, progress=progress)
    assert first == second
    assert first["clip_count"] == 2 and first["file_count"] == 10
    assert state.statistics == ["001.mp3", "002.mp3"] * 2
    assert state.decoded == ["dub_video.ogv", "icon.png", "_backing_track.mp3", "001.png", "002.png"] * 2
    assert state.active == 0 and len(state.estimates) == 14
    assert messages.index("prompt 1/2: checking and decoding audio") < messages.index(
        "prompt 2/2: checking metadata, timestamps, and references",
    )
    audio_memory = validator._decode_estimate().memory_bytes
    assert audio_memory < 65 * 1024**2
    assert validator._decode_estimate(1920, 1080, frame_buffers=8).memory_bytes > (
        validator._decode_estimate(640, 360, frame_buffers=8).memory_bytes
    )


@pytest.mark.parametrize("outcome", ["silent", "decode-error"])
def test_folder_audio_failure_stops_in_order_and_releases_admission(folder_validation, outcome):
    validator, folder, state = folder_validation
    if outcome == "silent":
        state.silent = True
    else:
        state.error = MediaError("corrupt additional stream")
    with pytest.raises((PackValidationError, MediaError)):
        validator.validate_folder(folder)
    assert state.statistics == ["001.mp3"]
    assert state.active == 0
    assert "001.png" not in state.decoded


def test_folder_wait_progress_stays_on_owner_and_cancels_before_admission(
    folder_validation, monkeypatch,
):
    validator, folder, state = folder_validation
    stopped = False

    def acquire(estimate):
        from choicer_voicer_pack_creator.operations import report

        report("Waiting for available export memory")
        pytest.fail("Cancelled progress should not admit work")

    def progress(message):
        nonlocal stopped
        if message.startswith("Waiting"):
            stopped = True

    monkeypatch.setattr(validation_module, "export_resources", SimpleNamespace(acquire=acquire))
    with operation_scope(lambda: stopped), pytest.raises(OperationCancelled):
        validator.validate_folder(folder, progress=progress)
    assert state.decoded == []


def test_wrong_still_dimensions_are_rejected_before_resource_estimation(folder_validation):
    validator, folder, state = folder_validation
    original = validator.media.probe_image_dimensions
    validator.media.probe_image_dimensions = lambda path: (
        (100000, 100000) if path.name == "001.png" else original(path)
    )
    with pytest.raises(PackValidationError, match="dimensions must match"):
        validator.validate_folder(folder)
    assert "001.png" not in state.decoded
    assert state.active == 0


def _archive(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_folder_validation_observes_cancellation_from_progress(tmp_path: Path) -> None:
    stopped = False

    def progress(message: str) -> None:
        nonlocal stopped
        stopped = True

    validator = PackValidator(None)  # type: ignore[arg-type]
    with operation_scope(cancelled=lambda: stopped), pytest.raises(OperationCancelled):
        validator.validate_folder(tmp_path, progress=progress)


def test_zip_validation_checks_cancellation_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _archive(tmp_path / "pack.zip", {"Pack/large.bin": b"x" * (3 * 1024 * 1024)})
    original_read = zipfile.ZipExtFile.read
    stopped = False
    reads = []

    def read(stream, size=-1):
        nonlocal stopped
        result = original_read(stream, size)
        reads.append(len(result))
        stopped = True
        return result

    monkeypatch.setattr(zipfile.ZipExtFile, "read", read)
    with operation_scope(cancelled=lambda: stopped), pytest.raises(OperationCancelled):
        PackValidator.validate_zip(path, "Pack", {"large.bin"})
    assert reads == [1024 * 1024]


def test_zip_validation_defers_cancellation_inside_publication(tmp_path: Path) -> None:
    path = _archive(tmp_path / "pack.zip", {"Pack/file.bin": b"valid content"})
    stopped = False
    with operation_scope(cancelled=lambda: stopped), critical_stage("Publishing"):
        stopped = True
        PackValidator.validate_zip(path, "Pack", {"file.bin"})


def test_zip_crc_errors_remain_validation_failures(tmp_path: Path) -> None:
    path = _archive(tmp_path / "pack.zip", {"Pack/file.bin": b"unique-content"})
    path.write_bytes(path.read_bytes().replace(b"unique-content", b"broken-content"))
    with pytest.raises(PackValidationError, match="ZIP CRC failed for Pack/file.bin"):
        PackValidator.validate_zip(path, "Pack", {"file.bin"})


@pytest.mark.parametrize(
    ("files", "expected", "message"),
    [
        ({"Pack/file.bin": b"x"}, {"missing.bin"}, "ZIP inventory differs"),
        (
            {"Pack/file.bin": b"x", "outside.bin": b"y"},
            {"file.bin"}, "ZIP has files outside its pack folder",
        ),
    ],
)
def test_zip_inventory_errors_remain_validation_failures(
    tmp_path: Path, files: dict[str, bytes], expected: set[str], message: str,
) -> None:
    path = _archive(tmp_path / "pack.zip", files)
    with pytest.raises(PackValidationError, match=message):
        PackValidator.validate_zip(path, "Pack", expected)
