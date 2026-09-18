from __future__ import annotations

import base64
import copy
import csv
import ctypes
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import bandit_cuda_runtime as runtime
from choicer_voicer_pack_creator.separation import SeparationCancelled


def _wheel(path: Path, distribution: str, extra: dict[str, bytes] | None = None) -> bytes:
    info = f"{distribution}-2.8.0+cu128.dist-info"
    members = {
        f"{distribution}/__init__.py": b"__version__ = '2.8.0+cu128'\n",
        f"{distribution}/nested/__init__.py": b"selected = 'cuda'\n",
        f"{distribution}/lib/original.dll": b"unaltered package-local DLL",
        f"{info}/LICENSE": b"upstream license",
        f"{info}/NOTICE": b"third party attribution",
        f"{info}/METADATA": f"Name: {distribution}\nVersion: 2.8.0+cu128\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nTag: cp312-cp312-win_amd64\n",
        f"{distribution}-2.8.0+cu128.data/purelib/{distribution}/relocated.py":
            b"selected = 'relocated'\n",
        f"{distribution}-2.8.0+cu128.data/scripts/never-run.py":
            b"raise AssertionError('scripts must not run')\n",
        f"{distribution}-2.8.0+cu128.data/purelib/never-run.pth":
            b"import forbidden_startup_hook\n",
        **(extra or {}),
    }
    # The two distributions cannot relocate the same path in a combined runtime.
    members[f"{distribution}-2.8.0+cu128.data/purelib/{distribution}-never-run.pth"] = members.pop(
        f"{distribution}-2.8.0+cu128.data/purelib/never-run.pth",
    )
    record = io.StringIO()
    writer = csv.writer(record)
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([name, f"sha256={digest}", len(content)])
    writer.writerow([f"{info}/RECORD", "", ""])
    members[f"{info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path.read_bytes()


@pytest.fixture
def wheels(tmp_path, monkeypatch):
    manifest = copy.deepcopy(runtime.cuda_runtime_manifest())
    payloads = {}
    for entry in manifest["wheels"]["cp312"]:
        payload = _wheel(tmp_path / entry["filename"], entry["distribution"])
        entry.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        payloads[entry["url"]] = payload
    monkeypatch.setattr(runtime, "cuda_runtime_manifest", lambda: copy.deepcopy(manifest))
    monkeypatch.setattr(runtime, "_abi", lambda: "cp312")
    calls = []

    def download(url, destination, expected_hash, expected_bytes, label, progress, cancelled):
        runtime.check_cancel(cancelled)
        payload = payloads[url]
        assert len(payload) == expected_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_hash
        calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return destination

    monkeypatch.setattr(runtime, "download_verified", download)
    return manifest, payloads, calls


def _install(tmp_path, **kwargs):
    return runtime.install_runtime(
        tmp_path / "data", tmp_path / "job", lambda *_: None, lambda: False, **kwargs,
    )


def test_official_pins_and_exact_download_bytes():
    manifest = runtime.cuda_runtime_manifest()
    assert [(wheel["bytes"], wheel["sha256"]) for wheel in manifest["wheels"]["cp311"]] == [
        (3461420395, "34c55443aafd31046a7963b63d30bc3b628ee4a704f826796c865fdfd05bb596"),
        (4678509, "7a1eb6154e05b8056b34c7a41495e09d57f79eb0180eb4e7f3bb2a61845ca8ea"),
    ]
    assert [(wheel["bytes"], wheel["sha256"]) for wheel in manifest["wheels"]["cp312"]] == [
        (3461384651, "0ad925202387f4e7314302a1b4f8860fa824357f9b1466d7992bf276370ebcff"),
        (4673203, "cce3a60cd9a97f7360c8f95504ac349311fb7d6b9b826135936764f4de5f782d"),
    ]
    assert manifest["requirements"]["cuda_toolkit_required"] is False


@pytest.mark.parametrize("abi,total", [("cp311", 3466098904), ("cp312", 3466057854)])
def test_download_bytes_selects_current_abi(monkeypatch, abi, total):
    monkeypatch.setattr(runtime, "_abi", lambda: abi)
    assert runtime.runtime_download_bytes() == total


@pytest.mark.parametrize("final_url,approved", [
    ("https://download.pytorch.org/whl/cu128/pinned.whl", True),
    ("http://download.pytorch.org/whl/cu128/pinned.whl", False),
    ("https://download.pytorch.org.evil.example/pinned.whl", False),
    ("https://untrusted.download.pytorch.org/pinned.whl", False),
])
def test_verified_downloader_accepts_only_exact_https_pytorch_host(
    tmp_path, monkeypatch, final_url, approved,
):
    from choicer_voicer_pack_creator import analysis

    payload = b"pinned official runtime fixture"

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def geturl(self):
            return final_url

    monkeypatch.setattr(
        analysis.urllib.request, "urlopen", lambda *args, **kwargs: Response(payload),
    )
    destination = tmp_path / "pinned.whl"

    def download():
        return runtime.download_verified(
            "https://download.pytorch.org/whl/cu128/pinned.whl", destination,
            hashlib.sha256(payload).hexdigest(), len(payload), "CUDA fixture",
            lambda *_: None, lambda: False,
        )

    if approved:
        assert download() == destination
        assert destination.read_bytes() == payload
    else:
        with pytest.raises(analysis.AnalysisError, match="unapproved host"):
            download()
        assert not destination.exists()
    assert not destination.with_suffix(".whl.partial").exists()


@pytest.mark.parametrize("field,value", [
    ("platform", "linux"), ("maxsize", 2**31 - 1),
    ("implementation", SimpleNamespace(name="pypy")),
    ("version_info", SimpleNamespace(major=3, minor=13)),
])
def test_unsupported_abi_fails_before_download(monkeypatch, tmp_path, field, value):
    monkeypatch.setattr(runtime.sys, field, value)
    monkeypatch.setattr(runtime, "download_verified", lambda *_: pytest.fail("unexpected network"))
    assert not runtime.runtime_supported()
    assert runtime.installed_runtime(tmp_path) is None
    with pytest.raises(runtime.CUDARuntimeError, match="Windows x64 CPython"):
        _install(tmp_path)


def test_cache_install_keeps_all_notices_metadata_and_relocation(wheels, tmp_path):
    root = _install(tmp_path)
    assert len(wheels[2]) == 2
    site = root / "site-packages"
    for name in ("torch", "torchaudio"):
        assert (site / name / "relocated.py").is_file()
        assert (root / "wheel-data" / name / "scripts" / "never-run.py").is_file()
        assert (site / f"{name}-never-run.pth").is_file()
        for filename in ("LICENSE", "NOTICE", "RECORD", "WHEEL", "METADATA"):
            assert (site / f"{name}-2.8.0+cu128.dist-info" / filename).is_file()
        assert (site / name / "lib" / "original.dll").read_bytes() == b"unaltered package-local DLL"
    assert runtime.installed_runtime(tmp_path / "data") == root
    assert _install(tmp_path, allow_download=False) == root
    assert len(wheels[2]) == 2
    assert not list(root.parent.glob("*.staging"))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_cache_extraction_verification_and_repair_use_extended_length_paths(
    wheels, tmp_path, monkeypatch,
):
    manifest, payloads, _ = wheels
    entry = manifest["wheels"]["cp312"][0]
    member = "torch/include/ATen/ops/" + "long_cuda_header_" * 6 + ".h"
    payload = _wheel(tmp_path / entry["filename"], "torch", {member: b"original CUDA header"})
    entry.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    payloads[entry["url"]] = payload
    data = tmp_path / ("long-profile-path-" * 4)
    original_open, original_mkdir = Path.open, Path.mkdir
    paths = []

    def check(path):
        if len(str(path)) >= 260:
            assert str(path).startswith("\\\\?\\"), "Requires a machine-wide long-path policy"
            paths.append(path)

    def checked_open(path, *args, **kwargs):
        check(path)
        return original_open(path, *args, **kwargs)

    def checked_mkdir(path, *args, **kwargs):
        check(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(Path, "mkdir", checked_mkdir)
    root = runtime.install_runtime(data, tmp_path, lambda *_: None, lambda: False)
    assert str(root).startswith("\\\\?\\")
    header = root / "site-packages" / Path(member)
    assert len(str(header)) > 260 and header.read_bytes() == b"original CUDA header"
    assert runtime.installed_runtime(data) == root
    header.write_bytes(b"corrupt")
    assert runtime.installed_runtime(data) is None
    assert runtime.install_runtime(data, tmp_path, lambda *_: None, lambda: False) == root
    assert header.read_bytes() == b"original CUDA header"
    assert paths and not list(root.parent.glob("*.staging"))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
@pytest.mark.parametrize("path,expected", [
    (r"C:\cache\..\runtime", "\\\\?\\C:\\runtime"),
    (r"\\server\share\runtime", "\\\\?\\UNC\\server\\share\\runtime"),
    ("\\\\?\\C:\\runtime", "\\\\?\\C:\\runtime"),
])
def test_extended_paths_normalize_dos_unc_and_existing_prefixes(path, expected):
    assert str(runtime._windows_path(Path(path))) == expected


def test_no_runtime_is_downloaded_without_installer_consent(wheels, tmp_path):
    assert runtime.installed_runtime(tmp_path / "data") is None
    assert not wheels[2]
    with pytest.raises(runtime.CUDARuntimeError, match="permission"):
        _install(tmp_path, allow_download=False)
    assert not wheels[2]
    assert not list((tmp_path / "data").rglob("*.staging"))


@pytest.mark.parametrize("corruption", [
    "wheel", "module", "license", "metadata", "extra", "marker", "record",
])
def test_corrupt_cache_never_accepted_and_repair_requires_consent(
    wheels, tmp_path, monkeypatch, corruption,
):
    events = []
    monkeypatch.setattr(
        runtime, "diagnostic_event", lambda event, **details: events.append((event, details)),
    )
    root = _install(tmp_path)
    files = {
        "wheel": root / "wheels" / wheels[0]["wheels"]["cp312"][0]["filename"],
        "module": root / "site-packages" / "torch" / "__init__.py",
        "license": root / "site-packages" / "torch-2.8.0+cu128.dist-info" / "LICENSE",
        "metadata": root / "site-packages" / "torch-2.8.0+cu128.dist-info" / "METADATA",
        "record": root / "site-packages" / "torch-2.8.0+cu128.dist-info" / "RECORD",
        "extra": root / "site-packages" / "torch" / "injected.py",
        "marker": root / "runtime.json",
    }
    files[corruption].write_bytes(b"damaged")
    assert runtime.installed_runtime(tmp_path / "data") is None
    invalid = [details for event, details in events if event == "bandit_cuda_runtime_cache_invalid"]
    assert invalid and invalid[-1]["reason"]
    assert invalid[-1]["path"] == str(root)
    with pytest.raises(runtime.CUDARuntimeError, match="permission"):
        _install(tmp_path, allow_download=False)
    assert len(wheels[2]) == 2
    assert _install(tmp_path) == root
    assert runtime.installed_runtime(tmp_path / "data") == root
    assert len(wheels[2]) == 4


def test_corrupt_runtime_is_rejected_before_import_or_dll_activation(wheels, tmp_path):
    root = _install(tmp_path)
    (root / "site-packages" / "torch" / "__init__.py").write_text(
        "raise AssertionError('corrupt code must never execute')",
    )
    code = """
import json, sys
from pathlib import Path
from choicer_voicer_pack_creator import bandit_cuda_runtime as runtime
root = Path(sys.argv[1])
manifest = json.loads((root / 'runtime.json').read_text())['manifest']
runtime.cuda_runtime_manifest = lambda: manifest
runtime._abi = lambda: 'cp312'
def forbidden(*args):
    raise AssertionError('DLL activation must not start with corrupt artifacts')
runtime._isolate_windows_dlls = forbidden
try:
    runtime.activate_runtime(root)
except runtime.CUDARuntimeError as error:
    assert 'corrupt' in str(error)
else:
    raise AssertionError('corrupt runtime accepted')
assert 'torch' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)], capture_output=True, text=True, check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("name", [
    "../escaped.py", "/escaped.py", "C:/escaped.py", "torch\\evil.py", "torch/../evil.py",
    "torch//evil.py", "torch/./evil.py", "torch/file:stream", "torch/NUL", "torch/a.",
    "torch/a ", "torch/a?.py", "torch-2.8.0+cu128.data/unknown/evil",
    "other-2.8.0+cu128.data/purelib/torch/evil.py",
])
def test_unsafe_wheel_paths_rejected(tmp_path, name):
    path = tmp_path / "bad.whl"
    _wheel(path, "torch", {name: b"untrusted"})
    with zipfile.ZipFile(path) as archive, pytest.raises(runtime.CUDARuntimeError):
        runtime._wheel_inventory(archive, "torch", lambda: False)
    assert not (tmp_path / "escaped.py").exists()


def test_symlink_members_and_relocation_collisions_rejected(tmp_path):
    path = tmp_path / "bad.whl"
    _wheel(path, "torch")
    with zipfile.ZipFile(path, "a") as archive:
        info = zipfile.ZipInfo("torch/symlink")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../target")
    with zipfile.ZipFile(path) as archive, pytest.raises(runtime.CUDARuntimeError, match="ordinary"):
        runtime._wheel_inventory(archive, "torch", lambda: False)
    _wheel(path, "torch", {
        "torch-2.8.0+cu128.data/purelib/torch/__init__.py": b"collision",
    })
    with zipfile.ZipFile(path) as archive, pytest.raises(runtime.CUDARuntimeError, match="collision"):
        runtime._wheel_inventory(archive, "torch", lambda: False)


def test_missing_record_and_unrecorded_files_are_rejected(tmp_path):
    path = tmp_path / "bad.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("torch/__init__.py", b"unrecorded")
    with zipfile.ZipFile(path) as archive, pytest.raises(runtime.CUDARuntimeError, match="RECORD"):
        runtime._wheel_inventory(archive, "torch", lambda: False)
    _wheel(path, "torch")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("torch/extra.py", b"unrecorded")
    with zipfile.ZipFile(path) as archive, pytest.raises(runtime.CUDARuntimeError, match="every file"):
        runtime._wheel_inventory(archive, "torch", lambda: False)


def test_cancellation_cleans_owned_staging_and_preserves_previous_cache(wheels, tmp_path, monkeypatch):
    root = _install(tmp_path)
    marker = root / "runtime.json"
    marker.write_bytes(b"invalid old cache")
    download = runtime.download_verified
    canceled = False

    def cancel_after_download(*args):
        nonlocal canceled
        downloaded = download(*args)
        canceled = True
        return downloaded

    monkeypatch.setattr(runtime, "download_verified", cancel_after_download)
    with pytest.raises(SeparationCancelled):
        runtime.install_runtime(
            tmp_path / "data", tmp_path / "job", lambda *_: None, lambda: canceled,
        )
    assert marker.read_bytes() == b"invalid old cache"
    assert not list(root.parent.glob("*.staging"))


def test_verification_is_cancellable(wheels, tmp_path):
    _install(tmp_path)
    with pytest.raises(SeparationCancelled):
        runtime.installed_runtime(tmp_path / "data", lambda: True)


def test_low_disk_fails_before_downloading(wheels, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(runtime.CUDARuntimeError, match="GiB free"):
        _install(tmp_path)
    assert not wheels[2]
    assert not list((tmp_path / "data").rglob("*.staging"))


def test_publication_failure_restores_previous_cache(wheels, tmp_path, monkeypatch):
    root = _install(tmp_path)
    (root / "runtime.json").write_bytes(b"old invalid cache")
    replace = runtime.os.replace

    def fail_publication(source, destination):
        if str(source).endswith(".staging"):
            raise OSError("publication blocked")
        return replace(source, destination)

    monkeypatch.setattr(runtime.os, "replace", fail_publication)
    with pytest.raises(runtime.CUDARuntimeError, match="publication blocked"):
        _install(tmp_path)
    assert (root / "runtime.json").read_bytes() == b"old invalid cache"
    assert not list(root.parent.glob("*.staging"))


def test_concurrent_cache_installers_share_one_verified_publication(wheels, tmp_path):
    errors, roots = [], []

    def run():
        try:
            roots.append(_install(tmp_path))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert len(roots) == 2 and roots[0] == roots[1]
    assert len(wheels[2]) == 2


def test_cache_file_lock_wait_is_cancellable_and_releases_after_failure(tmp_path):
    checks, errors = [], []

    def canceled():
        checks.append(True)
        return len(checks) > 1

    def contender():
        try:
            with runtime._cache_lock(tmp_path, canceled):
                pytest.fail("another publisher still owns the lock")
        except SeparationCancelled as error:
            errors.append(error)

    with runtime._cache_lock(tmp_path, lambda: False):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1
    with runtime._cache_lock(tmp_path, lambda: False):
        pass


def test_finder_uses_complete_cuda_packages_before_frozen_finder(wheels, tmp_path):
    root = _install(tmp_path)
    code = """
import importlib.abc, importlib.metadata, json, sys
from pathlib import Path
from choicer_voicer_pack_creator import bandit_cuda_runtime as runtime
root = Path(sys.argv[1])
runtime._validate = lambda *args: True
runtime._isolate_windows_dlls = lambda *args: None
runtime._verify_loaded_libraries = lambda *args: None
class FrozenFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('torch', 'torchaudio'):
            raise AssertionError('frozen CPU finder must be bypassed: ' + name)
sys.meta_path.insert(0, FrozenFinder())
original = list(sys.path)
runtime.activate_runtime(root)
import torch, torch.nested, torch.relocated, torchaudio, torchaudio.nested
assert torch.__version__ == torchaudio.__version__ == '2.8.0+cu128'
assert torch.nested.selected == torchaudio.nested.selected == 'cuda'
assert torch.relocated.selected == 'relocated'
assert importlib.metadata.version('torch') == '2.8.0+cu128'
assert sys.path == original
assert 'forbidden_startup_hook' not in sys.modules
try:
    import torch.missing_from_selected_runtime
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('missing modules must not fall through')
try:
    import ctranslate2
except ImportError:
    pass
else:
    raise AssertionError('other backend must remain isolated')
print('isolated')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "isolated"


@pytest.mark.parametrize("name", ["torch", "torch.submodule", "torchaudio", "ctranslate2", "PySide6"])
def test_activation_rejects_loaded_backend_or_editor(monkeypatch, tmp_path, name):
    monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    with pytest.raises(runtime.CUDARuntimeError, match="fresh worker|editor"):
        runtime.activate_runtime(tmp_path)


def test_finder_rejects_foreign_package_search_path(tmp_path):
    finder = runtime._RuntimeFinder(tmp_path / "selected")
    with pytest.raises(ImportError, match="escaped"):
        finder.find_spec("torch.child", [str(tmp_path / "cpu" / "torch")])
    assert finder.find_spec("json") is None


@pytest.mark.parametrize("preloaded", [False, True])
def test_windows_dll_isolation_is_worker_local_and_never_substitutes_libraries(
    monkeypatch, tmp_path, preloaded,
):
    calls = []

    class NativeFunction:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    kernel = SimpleNamespace(
        GetModuleHandleW=NativeFunction(lambda name: int(preloaded)),
        SetDllDirectoryW=NativeFunction(lambda value: calls.append(("directory", value)) or 1),
        SetDefaultDllDirectories=NativeFunction(lambda value: calls.append(("flags", value)) or 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    monkeypatch.setattr(runtime, "_HANDLES", [])
    monkeypatch.setattr(runtime.os, "add_dll_directory", lambda path: calls.append(("add", path)))
    monkeypatch.setenv("PATH", "untrusted;cpu-backend")
    monkeypatch.setenv("CUDA_PATH", "untrusted")
    monkeypatch.setenv("CUDA_HOME", "untrusted")
    monkeypatch.setenv("KMP_DUPLICATE_LIB_OK", "TRUE")
    site = tmp_path / "site"
    dll = site / "torch" / "lib" / "libiomp5md.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"original upstream library")
    if preloaded:
        with pytest.raises(ImportError, match="already loaded"):
            runtime._isolate_windows_dlls(site)
        assert not calls
        assert os.environ["PATH"] == "untrusted;cpu-backend"
    else:
        runtime._isolate_windows_dlls(site)
        assert calls == [("directory", ""), ("flags", 0xC00), ("add", str(dll.parent))]
        assert "untrusted" not in os.environ["PATH"]
        assert not {"CUDA_PATH", "CUDA_HOME", "KMP_DUPLICATE_LIB_OK"} & os.environ.keys()
    assert dll.read_bytes() == b"original upstream library"


@pytest.mark.parametrize("foreign", [False, True])
def test_loaded_library_audit_rejects_dlls_from_base_backend(monkeypatch, tmp_path, foreign):
    class NativeFunction:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def filename(handle, buffer, size):
        source = tmp_path / ("base" if foreign else "cuda")
        buffer.value = str(source / "torch" / "lib" / handle)
        return len(buffer.value)

    kernel = SimpleNamespace(
        GetModuleHandleW=NativeFunction(lambda name: name),
        GetModuleFileNameW=NativeFunction(filename),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    if foreign:
        with pytest.raises(ImportError, match="original package-local"):
            runtime._verify_loaded_libraries(tmp_path / "cuda")
    else:
        runtime._verify_loaded_libraries(runtime._windows_path(tmp_path / "cuda"))


def test_unrelated_programming_errors_are_not_reported_as_runtime_failures(
    wheels, tmp_path, monkeypatch,
):
    def broken_download(*args):
        raise ValueError("unrelated defect")

    monkeypatch.setattr(runtime, "download_verified", broken_download)
    with pytest.raises(ValueError, match="unrelated defect"):
        _install(tmp_path)
    assert not list((tmp_path / "data").rglob("*.staging"))


def test_abi_and_manifest_revision_isolate_cache_targets():
    manifest = runtime.cuda_runtime_manifest()
    first = runtime._target(Path("data"), manifest, "cp311")
    assert runtime._target(Path("data"), manifest, "cp312") != first
    updated = copy.deepcopy(manifest)
    updated["wheels"]["cp311"][0]["sha256"] = "0" * 64
    assert runtime._target(Path("data"), updated, "cp311") != first


def test_import_does_not_modify_environment_or_import_backends():
    code = """
import os, sys
before = dict(os.environ)
from choicer_voicer_pack_creator import bandit_cuda_runtime
assert dict(os.environ) == before
assert not {'torch', 'torchaudio', 'ctranslate2', 'PySide6'} & sys.modules.keys()
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_optional_native_cuda_cache_import_without_gpu_or_download():
    configured = os.environ.get("CVPC_CUDA_RUNTIME")
    if not configured:
        pytest.skip("Set CVPC_CUDA_RUNTIME to an existing verified cache for the native import smoke")
    root = runtime._windows_path(Path(configured)).resolve()
    code = """
import json, sys
from pathlib import Path
from choicer_voicer_pack_creator.bandit_cuda_runtime import native_import_smoke
print(json.dumps(native_import_smoke(Path(sys.argv[1]))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)], capture_output=True, text=True, check=False,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["torch"] == report["torchaudio"] == "2.8.0+cu128"
    assert report["cuda"] == "12.8" and report["original_package_dlls"] is True
    assert report["frozen"] is False
    assert report["tensor_device"] == "cpu" and report["tensor_dtype"] == "torch.float32"
    assert report["tensor_result"] == [[140.0, 364.0], [364.0, 1100.0]]
    assert report["threads"] == report["interop_threads"] == 1
    assert report["ctranslate2_imported"] is report["qt_imported"] is False
    assert isinstance(report["gpu_available"], bool)
    assert report["compiled_architectures"]
    assert all(
        runtime._windows_path(Path(path)).resolve().is_relative_to(root / "site-packages")
        for path in report["package_paths"].values()
    )
    assert all(
        runtime._windows_path(Path(path)).resolve().is_relative_to(root / "site-packages" / "torch" / "lib")
        for path in report["dll_paths"].values()
    )
