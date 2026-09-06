from __future__ import annotations

from pathlib import Path

import pytest

import choicer_voicer_pack_creator.exporter as exporter_module
from choicer_voicer_pack_creator.exporter import PackExporter, sha256


class FailingValidator:
    def validate_folder(
        self, _folder: Path, expected_clips: int | None = None, *, progress=None,
    ) -> dict:
        raise RuntimeError("injected post-publish validation failure")


def test_publish_restores_previous_pack_when_final_validation_fails(tmp_path: Path) -> None:
    target = tmp_path / "Pack"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "Stage"
    stage.mkdir()
    new_file = stage / "new.txt"
    new_file.write_text("new", encoding="utf-8")

    exporter = PackExporter.__new__(PackExporter)
    exporter.validator = FailingValidator()  # type: ignore[assignment]
    messages = []
    with pytest.raises(RuntimeError, match="injected post-publish"):
        exporter._publish_verified(
            stage,
            target,
            None,
            None,
            "Pack",
            {"new.txt": sha256(new_file)},
            1,
            progress=messages.append,
        )

    assert "restoring previous output" in messages[-1].message
    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (target / "new.txt").exists()
    assert not list(tmp_path.glob(".Pack.previous-*"))


def test_publish_does_not_delete_pack_when_initial_backup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "Pack"
    target.mkdir()
    old_file = target / "old.txt"
    old_file.write_bytes(b"untouched old pack")
    stage = tmp_path / "Stage"
    stage.mkdir()
    new_file = stage / "new.txt"
    new_file.write_bytes(b"new pack")
    real_replace = exporter_module.os.replace

    def fail_pack_backup(source: str | Path, destination: str | Path) -> None:
        if Path(source).resolve() == target.resolve():
            raise OSError("injected initial pack backup failure")
        real_replace(source, destination)

    exporter = PackExporter.__new__(PackExporter)
    exporter.validator = FailingValidator()  # type: ignore[assignment]
    monkeypatch.setattr(exporter_module.os, "replace", fail_pack_backup)
    with pytest.raises(OSError, match="initial pack backup"):
        exporter._publish_verified(
            stage,
            target,
            None,
            None,
            "Pack",
            {"new.txt": sha256(new_file)},
            1,
        )

    assert old_file.read_bytes() == b"untouched old pack"
    assert not (target / "new.txt").exists()


def test_publish_restores_pack_and_keeps_zip_when_zip_backup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "Pack"
    target.mkdir()
    old_file = target / "old.txt"
    old_file.write_bytes(b"old pack")
    target_zip = tmp_path / "Pack.zip"
    target_zip.write_bytes(b"untouched old zip")
    stage = tmp_path / "Stage"
    stage.mkdir()
    new_file = stage / "new.txt"
    new_file.write_bytes(b"new pack")
    staged_zip = tmp_path / "Stage.zip"
    staged_zip.write_bytes(b"new zip")
    real_replace = exporter_module.os.replace

    def fail_zip_backup(source: str | Path, destination: str | Path) -> None:
        if Path(source).resolve() == target_zip.resolve():
            raise OSError("injected ZIP backup failure")
        real_replace(source, destination)

    exporter = PackExporter.__new__(PackExporter)
    exporter.validator = FailingValidator()  # type: ignore[assignment]
    monkeypatch.setattr(exporter_module.os, "replace", fail_zip_backup)
    with pytest.raises(OSError, match="ZIP backup"):
        exporter._publish_verified(
            stage,
            target,
            staged_zip,
            target_zip,
            "Pack",
            {"new.txt": sha256(new_file)},
            1,
        )

    assert old_file.read_bytes() == b"old pack"
    assert target_zip.read_bytes() == b"untouched old zip"
    assert not (target / "new.txt").exists()