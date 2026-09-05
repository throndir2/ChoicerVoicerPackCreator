from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from choicer_voicer_pack_creator import __version__

REPOSITORY = "throndir2/ChoicerVoicerPackCreator"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases"
APP_NAME = "Choicer Voicer Pack Creator"
EXECUTABLE = f"{APP_NAME}.exe"
MANIFEST = "portable-files.json"
MAX_ARCHIVE_BYTES = 1024**3
MAX_EXPANDED_BYTES = 3 * 1024**3
BUFFER_SIZE = 1024 * 1024
Progress = Callable[[str, float], None]
Cancelled = Callable[[], bool]


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


class UpdateRollbackError(UpdateError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    prerelease: bool
    archive_url: str
    archive_size: int
    checksum_url: str
    digest: str | None

    @property
    def archive_name(self) -> str:
        return f"Choicer-Voicer-Pack-Creator-{self.version}-Windows-x64.zip"

    @property
    def page_url(self) -> str:
        return f"{RELEASES_URL}/tag/{urllib.parse.quote(self.tag, safe='')}"


@dataclass(frozen=True)
class PreparedUpdate:
    directory: Path
    target: Path
    version: str

    @property
    def staged(self) -> Path:
        return self.directory / "application"


def version_key(value: str) -> tuple[int, int, int, bool, tuple[tuple[bool, int | str], ...]]:
    match = re.fullmatch(
        r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?",
        value,
    )
    if not match:
        raise UpdateError(f"Unsupported release version: {value}")
    prerelease = match[4]
    identifiers = tuple(
        (not part.isdigit(), int(part) if part.isdigit() else part)
        for part in prerelease.split(".")
    ) if prerelease else ()
    return int(match[1]), int(match[2]), int(match[3]), prerelease is None, identifiers


def _check_cancel(cancelled: Cancelled) -> None:
    if cancelled():
        raise UpdateCancelled("Update canceled. The installed application was not changed.")


def _approved_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _approved_url(newurl):
            raise UpdateError("GitHub redirected the update request to an unapproved location.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    if not _approved_url(url):
        raise UpdateError("The update URL is not an approved GitHub HTTPS URL.")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"ChoicerVoicerPackCreator/{__version__}",
            "Accept": "application/vnd.github+json" if url.startswith(API_URL) else "*/*",
        },
    )
    return urllib.request.build_opener(_GitHubRedirects()).open(request, timeout=10)


def _read(url: str, limit: int, cancelled: Cancelled) -> bytes:
    _check_cancel(cancelled)
    with _open(url) as response:
        content = bytearray()
        deadline = time.monotonic() + 30
        while chunk := response.read(min(65536, limit + 1 - len(content))):
            _check_cancel(cancelled)
            content.extend(chunk)
            if len(content) > limit or time.monotonic() > deadline:
                raise UpdateError("The GitHub update response exceeded its size or time limit.")
    _check_cancel(cancelled)
    return bytes(content)


