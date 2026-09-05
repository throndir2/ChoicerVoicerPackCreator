from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from choicer_voicer_pack_creator import updates


def package(root: Path, version: str, extra: dict[str, bytes] | None = None) -> Path:
    files = {
        updates.EXECUTABLE: f"app-{version}".encode(),
        "_internal/runtime.dll": f"runtime-{version}".encode(),
        "bin/ffmpeg.exe": b"ffmpeg",
        "bin/ffprobe.exe": b"ffprobe",
        **(extra or {}),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    updates.write_portable_manifest(root, version)
    return root


def release_json(version: str, *, prerelease: bool = False) -> dict:
    name = f"Choicer-Voicer-Pack-Creator-{version}-Windows-x64.zip"
    return {
        "tag_name": f"v{version}", "draft": False, "prerelease": prerelease,
        "assets": [
            {
                "name": filename, "size": 500,
                "browser_download_url": f"{updates.RELEASES_URL}/download/v{version}/{filename}",
            }
            for filename in (name, name + ".sha256")
        ],
    }


def fake_releases(monkeypatch, data: list) -> None:
    monkeypatch.setattr(updates, "_read", lambda *_args: json.dumps(data).encode())


def prepared_package(tmp_path: Path) -> updates.PreparedUpdate:
    target = package(tmp_path / "Installed app", "0.5.1", {"_internal/obsolete.dll": b"old"})
    directory = tmp_path / ".cvpc-update-test"
    staged = package(
        directory / "application", "0.6.0", {"_internal/new.dll": b"new", "new-file.txt": b"new"}
    )
    assert staged.exists()
    return updates.PreparedUpdate(directory, target, "0.6.0")


def archive_bytes(root: Path, extra: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, f"{updates.APP_NAME}/{path.relative_to(root).as_posix()}")
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


def setup_download(monkeypatch, payload: bytes, *, checksum: str | None = None) -> updates.Release:
    digest = hashlib.sha256(payload).hexdigest()
    release = updates.Release(
        "0.6.0", "v0.6.0", True, f"{updates.RELEASES_URL}/download/v0.6.0/package.zip",
        len(payload), f"{updates.RELEASES_URL}/download/v0.6.0/package.zip.sha256", f"sha256:{digest}",
    )
    monkeypatch.setattr(
        updates, "_read",
        lambda *_args: f"{checksum or digest}  {release.archive_name}\n".encode(),
    )
    monkeypatch.setattr(updates, "_open", lambda _url: io.BytesIO(payload))
    return release


@pytest.mark.parametrize(("older", "newer"), [
    ("0.5.9", "v0.5.10"), ("0.9.0", "0.10.0"), ("0.5.1", "0.6.0-rc.1"),
    ("0.6.0-rc.1", "0.6.0-rc.2"), ("0.6.0-rc.9", "0.6.0-rc.10"),
    ("0.6.0-rc.10", "0.6.0"), ("0.6.0-alpha", "0.6.0-alpha.1"),
])
def test_version_order(older, newer) -> None:
    assert updates.version_key(older) < updates.version_key(newer)


def test_release_discovery_includes_prereleases_and_uses_version_not_date(monkeypatch) -> None:
    fake_releases(monkeypatch, [
        release_json("0.5.2"), release_json("0.10.0", prerelease=True),
        release_json("0.6.0"), release_json("0.4.0"),
    ])
    assert updates.find_release("0.5.1").version == "0.10.0"
    assert updates.find_release("0.5.1", include_prereleases=False).version == "0.6.0"
    assert updates.find_release("0.10.0") is None


def test_drafts_incomplete_assets_and_non_version_tags_are_not_offered(monkeypatch) -> None:
    draft = {**release_json("0.7.0"), "draft": True}
    incomplete = release_json("0.8.0")
    incomplete["assets"].pop()
    nonversion = {**release_json("0.9.0"), "tag_name": "nightly"}
    fake_releases(monkeypatch, [draft, incomplete, nonversion, release_json("0.5.1")])
    assert updates.find_release("0.5.1") is None


def test_release_discovery_follows_pages(monkeypatch) -> None:
    urls = []

    def read(url, *_args):
        urls.append(url)
        return json.dumps(
            [release_json("0.5.1")] * 100 if url.endswith("page=1")
            else [release_json("0.6.0")]
        ).encode()

    monkeypatch.setattr(updates, "_read", read)
    assert updates.find_release("0.5.1").version == "0.6.0"
    assert len(urls) == 2


def test_release_assets_must_belong_to_public_repository(monkeypatch) -> None:
    item = release_json("0.6.0")
    item["assets"][0]["browser_download_url"] = "https://github.com/other/repo/releases/download/x/a"
    fake_releases(monkeypatch, [item])
    with pytest.raises(updates.UpdateError, match="configured repository"):
        updates.find_release("0.5.1")


@pytest.mark.parametrize("url", [
    "http://github.com/a", "https://evil.example/a", "file:///C:/update.zip",
    "https://github.com.evil.example/a", "https://github.com:444/a",
    "https://user:password@github.com/a",
])
def test_download_urls_are_limited_to_github_https(url) -> None:
    with pytest.raises(updates.UpdateError, match="approved"):
        updates._open(url)


def test_redirect_to_unapproved_host_is_rejected() -> None:
    with pytest.raises(updates.UpdateError, match="unapproved"):
        updates._GitHubRedirects().redirect_request(
            None, None, 302, "", {}, "https://evil.example/update.zip"
        )


def test_manifest_records_only_shipped_files_and_detects_modifications(tmp_path) -> None:
    root = package(tmp_path / "app", "0.5.1")
    (root / "my-project.cvpack.json").write_text("project")
    files = updates.verify_installation(root, "0.5.1")
    assert "my-project.cvpack.json" not in files
    (root / "bin" / "ffmpeg.exe").write_bytes(b"custom ffmpeg")
    with pytest.raises(updates.UpdateError, match="locally modified"):
        updates.verify_installation(root, "0.5.1")


@pytest.mark.parametrize("name", [
    "../outside.exe", "/absolute.exe", "C:/bad.exe", "directory\\file.exe",
    "bad/../file.exe", "file.exe:stream", "NUL.txt", "directory/file. ",
    "directory//file.exe", "./file.exe", "", ".",
])
def test_unsafe_manifest_paths_are_rejected(tmp_path, name) -> None:
    root = package(tmp_path / "app", "0.5.1")
    path = root / updates.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["files"][name] = "a" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(updates.UpdateError, match="Unsafe"):
        updates.read_portable_manifest(root)


def test_case_insensitive_duplicate_manifest_paths_rejected(tmp_path) -> None:
    root = package(tmp_path / "app", "0.5.1")
    path = root / updates.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["files"]["BIN/FFMPEG.EXE"] = "a" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(updates.UpdateError, match="duplicate"):
        updates.read_portable_manifest(root)


def test_verified_download_is_staged_without_changing_installation(tmp_path, monkeypatch) -> None:
    target = package(tmp_path / "app", "0.5.1")
    incoming = package(tmp_path / "new", "0.6.0")
    payload = archive_bytes(incoming)
    release = setup_download(monkeypatch, payload)
    progress = []
    prepared = updates.prepare_update(
        release, target, lambda *args: progress.append(args), lambda: False, "0.5.1"
    )
    assert updates.verify_installation(target, "0.5.1")
    assert updates.verify_installation(prepared.staged, "0.6.0")
    assert progress[-1][0] == "Verifying and staging update..."
    assert (prepared.directory / "plan.json").is_file()
    assert not (prepared.directory / "release.zip").exists()


def test_bad_checksum_never_changes_installed_app(tmp_path, monkeypatch) -> None:
    target = package(tmp_path / "app", "0.5.1")
    incoming = package(tmp_path / "new", "0.6.0")
    release = setup_download(monkeypatch, archive_bytes(incoming), checksum="0" * 64)
    with pytest.raises(updates.UpdateError, match="GitHub's archive digest"):
        updates.prepare_update(release, target, lambda *_: None, lambda: False, "0.5.1")
    assert updates.verify_installation(target, "0.5.1")
    assert not list(tmp_path.glob(".cvpc-update-*"))


def test_corrupt_download_is_rejected_even_if_the_zip_is_valid(tmp_path, monkeypatch) -> None:
    target = package(tmp_path / "app", "0.5.1")
    incoming = package(tmp_path / "new", "0.6.0")
    payload = archive_bytes(incoming)
    release = setup_download(monkeypatch, payload)
    monkeypatch.setattr(updates, "_open", lambda _url: io.BytesIO(payload[:-1] + b"x"))
    with pytest.raises(updates.UpdateError, match="SHA-256"):
        updates.prepare_update(release, target, lambda *_: None, lambda: False, "0.5.1")
    assert updates.verify_installation(target, "0.5.1")
    assert not list(tmp_path.glob(".cvpc-update-*"))


@pytest.mark.parametrize("extra", [
    {"../escaped.exe": b"bad"},
    {f"{updates.APP_NAME}/../escaped.exe": b"bad"},
    {f"{updates.APP_NAME}/BIN/FFMPEG.EXE": b"bad"},
    {f"{updates.APP_NAME}/not-in-manifest.exe": b"bad"},
])
def test_unsafe_or_uninventoried_archive_is_rejected(tmp_path, monkeypatch, extra) -> None:
    target = package(tmp_path / "app", "0.5.1")
    incoming = package(tmp_path / "new", "0.6.0")
    release = setup_download(monkeypatch, archive_bytes(incoming, extra))
    with pytest.raises(updates.UpdateError):
        updates.prepare_update(release, target, lambda *_: None, lambda: False, "0.5.1")
    assert updates.verify_installation(target, "0.5.1")
    assert not list(tmp_path.glob(".cvpc-update-*"))
    assert not (tmp_path / "escaped.exe").exists()


def test_zip_symlink_rejected(tmp_path) -> None:
    archive = tmp_path / "release.zip"
    entry = zipfile.ZipInfo(f"{updates.APP_NAME}/linked.exe")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(entry, "target")
    with pytest.raises(updates.UpdateError, match="link"):
        updates._extract(archive, tmp_path / "extract", lambda: False)


def test_semver_prerelease_is_excluded_from_stable_channel_even_if_mislabeled(monkeypatch) -> None:
    fake_releases(monkeypatch, [release_json("0.6.0-rc.1", prerelease=False)])
    assert updates.find_release("0.5.1", include_prereleases=False) is None
    assert updates.find_release("0.5.1").prerelease


def test_cancel_during_download_removes_staging_and_leaves_app_untouched(tmp_path, monkeypatch) -> None:
    target = package(tmp_path / "app", "0.5.1")
    incoming = package(tmp_path / "new", "0.6.0")
    release = setup_download(monkeypatch, archive_bytes(incoming))
    canceled = False

    def progress(message, _fraction):
        nonlocal canceled
        if message == "Downloading update...":
            canceled = True

    with pytest.raises(updates.UpdateCancelled):
        updates.prepare_update(release, target, progress, lambda: canceled, "0.5.1")
    assert updates.verify_installation(target, "0.5.1")
    assert not list(tmp_path.glob(".cvpc-update-*"))


def test_apply_only_replaces_managed_files_and_removes_stale_managed_files(tmp_path) -> None:
    prepared = prepared_package(tmp_path)
    target = prepared.target
    extras = {
        "projects/important.cvpack.json": b"edits",
        "my-video.mp4": b"media",
        "_internal/user-plugin.txt": b"custom",
        "bin/custom-tool.exe": b"custom",
    }
    for name, content in extras.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    updates.apply_update(prepared, "0.5.1", updates.sha256(target / updates.MANIFEST))
    assert updates.verify_installation(target, "0.6.0")
    assert not (target / "_internal" / "obsolete.dll").exists()
    for name, content in extras.items():
        assert (target / name).read_bytes() == content
    assert (prepared.directory / "backup" / updates.EXECUTABLE).read_bytes() == b"app-0.5.1"


def test_apply_refuses_collisions_with_unrelated_files(tmp_path) -> None:
    prepared = prepared_package(tmp_path)
    unrelated = prepared.target / "new-file.txt"
    unrelated.write_text("mine")
    with pytest.raises(updates.UpdateError, match="unrelated file"):
        updates.apply_update(
            prepared, "0.5.1", updates.sha256(prepared.target / updates.MANIFEST)
        )
    assert unrelated.read_text() == "mine"
    assert updates.verify_installation(prepared.target, "0.5.1")


def test_apply_refuses_modified_app_files_or_changed_manifest(tmp_path) -> None:
    prepared = prepared_package(tmp_path)
    manifest_hash = updates.sha256(prepared.target / updates.MANIFEST)
    (prepared.target / updates.MANIFEST).write_text("{}")
    with pytest.raises(updates.UpdateError, match="changed after"):
        updates.apply_update(prepared, "0.5.1", manifest_hash)


def test_apply_rolls_back_all_files_after_copy_failure(tmp_path, monkeypatch) -> None:
    prepared = prepared_package(tmp_path)
    original_copy = updates.shutil.copyfileobj
    calls = 0

    def fail_copy(source, destination, length):
        nonlocal calls
        calls += 1
        if calls == 3:
            destination.write(b"partial")
            raise OSError("injected disk failure")
        original_copy(source, destination, length)

    monkeypatch.setattr(updates.shutil, "copyfileobj", fail_copy)
    with pytest.raises(updates.UpdateError, match="previous application was restored"):
        updates.apply_update(
            prepared, "0.5.1", updates.sha256(prepared.target / updates.MANIFEST)
        )
    assert updates.verify_installation(prepared.target, "0.5.1")
    assert not (prepared.target / "new-file.txt").exists()


def test_locked_files_leave_original_application_intact(tmp_path, monkeypatch) -> None:
    prepared = prepared_package(tmp_path)

    def fail_replace(*_args):
        raise PermissionError("injected locked file")

    monkeypatch.setattr(updates.os, "replace", fail_replace)
    monkeypatch.setattr(updates.time, "sleep", lambda *_: None)
    with pytest.raises(updates.UpdateError, match="previous application was restored"):
        updates.apply_update(
            prepared, "0.5.1", updates.sha256(prepared.target / updates.MANIFEST)
        )
    assert updates.verify_installation(prepared.target, "0.5.1")


def test_rollback_failure_retains_exact_old_file_backup(tmp_path, monkeypatch) -> None:
    prepared = prepared_package(tmp_path)
    original_replace = updates.os.replace

    def fail_copy(_source, destination, _length):
        destination.write(b"partial")
        raise OSError("injected write failure")

    def fail_restore(source, destination):
        if Path(source).is_relative_to(prepared.directory / "backup"):
            raise PermissionError("injected rollback failure")
        original_replace(source, destination)

    monkeypatch.setattr(updates.shutil, "copyfileobj", fail_copy)
    monkeypatch.setattr(updates.os, "replace", fail_restore)
    with pytest.raises(updates.UpdateError, match="Rollback was incomplete"):
        updates.apply_update(
            prepared, "0.5.1", updates.sha256(prepared.target / updates.MANIFEST)
        )
    assert (prepared.directory / "backup" / updates.EXECUTABLE).read_bytes() == b"app-0.5.1"


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive filesystem")
def test_case_only_file_rename_is_not_deleted_twice(tmp_path) -> None:
    target = package(tmp_path / "app", "0.5.1", {"Case.txt": b"old"})
    directory = tmp_path / ".cvpc-update-test"
    package(directory / "application", "0.6.0", {"case.txt": b"new"})
    prepared = updates.PreparedUpdate(directory, target, "0.6.0")
    updates.apply_update(prepared, "0.5.1", updates.sha256(target / updates.MANIFEST))
    assert (target / "case.txt").read_bytes() == b"new"
    assert updates.verify_installation(target, "0.6.0")


def test_reparse_points_are_never_traversed(tmp_path, monkeypatch) -> None:
    root = package(tmp_path / "app", "0.5.1")
    original_lstat = Path.lstat

    class ReparseInfo:
        st_mode = stat.S_IFDIR
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    def fake_lstat(path):
        if path == root / "bin":
            return ReparseInfo()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(updates.UpdateError, match="junctions"):
        updates.verify_installation(root, "0.5.1")


def test_result_cannot_refer_to_another_directory(tmp_path) -> None:
    with pytest.raises(updates.UpdateError, match="Invalid update result"):
        updates.read_update_result(tmp_path / "user-files", tmp_path / "app")


@pytest.mark.skipif(os.name != "nt", reason="Windows native update failure notification")
def test_helper_does_not_restart_incompletely_restored_application(tmp_path, monkeypatch) -> None:
    prepared = prepared_package(tmp_path)
    (prepared.directory / "plan.json").write_text(json.dumps({
        "target": str(prepared.target), "version": "0.6.0", "previous_version": "0.5.1",
        "previous_manifest_hash": updates.sha256(prepared.target / updates.MANIFEST), "pid": 123,
    }))
    monkeypatch.setattr(updates.sys, "executable", str(prepared.staged / updates.EXECUTABLE))
    monkeypatch.setattr(updates, "_wait_for_editor", lambda *_: None)

    def fail_rollback(*_args):
        raise updates.UpdateRollbackError("Rollback was incomplete; backup retained")

    messages = []
    launches = []
    monkeypatch.setattr(updates, "apply_update", fail_rollback)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: launches.append(a))
    monkeypatch.setattr(
        updates.ctypes.windll.user32, "MessageBoxW", lambda *args: messages.append(args)
    )
    assert updates.helper_main(prepared.directory) == 1
    assert not launches
    assert "recovery is required" in messages[0][1]
    result = json.loads((prepared.directory / "result.json").read_text())
    assert result["success"] is False
