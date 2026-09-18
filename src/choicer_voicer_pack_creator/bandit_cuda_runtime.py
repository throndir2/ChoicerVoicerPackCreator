"""Consent-driven CUDA wheel cache; activation is restricted to a fresh worker.

The shipped CPU environment is never changed. Wheels, all notices, and original
dist-info/RECORD files remain in the cache. No installer, .pth processing, or
startup hooks are run; only the selected wheel packages enter the import path.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from choicer_voicer_pack_creator.analysis import AnalysisError, download_verified
from choicer_voicer_pack_creator.diagnostics import diagnostic_event
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.separation import (
    SeparationCancelled,
    SeparationError,
    check_cancel,
    verify_model_file,
)

Cancel = Callable[[], bool]
Progress = Callable[[str, float | None], None]
_BLOCK = 1024 * 1024
_MARGIN = 256 * 1024**2
_MAX_EXPANDED = 12 * 1024**3
_HANDLES: list[Any] = []
_ROOTS = ("torch", "torchgen", "functorch", "torchaudio", "torio")
_TORCH_DLLS = ("torch_cpu.dll", "torch_cuda.dll", "c10.dll", "libiomp5md.dll")


class CUDARuntimeError(SeparationError):
    """The optional runtime configuration or cache cannot be used safely."""


def cuda_runtime_manifest() -> dict[str, Any]:
    path = Path(__file__).with_name("resources") / "bandit-cuda-runtime.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value["schema_version"] != 1
            or value["runtime_version"] != "torch-2.8.0-cu128-v1"
            or value["torch_version"] != "2.8.0+cu128"
            or value["torchaudio_version"] != "2.8.0+cu128"
            or value["cuda_version"] != "12.8" or value["platform"] != "win_amd64"
            or value["package_roots"] != list(_ROOTS)
            or set(value["wheels"]) != {"cp311", "cp312"}
        ):
            raise ValueError("unsupported runtime configuration")
        for abi, wheels in value["wheels"].items():
            if [wheel["distribution"] for wheel in wheels] != ["torch", "torchaudio"]:
                raise ValueError("incomplete runtime")
            for wheel in wheels:
                expected = f"{wheel['distribution']}-2.8.0+cu128-{abi}-{abi}-win_amd64.whl"
                parsed = urlparse(wheel["url"])
                if (
                    wheel["filename"] != expected
                    or parsed.scheme != "https" or parsed.netloc != "download.pytorch.org"
                    or unquote(parsed.path) != f"/whl/cu128/{expected}"
                    or parsed.query or parsed.fragment
                    or not re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"])
                    or type(wheel["bytes"]) is not int or wheel["bytes"] <= 0
                ):
                    raise ValueError("invalid official wheel pin")
        return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CUDARuntimeError(f"Invalid optional CUDA runtime manifest: {error}") from error


def _abi() -> str:
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if (
        sys.platform != "win32" or sys.implementation.name != "cpython"
        or platform.machine().upper() not in {"AMD64", "X86_64"}
        or sys.maxsize <= 2**32 or abi not in {"cp311", "cp312"}
    ):
        raise CUDARuntimeError("Optional CUDA runtime requires Windows x64 CPython 3.11 or 3.12")
    return abi


def runtime_download_bytes() -> int:
    return sum(wheel["bytes"] for wheel in cuda_runtime_manifest()["wheels"][_abi()])


def runtime_supported() -> bool:
    try:
        _abi()
    except CUDARuntimeError:
        return False
    return True


def _identity(manifest: dict[str, Any], abi: str) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:16] + "-" + abi


def _target(data_root: Path, manifest: dict[str, Any], abi: str) -> Path:
    return _windows_path(
        data_root / "separation-runtimes" / manifest["runtime_version"]
        / _identity(manifest, abi)
    )


def _windows_path(path: Path) -> Path:
    """Keep wheel paths usable without the machine-wide LongPathsEnabled policy."""
    absolute = os.path.abspath(path)
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = path.parts
    if (
        not parts or path.is_absolute() or "\\" in name or "\x00" in name
        or any(part in {".", ".."} or ":" in part or part.endswith((" ", "."))
               or any(character in part for character in '<>"|?*')
               or any(ord(character) < 32 for character in part)
               or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
               for part in parts)
        or "/".join(parts) != name.rstrip("/")
    ):
        raise CUDARuntimeError(f"Unsafe CUDA wheel member: {name!r}")
    return parts


def _relocated(name: str, distribution: str) -> Path:
    parts = _safe_parts(name)
    if parts[0].endswith(".data"):
        if parts[0] != f"{distribution}-2.8.0+cu128.data" or len(parts) < 3:
            raise CUDARuntimeError(f"Invalid CUDA wheel relocation: {name}")
        if parts[1] in {"purelib", "platlib"}:
            return Path("site-packages", *parts[2:])
        if parts[1] in {"data", "headers", "scripts"}:
            # Retain non-importable content without ever installing/running scripts.
            return Path("wheel-data", distribution, *parts[1:])
        raise CUDARuntimeError(f"Unsupported CUDA wheel relocation: {name}")
    return Path("site-packages", *parts)


def _wheel_inventory(
    archive: zipfile.ZipFile, distribution: str, cancelled: Cancel,
) -> list[tuple[zipfile.ZipInfo, Path, str]]:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names: set[str] = set()
    targets: set[str] = set()
    record_name = f"{distribution}-2.8.0+cu128.dist-info/RECORD"
    if sum(info.file_size for info in infos) > _MAX_EXPANDED or len(infos) > 100_000:
        raise CUDARuntimeError("CUDA wheel exceeds its extraction budget")
    for info in archive.infolist():
        check_cancel(cancelled)
        _safe_parts(info.filename)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
            raise CUDARuntimeError("CUDA wheels must contain ordinary, unencrypted files")
        if info.filename.casefold() in names:
            raise CUDARuntimeError("Duplicate CUDA wheel member")
        names.add(info.filename.casefold())
    try:
        record_data = archive.read(record_name)
        records = {}
        for name, digest, size in csv.reader(io.StringIO(record_data.decode("utf-8"))):
            if name in records:
                raise ValueError("duplicate RECORD entry")
            records[name] = (digest, size)
    except (KeyError, ValueError, UnicodeError, csv.Error) as error:
        raise CUDARuntimeError(f"Invalid CUDA wheel RECORD: {error}") from error
    if set(records) != {info.filename for info in infos}:
        raise CUDARuntimeError("CUDA wheel RECORD does not describe every file")
    result = []
    for info in infos:
        check_cancel(cancelled)
        relative = _relocated(info.filename, distribution)
        key = relative.as_posix().casefold()
        if key in targets:
            raise CUDARuntimeError("CUDA wheel relocation collision")
        targets.add(key)
        digest, size = records[info.filename]
        if info.filename == record_name:
            if digest or size:
                raise CUDARuntimeError("Invalid CUDA wheel RECORD self entry")
            digest_hex = hashlib.sha256(record_data).hexdigest()
        else:
            try:
                algorithm, encoded = digest.split("=", 1)
                raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_",
                                       validate=True)
                if algorithm != "sha256" or len(raw) != 32 or int(size) != info.file_size:
                    raise ValueError("invalid RECORD digest/size")
                digest_hex = raw.hex()
            except (ValueError, TypeError) as error:
                raise CUDARuntimeError(f"Invalid CUDA wheel hash: {info.filename}") from error
        result.append((info, relative, digest_hex))
    if not any("license" in info.filename.casefold() for info in infos):
        raise CUDARuntimeError("CUDA wheel is missing license notices")
    return result


def _plain_tree(root: Path, cancelled: Cancel) -> set[Path]:
    files: set[Path] = set()
    pending = [root]
    while pending:
        check_cancel(cancelled)
        directory = pending.pop()
        attributes = directory.lstat()
        if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 1024:
            raise CUDARuntimeError("CUDA runtime cache contains a link or reparse point")
        for path in directory.iterdir():
            check_cancel(cancelled)
            attributes = path.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 1024:
                raise CUDARuntimeError("CUDA runtime cache contains a link or reparse point")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.add(path.relative_to(root))
            else:
                raise CUDARuntimeError("CUDA runtime cache contains a non-regular file")
    return files


def _validate(root: Path, manifest: dict[str, Any], abi: str, cancelled: Cancel) -> bool:
    root = _windows_path(root)
    def invalid(reason: str) -> bool:
        diagnostic_event("bandit_cuda_runtime_cache_invalid", path=str(root), reason=reason)
        return False

    check_cancel(cancelled)
    if not root.is_dir() or root.is_symlink():
        diagnostic_event("bandit_cuda_runtime_cache_missing", path=str(root))
        return False
    try:
        actual = _plain_tree(root, cancelled)
        expected = {Path("runtime.json")}
        if json.loads((root / "runtime.json").read_text(encoding="utf-8")) != {
            "manifest": manifest, "abi": abi,
        }:
            return invalid("Runtime manifest or Python ABI does not match")
        for wheel in manifest["wheels"][abi]:
            relative = Path("wheels", wheel["filename"])
            expected.add(relative)
            if not verify_model_file(root / relative, wheel["bytes"], wheel["sha256"], cancelled):
                return invalid(f"Pinned wheel failed size/SHA-256 verification: {wheel['filename']}")
            with zipfile.ZipFile(root / relative) as archive:
                for info, target, digest in _wheel_inventory(
                    archive, wheel["distribution"], cancelled,
                ):
                    if target in expected:
                        return invalid(f"Wheel relocation collision: {target}")
                    expected.add(target)
                    if not verify_model_file(root / target, info.file_size, digest, cancelled):
                        return invalid(f"Extracted file failed size/SHA-256 verification: {target}")
        if actual != expected:
            return invalid("The cache contains unexpected or missing files")
        return True
    except (OSError, ValueError, zipfile.BadZipFile, CUDARuntimeError) as error:
        return invalid(f"Could not verify the runtime cache: {error}")


@contextmanager
def _cache_lock(parent: Path, cancelled: Cancel) -> Iterator[None]:
    """Serialize publishers across app instances; the OS releases crashed owners."""
    import msvcrt

    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".install.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"\0")
            lock.flush()
        deadline = time.monotonic() + 3600
        while True:
            check_cancel(cancelled)
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in {13, 36}:
                    raise
                if time.monotonic() >= deadline:
                    raise CUDARuntimeError("Another CUDA runtime installation is still busy") from error
                time.sleep(0.1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def installed_runtime(data_root: Path, cancelled: Cancel = lambda: False) -> Path | None:
    check_cancel(cancelled)
    if not runtime_supported():
        return None
    manifest, abi = cuda_runtime_manifest(), _abi()
    target = _target(data_root, manifest, abi)
    try:
        with operation_scope(cancelled), path_leases(read_paths=(target,)):
            return target if _validate(target, manifest, abi, cancelled) else None
    except OperationCancelled as error:
        raise SeparationCancelled("CUDA runtime verification was canceled") from error


def _check_disk(path: Path, required: int) -> None:
    if shutil.disk_usage(path).free < required:
        raise CUDARuntimeError(
            f"Optional CUDA runtime needs {required / 1024**3:.1f} GiB free for download/extraction"
        )


def install_runtime(
    data_root: Path, job: Path, progress: Progress, cancelled: Cancel,
    *, allow_download: bool = True,
) -> Path:
    """Install only when explicitly invoked after separate runtime consent.

    allow_download=False is a fail-closed convenience for callers checking consent;
    cached verified runtimes can still be returned without consent or network.
    """
    manifest, abi = cuda_runtime_manifest(), _abi()
    target = _target(data_root, manifest, abi)
    try:
        with operation_scope(cancelled, progress), path_leases(
            write_paths=(target.parent,),
        ), _cache_lock(target.parent, cancelled):
            if _validate(target, manifest, abi, cancelled):
                return target
            if not allow_download:
                raise CUDARuntimeError("Downloading or repairing the CUDA runtime needs permission")
            # A unique sibling ensures atomic publication, including when job uses another disk.
            stage = target.with_name(f".{target.name}.{uuid.uuid4().hex}.staging")
            stage.mkdir()
            try:
                wheels = manifest["wheels"][abi]
                _check_disk(stage, sum(wheel["bytes"] for wheel in wheels) + _MAX_EXPANDED + _MARGIN)
                for wheel in wheels:
                    check_cancel(cancelled)
                    downloaded = download_verified(
                        wheel["url"], stage / "wheels" / wheel["filename"],
                        wheel["sha256"], wheel["bytes"], f"optional CUDA {wheel['distribution']}",
                        progress, cancelled,
                    )
                    progress(f"Extracting verified CUDA {wheel['distribution']}…", None)
                    with zipfile.ZipFile(downloaded) as archive:
                        inventory = _wheel_inventory(archive, wheel["distribution"], cancelled)
                        _check_disk(stage, sum(info.file_size for info, _, _ in inventory) + _MARGIN)
                        for info, relative, expected_hash in inventory:
                            check_cancel(cancelled)
                            destination = stage / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            digest = hashlib.sha256()
                            with archive.open(info) as source, destination.open("xb") as output:
                                while block := source.read(_BLOCK):
                                    check_cancel(cancelled)
                                    digest.update(block)
                                    output.write(block)
                            if digest.hexdigest() != expected_hash:
                                raise CUDARuntimeError(f"CUDA wheel file hash mismatch: {info.filename}")
                (stage / "runtime.json").write_text(
                    json.dumps({"manifest": manifest, "abi": abi}), encoding="utf-8",
                )
                if not _validate(stage, manifest, abi, cancelled):
                    raise CUDARuntimeError("Extracted CUDA runtime failed verification")
                check_cancel(cancelled)
                if target.exists():
                    obsolete = target.with_name(f".{target.name}.{uuid.uuid4().hex}.invalid")
                    os.replace(target, obsolete)
                    try:
                        os.replace(stage, target)
                    except OSError:
                        os.replace(obsolete, target)
                        raise
                    shutil.rmtree(obsolete)
                else:
                    os.replace(stage, target)
                return target
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    except OperationCancelled as error:
        raise SeparationCancelled("CUDA runtime installation was canceled") from error
    except (OSError, zipfile.BadZipFile, AnalysisError) as error:
        raise CUDARuntimeError(f"Could not prepare the optional CUDA runtime: {error}") from error


def _verify_loaded_libraries(site: Path) -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    loaded = {}
    for name in _TORCH_DLLS:
        handle = kernel.GetModuleHandleW(name)
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetModuleFileNameW(handle, buffer, len(buffer)) if handle else 0
        if (
            not size or size >= len(buffer)
            or _windows_path(Path(buffer.value)).resolve()
            != _windows_path(site / "torch" / "lib" / name).resolve()
        ):
            raise ImportError(f"CUDA worker did not load its original package-local {name}")
        loaded[name] = str(_windows_path(Path(buffer.value)).resolve())
    return loaded


class _CheckedTorchLoader:
    def __init__(self, loader: Any, site: Path) -> None:
        self.loader, self.site = loader, site

    def __getattr__(self, name: str) -> Any:
        return getattr(self.loader, name)

    def create_module(self, spec: Any) -> Any:
        return self.loader.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self.loader.exec_module(module)
        _verify_loaded_libraries(self.site)


class _RuntimeFinder(importlib.abc.MetaPathFinder):
    def __init__(self, site: Path) -> None:
        self.site = _windows_path(site).resolve()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        root = fullname.split(".", 1)[0]
        if root == "ctranslate2":
            raise ImportError("CTranslate2 cannot load in the CUDA separation worker")
        if root not in _ROOTS:
            return None
        search = [str(self.site)] if "." not in fullname else list(path or ())
        if not search or any(
            not _windows_path(Path(entry)).resolve().is_relative_to(self.site) for entry in search
        ):
            raise ImportError(f"CUDA package import escaped the selected runtime: {fullname}")
        # PathFinder bypasses PyInstaller's earlier archive finder, for submodules too.
        spec = importlib.machinery.PathFinder.find_spec(fullname, search, target)
        if spec is None:
            raise ModuleNotFoundError(f"Selected CUDA runtime has no module {fullname}", name=fullname)
        if spec.origin and not _windows_path(Path(spec.origin)).resolve().is_relative_to(self.site):
            raise ImportError(f"CUDA package resolved outside the selected runtime: {fullname}")
        if fullname == "torch":
            spec.loader = _CheckedTorchLoader(spec.loader, self.site)
        return spec

    def find_distributions(self, context: Any = None) -> Iterator[Any]:
        name = getattr(context, "name", None)
        for distribution in ("torch", "torchaudio"):
            if name is None or name.casefold().replace("-", "_") == distribution:
                yield importlib.metadata.Distribution.at(
                    self.site / f"{distribution}-2.8.0+cu128.dist-info",
                )


def _isolate_windows_dlls(site: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    for name in (*_TORCH_DLLS, "ctranslate2.dll"):
        if kernel.GetModuleHandleW(name):
            raise ImportError(f"Foreign backend DLL already loaded: {name}")
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    kernel.SetDefaultDllDirectories.argtypes = [wintypes.DWORD]
    kernel.SetDefaultDllDirectories.restype = wintypes.BOOL
    # Drop the frozen bootloader's _MEIPASS directory and the cwd/app-dir search.
    # Extension loads still use their own DLL_LOAD_DIR and explicit user directories.
    if not kernel.SetDllDirectoryW("") or not kernel.SetDefaultDllDirectories(0x800 | 0x400):
        raise OSError(
            ctypes.get_last_error(), "Could not isolate the CUDA worker DLL search",
        )
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    os.environ["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    for name in tuple(os.environ):
        if name.upper() in {"KMP_DUPLICATE_LIB_OK", "CUDA_PATH", "CUDA_HOME"}:
            os.environ.pop(name)
    for directory in (site / "torch" / "lib", site / "torchaudio" / "lib", site / "torio" / "lib"):
        if directory.is_dir():
            _HANDLES.append(os.add_dll_directory(str(directory)))


def activate_runtime(runtime_root: Path) -> None:
    """Activate in a disposable, Qt-free worker, before any heavy backend import."""
    if any(
        name.split(".", 1)[0] in {*_ROOTS, "ctranslate2"}
        for name in sys.modules
    ):
        raise CUDARuntimeError("CUDA activation requires a fresh worker without imported backends")
    if any(name.startswith(("PySide6", "PyQt")) for name in sys.modules):
        raise CUDARuntimeError("CUDA activation is forbidden in the editor process")
    runtime_root = _windows_path(runtime_root)
    manifest, abi = cuda_runtime_manifest(), _abi()
    if not _validate(runtime_root, manifest, abi, lambda: False):
        raise CUDARuntimeError("The optional CUDA runtime cache is missing, incomplete, or corrupt")
    site = runtime_root.resolve() / "site-packages"
    _isolate_windows_dlls(site)
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, _RuntimeFinder(site))


def native_import_smoke(runtime_root: Path) -> dict[str, Any]:
    """Optional fresh-process native probe using an existing cache, even without a GPU."""
    activate_runtime(runtime_root)
    import torch
    import torchaudio

    if (
        torch.__version__ != "2.8.0+cu128" or torchaudio.__version__ != "2.8.0+cu128"
        or torch.version.cuda != "12.8"
    ):
        raise ImportError("Native CUDA import smoke loaded an unexpected Torch/TorchAudio runtime")
    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    values = torch.arange(16, dtype=torch.float32, device="cpu").reshape(2, 8)
    result = values @ values.T
    if (
        result.device.type != "cpu" or result.dtype != torch.float32
        or not torch.isfinite(result).all()
        or result.tolist() != [[140.0, 364.0], [364.0, 1100.0]]
    ):
        raise RuntimeError("CUDA wheel native CPU tensor smoke produced an invalid result")
    return {
        "torch": torch.__version__, "torchaudio": torchaudio.__version__,
        "cuda": torch.version.cuda, "gpu_available": torch.cuda.is_available(),
        "compiled_architectures": torch._C._cuda_getArchFlags().split(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "package_paths": {
            "torch": str(_windows_path(Path(torch.__file__)).resolve()),
            "torchaudio": str(_windows_path(Path(torchaudio.__file__)).resolve()),
        },
        "dll_paths": _verify_loaded_libraries(runtime_root.resolve() / "site-packages"),
        "original_package_dlls": True,
        "tensor_device": result.device.type, "tensor_dtype": str(result.dtype),
        "tensor_result": result.tolist(), "threads": torch.get_num_threads(),
        "interop_threads": torch.get_num_interop_threads(),
        "ctranslate2_imported": any(
            name == "ctranslate2" or name.startswith("ctranslate2.") for name in sys.modules
        ),
        "qt_imported": any(name.startswith(("PySide6", "PyQt")) for name in sys.modules),
    }