def find_release(
    current_version: str = __version__,
    *,
    include_prereleases: bool = True,
    cancelled: Cancelled = lambda: False,
) -> Release | None:
    current = version_key(current_version)
    candidates: list[Release] = []
    # GitHub sorts releases by creation date, not version; inspect complete pages.
    for page in range(1, 11):
        data = json.loads(_read(f"{API_URL}?per_page=100&page={page}", 4 * BUFFER_SIZE, cancelled))
        if not isinstance(data, list):
            raise UpdateError("GitHub returned an invalid release list.")
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = item.get("tag_name")
            if not isinstance(tag, str):
                continue
            try:
                newer = version_key(tag) > current
            except UpdateError:
                continue  # Non-application tags are not update candidates.
            prerelease = bool(item.get("prerelease")) or not version_key(tag)[3]
            if not newer or (prerelease and not include_prereleases):
                continue
            version = tag.removeprefix("v")
            name = f"Choicer-Voicer-Pack-Creator-{version}-Windows-x64.zip"
            assets = item.get("assets", [])
            if not isinstance(assets, list):
                raise UpdateError("GitHub returned invalid release assets.")
            matching = {
                asset.get("name"): asset
                for asset in assets if isinstance(asset, dict)
                and asset.get("name") in (name, name + ".sha256")
            }
            if len(matching) != 2:
                continue  # A release may still be uploading its portable assets.
            archive = matching[name]
            checksum = matching[name + ".sha256"]
            size = archive.get("size")
            expected_url = f"{RELEASES_URL}/download/{urllib.parse.quote(tag, safe='')}/"
            for asset in (archive, checksum):
                if asset.get("browser_download_url") != expected_url + asset["name"]:
                    raise UpdateError("Release assets do not belong to the configured repository.")
            if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE_BYTES:
                raise UpdateError("The release archive has an invalid size.")
            digest = archive.get("digest")
            if digest is not None and (
                not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ):
                raise UpdateError("GitHub returned an invalid archive digest.")
            candidates.append(Release(
                version, tag, prerelease, archive["browser_download_url"], size,
                checksum["browser_download_url"], digest,
            ))
        if len(data) < 100:
            break
    else:
        raise UpdateError("Too many GitHub release pages; use the releases page to update manually.")
    return max(candidates, key=lambda release: version_key(release.version), default=None)


def sha256(path: Path, cancelled: Cancelled = lambda: False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BUFFER_SIZE), b""):
            _check_cancel(cancelled)
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value or not path.parts or "\\" in value or path.is_absolute() or path.as_posix() != value
        or any(
            part in (".", "..") or part.endswith((" ", "."))
            or re.search(r'[<>:"|?*\x00-\x1f]', part)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
            for part in path.parts
        )
    ):
        raise UpdateError(f"Unsafe portable file path: {value}")
    return path


def _safe_path(root: Path, relative: str) -> Path:
    path = root
    for part in ("", *_relative_path(relative).parts):
        path = path / part if part else path
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise UpdateError(f"Updates cannot traverse links or junctions: {path}")
    return path


def write_portable_manifest(root: Path, version: str) -> None:
    version_key(version)
    files = {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*")) if path.is_file() and path != root / MANIFEST
    }
    (root / MANIFEST).write_text(
        json.dumps({"format": 1, "application": APP_NAME, "version": version, "files": files},
                   indent=2) + "\n",
        encoding="utf-8",
    )
    read_portable_manifest(root)


def read_portable_manifest(root: Path, expected_version: str | None = None) -> dict[str, str]:
    path = _safe_path(root, MANIFEST)
    if not path.is_file() or path.stat().st_size > 4 * BUFFER_SIZE:
        raise UpdateError("This package has no supported portable-file inventory. Update manually.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or (
        value.get("format") != 1 or value.get("application") != APP_NAME
        or (expected_version is not None and value.get("version") != expected_version)
    ):
        raise UpdateError("The portable-file inventory does not match this application/version.")
    files = value.get("files")
    if not isinstance(files, dict) or not files or len(files) > 30000:
        raise UpdateError("The portable-file inventory is invalid.")
    seen: set[str] = {MANIFEST.casefold()}
    for name, digest in files.items():
        _relative_path(name)
        if name.casefold() in seen or not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise UpdateError(f"Invalid or duplicate portable-file entry: {name}")
        seen.add(name.casefold())
    required = {EXECUTABLE, "bin/ffmpeg.exe", "bin/ffprobe.exe"}
    if not required <= files.keys() or not any(name.startswith("_internal/") for name in files):
        raise UpdateError("The portable package is missing required application files.")
    return files


def installation_directory() -> Path | None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).absolute().parent


def verify_installation(
    target: Path, version: str, cancelled: Cancelled = lambda: False,
) -> dict[str, str]:
    files = read_portable_manifest(target, version)
    for name, digest in files.items():
        _check_cancel(cancelled)
        path = _safe_path(target, name)
        if not path.is_file() or sha256(path, cancelled) != digest:
            raise UpdateError(
                f"An application file is missing or locally modified:\n{path}\n\n"
                "It will not be overwritten. Extract the new release into a separate folder."
            )
    return files


