from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build.py"
SPEC = importlib.util.spec_from_file_location("build_script_under_test", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {SCRIPT_PATH}")
sys.path.insert(0, str(SCRIPT_PATH.parent))
try:
    BUILD_SCRIPT = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(BUILD_SCRIPT)
finally:
    sys.path.pop(0)


def _prepare_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    distribution = tmp_path / "dist" / "vtest"
    application = distribution / "portable-build" / "Choicer Voicer Pack Creator"
    application.mkdir(parents=True)
    executable = application / "Choicer Voicer Pack Creator.exe"
    executable.write_bytes(b"application")
    candidate = distribution / "portable-build" / ".candidate.zip"
    candidate.write_bytes(b"validated candidate")
    stable = distribution / "share.zip"
    pending = distribution / "pending-portable.json"
    latest = distribution / "latest-portable.json"
    pending.write_text(
        json.dumps(
            {
                "version": "test",
                "build_id": "build-id",
                "application_directory": application.relative_to(tmp_path).as_posix(),
                "executable": executable.relative_to(tmp_path).as_posix(),
                "candidate_archive": candidate.relative_to(tmp_path).as_posix(),
                "archive": stable.relative_to(tmp_path).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(BUILD_SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(BUILD_SCRIPT, "DIST", distribution)
    monkeypatch.setattr(BUILD_SCRIPT, "APP_VERSION", "test")
    monkeypatch.setattr(BUILD_SCRIPT, "PENDING_BUILD_MANIFEST", pending)
    monkeypatch.setattr(BUILD_SCRIPT, "LATEST_BUILD_MANIFEST", latest)
    return candidate, stable, latest


def test_promote_candidate_replaces_stable_only_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")

    assert BUILD_SCRIPT.promote_candidate("build-id") == 0

    assert stable.read_bytes() == b"validated candidate"
    assert not candidate.exists()
    manifest = json.loads(latest.read_text(encoding="utf-8"))
    assert manifest["build_id"] == "build-id"
    assert "candidate_archive" not in manifest
    assert not BUILD_SCRIPT.PENDING_BUILD_MANIFEST.exists()


def test_failed_manifest_promotion_restores_previous_stable_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    latest.write_text('{"build_id":"previous"}\n', encoding="utf-8")

    def fail_manifest(_path: Path, _value: dict[str, str]) -> None:
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(BUILD_SCRIPT, "_write_manifest_atomic", fail_manifest)
    with pytest.raises(OSError, match="manifest publication"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"
    assert json.loads(latest.read_text(encoding="utf-8"))["build_id"] == "previous"
    assert BUILD_SCRIPT.PENDING_BUILD_MANIFEST.is_file()


def test_failed_candidate_replace_never_removes_stable_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, _latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    real_replace = BUILD_SCRIPT.os.replace

    def fail_candidate_replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).resolve() == candidate.resolve() and Path(destination).resolve() == stable.resolve():
            raise OSError("injected candidate replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(BUILD_SCRIPT.os, "replace", fail_candidate_replace)
    with pytest.raises(OSError, match="candidate replacement"):
        BUILD_SCRIPT.promote_candidate("build-id")

    assert stable.read_bytes() == b"previous stable"
    assert candidate.read_bytes() == b"validated candidate"
    assert not list(stable.parent.glob("*.previous-*"))


def test_manifest_and_rollback_failure_retains_exact_previous_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, stable, latest = _prepare_candidate(tmp_path, monkeypatch)
    stable.write_bytes(b"previous stable")
    latest.write_text('{"build_id":"previous"}\n', encoding="utf-8")
    real_replace = BUILD_SCRIPT.os.replace

    def fail_manifest(_path: Path, _value: dict[str, str]) -> None:
        raise OSError("injected manifest publication failure")

    def fail_backup_restore(source: str | Path, destination: str | Path) -> None:
        if ".previous-build-id" in Path(source).name and Path(destination).resolve() == stable.resolve():
            raise OSError("injected stable rollback failure")
        real_replace(source, destination)

    monkeypatch.setattr(BUILD_SCRIPT, "_write_manifest_atomic", fail_manifest)
    monkeypatch.setattr(BUILD_SCRIPT.os, "replace", fail_backup_restore)
    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        BUILD_SCRIPT.promote_candidate("build-id")

    backups = list(stable.parent.glob("*.previous-build-id"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous stable"
    assert stable.read_bytes() == b"validated candidate"
    assert candidate.read_bytes() == b"validated candidate"
    assert json.loads(latest.read_text(encoding="utf-8"))["build_id"] == "previous"
