from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    critical_stage,
    operation_scope,
)
from choicer_voicer_pack_creator.validation import PackValidationError, PackValidator


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