def _check_collisions(target: Path, old: dict[str, str], new: dict[str, str]) -> None:
    owned = {name.casefold() for name in old}
    for name in new:
        destination = _safe_path(target, name)
        if destination.exists() and name.casefold() not in owned:
            raise UpdateError(f"The update would overwrite an unrelated file:\n{destination}")
        for parent in destination.parents:
            if parent == target:
                break
            if parent.exists() and not parent.is_dir():
                raise UpdateError(f"The update conflicts with an existing file:\n{parent}")


def _extract(archive: Path, destination: Path, cancelled: Cancelled) -> None:
    with zipfile.ZipFile(archive) as package:
        files: list[tuple[zipfile.ZipInfo, str]] = []
        seen: set[str] = set()
        total = 0
        for entry in package.infolist():
            path = _relative_path(entry.filename.rstrip("/"))
            if path.parts[0] != APP_NAME:
                raise UpdateError("The release ZIP has an unexpected application folder.")
            if (
                stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            ):
                raise UpdateError("The release ZIP contains a link or unsupported/encrypted file.")
            if entry.is_dir():
                continue
            if len(path.parts) < 2:
                raise UpdateError("The release ZIP contains an invalid file.")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative.casefold() in seen:
                raise UpdateError("The release ZIP contains duplicate file paths.")
            seen.add(relative.casefold())
            total += entry.file_size
            files.append((entry, relative))
        if not files or len(files) > 30001 or total > MAX_EXPANDED_BYTES:
            raise UpdateError("The release ZIP exceeds the supported portable-package size.")
        if shutil.disk_usage(destination.parent).free < total * 2 + 100 * BUFFER_SIZE:
            raise UpdateError("Not enough free disk space to stage and back up the update.")
        for entry, relative in files:
            _check_cancel(cancelled)
            output_path = _safe_path(destination, relative)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with package.open(entry) as source, output_path.open("xb") as output:
                while chunk := source.read(BUFFER_SIZE):
                    _check_cancel(cancelled)
                    output.write(chunk)


