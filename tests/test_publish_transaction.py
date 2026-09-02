from __future__ import annotations

from pathlib import Path

import pytest

from choicer_voicer_pack_creator.exporter import PackExporter, sha256


class FailingValidator:
    def validate_folder(self, _folder: Path, expected_clips: int | None = None) -> dict:
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
    with pytest.raises(RuntimeError, match="injected post-publish"):
        exporter._publish_verified(
            stage,
            target,
            None,
            None,
            "Pack",
            {"new.txt": sha256(new_file)},
            1,
        )

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (target / "new.txt").exists()
    assert not list(tmp_path.glob(".Pack.previous-*"))