def prepare_update(
    release: Release,
    target: Path,
    progress: Progress,
    cancelled: Cancelled,
    current_version: str = __version__,
) -> PreparedUpdate:
    if version_key(release.version) <= version_key(current_version):
        raise UpdateError("The selected release is not newer than the installed application.")
    progress("Checking application files...", 0.0)
    old = verify_installation(target, current_version, cancelled)
    manifest_hash = sha256(target / MANIFEST)
    _check_cancel(cancelled)
    if shutil.disk_usage(target.parent).free < release.archive_size * 3:
        raise UpdateError("Not enough free disk space to download the update.")
    directory = Path(tempfile.mkdtemp(prefix=".cvpc-update-", dir=target.parent))
    prepared = PreparedUpdate(directory, target, release.version)
    try:
        checksum_text = _read(release.checksum_url, 1024, cancelled).decode("ascii").strip()
        match = re.fullmatch(
            r"([0-9a-fA-F]{64}) [ *]" + re.escape(release.archive_name), checksum_text
        )
        if not match:
            raise UpdateError("The release checksum file is invalid.")
        expected_hash = match[1].lower()
        if release.digest is not None and release.digest != f"sha256:{expected_hash}":
            raise UpdateError("The release checksum does not match GitHub's archive digest.")
        archive = directory / "release.zip"
        digest = hashlib.sha256()
        downloaded = 0
        deadline = time.monotonic() + 1800
        with _open(release.archive_url) as response, archive.open("xb") as output:
            while chunk := response.read(BUFFER_SIZE):
                _check_cancel(cancelled)
                downloaded += len(chunk)
                if downloaded > release.archive_size or time.monotonic() > deadline:
                    raise UpdateError("The update download exceeded its size or time limit.")
                output.write(chunk)
                digest.update(chunk)
                progress("Downloading update...", downloaded / release.archive_size)
            output.flush()
            os.fsync(output.fileno())
        if downloaded != release.archive_size or digest.hexdigest() != expected_hash:
            raise UpdateError("The update archive failed size/SHA-256 verification.")
        progress("Verifying and staging update...", 1.0)
        _extract(archive, prepared.staged, cancelled)
        new = verify_installation(prepared.staged, release.version, cancelled)
        actual = {
            path.relative_to(prepared.staged).as_posix()
            for path in prepared.staged.rglob("*") if path.is_file()
        }
        if actual != new.keys() | {MANIFEST}:
            raise UpdateError("The update ZIP does not match its portable-file inventory.")
        _check_collisions(target, old, new)
        _check_cancel(cancelled)
        plan = {
            "target": str(target), "version": release.version, "previous_version": current_version,
            "previous_manifest_hash": manifest_hash,
        }
        (directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        archive.unlink()
        return prepared
    except (OSError, ValueError, UpdateError, zipfile.BadZipFile, http.client.HTTPException):
        shutil.rmtree(directory)
        raise


def apply_update(prepared: PreparedUpdate, previous_version: str, manifest_hash: str) -> None:
    target = prepared.target
    if sha256(_safe_path(target, MANIFEST)) != manifest_hash:
        raise UpdateError("The installed package changed after the update was prepared.")
    old = verify_installation(target, previous_version)
    new = verify_installation(prepared.staged, prepared.version)
    _check_collisions(target, old, new)
    # Only inventoried files are touched. Never mirror/delete an application directory.
    names_by_case = {name.casefold(): name for name in old}
    names_by_case.update({name.casefold(): name for name in new})
    names = sorted(names_by_case.values()) + [MANIFEST]
    original_hashes = {name.casefold(): digest for name, digest in old.items()}
    original_hashes[MANIFEST.casefold()] = manifest_hash
    backup = prepared.directory / "backup"
    backup.mkdir()
    backed_up: list[str] = []
    written: list[str] = []
    try:
        for name in names:
            destination = _safe_path(target, name)
            source = _safe_path(prepared.staged, name)
            if destination.is_file():
                if (
                    name.casefold() not in original_hashes
                    or sha256(destination) != original_hashes[name.casefold()]
                ):
                    raise UpdateError(f"A file changed while installing the update: {destination}")
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                # Locked binaries are retried briefly after the editor/bootloader exits.
                for attempt in range(20):
                    try:
                        os.replace(destination, saved)
                        break
                    except PermissionError:
                        if attempt == 19:
                            raise
                        time.sleep(0.25)
                backed_up.append(name)
            if name in new or name == MANIFEST:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as input_stream, destination.open("xb") as output:
                    written.append(name)
                    shutil.copyfileobj(input_stream, output, BUFFER_SIZE)
                    output.flush()
                    os.fsync(output.fileno())
        verify_installation(target, prepared.version)
    except (OSError, ValueError, UpdateError) as error:
        rollback_errors: list[str] = []
        for name in reversed(written):
            try:
                _safe_path(target, name).unlink(missing_ok=True)
            except (OSError, UpdateError) as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        for name in reversed(backed_up):
            try:
                os.replace(backup / name, _safe_path(target, name))
            except (OSError, UpdateError) as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        if rollback_errors:
            raise UpdateRollbackError(
                f"Update failed: {error}\nRollback was incomplete. Keep the backup at:\n{backup}\n"
                + "\n".join(rollback_errors)
            ) from error
        raise UpdateError(f"Update failed; the previous application was restored.\n{error}") from error


def _independent_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Both the staged helper and restarted app are independent PyInstaller instances.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def launch_update(prepared: PreparedUpdate, project: Path | None) -> None:
    plan_path = prepared.directory / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.update({"pid": os.getpid(), "project": str(project) if project else None})
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    process = subprocess.Popen(
        [str(prepared.staged / EXECUTABLE), "--apply-update", str(prepared.directory)],
        cwd=prepared.directory, env=_independent_environment(),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (prepared.directory / "ready").is_file():
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    (prepared.directory / "cancel").touch()
    raise UpdateError(
        "The update helper could not start. The editor will remain open. "
        f"Temporary update files are at:\n{prepared.directory}"
    )


def _wait_for_editor(pid: int, directory: Path) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        (directory / "ready").touch()
        for _ in range(240):
            if (directory / "cancel").exists():
                raise UpdateCancelled("The editor canceled the update.")
            result = kernel32.WaitForSingleObject(handle, 500)
            if result == 0:
                return
            if result != 258:  # WAIT_TIMEOUT
                raise ctypes.WinError(ctypes.get_last_error())
        raise UpdateError("The editor did not exit in time. No update was applied.")
    finally:
        kernel32.CloseHandle(handle)


def helper_main(directory: Path) -> int:
    """Run from the verified staged EXE, without importing Qt or acquiring the editor lock."""
    directory = directory.absolute()
    if sys.platform != "win32" or not re.fullmatch(r"\.cvpc-update-[a-z0-9_]+", directory.name):
        raise UpdateError("Invalid Windows update workspace.")
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    target = Path(plan["target"]).absolute()
    if target.parent != directory.parent or target == directory:
        raise UpdateError("The update target is not beside its staging directory.")
    prepared = PreparedUpdate(directory, target, plan["version"])
    if Path(sys.executable).absolute() != prepared.staged / EXECUTABLE:
        raise UpdateError("The updater must run from the staged application.")
    try:
        _wait_for_editor(plan["pid"], directory)
    except (OSError, UpdateError) as error:
        (directory / "helper-error.txt").write_text(str(error), encoding="utf-8")
        return 1
    success = False
    can_restart = True
    try:
        apply_update(prepared, plan["previous_version"], plan["previous_manifest_hash"])
        message = f"Updated to {prepared.version}."
        success = True
    except (OSError, ValueError, UpdateError) as error:
        message = str(error)
        can_restart = not isinstance(error, UpdateRollbackError)
    (directory / "result.json").write_text(
        json.dumps({"success": success, "message": message}), encoding="utf-8"
    )
    if not can_restart:
        ctypes.windll.user32.MessageBoxW(
            None, f"{message}\n\nThe editor was not restarted because recovery is required. "
            f"Keep the update files at:\n{directory}", "Application update failed", 0x10,
        )
        return 1
    arguments = [str(target / EXECUTABLE), f"--update-result={directory}"]
    if plan.get("project"):
        arguments.append(plan["project"])
    try:
        subprocess.Popen(arguments, cwd=target, env=_independent_environment(),
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    except OSError as error:
        ctypes.windll.user32.MessageBoxW(
            None, f"{message}\n\nCould not restart the editor: {error}\n"
            f"Update details and backups: {directory}", "Application update", 0x10,
        )
        return 1
    return 0 if success else 1


def read_update_result(directory: Path, target: Path) -> tuple[bool, str]:
    if (
        directory.parent != target.parent
        or not re.fullmatch(r"\.cvpc-update-[a-z0-9_]+", directory.name)
    ):
        raise UpdateError("Invalid update result directory.")
    plan_path = _safe_path(directory, "plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if (
        not isinstance(plan, dict) or not isinstance(plan.get("target"), str)
        or Path(plan["target"]).absolute() != target.absolute()
    ):
        raise UpdateError("The update result belongs to another application folder.")
    result = json.loads(_safe_path(directory, "result.json").read_text(encoding="utf-8"))
    if (
        not isinstance(result, dict) or not isinstance(result.get("success"), bool)
        or not isinstance(result.get("message"), str)
    ):
        raise UpdateError("Invalid update result.")
    return result["success"], result["message"]
