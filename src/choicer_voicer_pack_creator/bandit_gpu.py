"""Worker-only NVIDIA discovery and full-size BandIt CUDA qualification."""
from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.separation import SeparationError, check_cancel

MINIMUM_FREE_BYTES = 3 * 1024**3
HEADROOM_BYTES = 512 * 1024**2


class GPUUnavailable(SeparationError):
    """A diagnosed device/runtime incompatibility; retry in a fresh CPU process."""


def probe_nvidia() -> dict[str, Any]:
    """Only a download-eligibility hint, never proof of usable Torch execution."""
    if sys.platform != "win32":
        return {"candidate": False, "reason": "CUDA acceleration currently supports Windows only."}
    try:
        driver = ctypes.WinDLL("nvcuda.dll", winmode=0x00000800)
    except OSError as error:
        return {"candidate": False, "reason": f"NVIDIA CUDA driver unavailable: {error}"}
    driver.cuInit.argtypes = [ctypes.c_uint]
    driver.cuInit.restype = ctypes.c_int
    driver.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver.cuDriverGetVersion.restype = ctypes.c_int
    driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver.cuDeviceGetCount.restype = ctypes.c_int
    code = driver.cuInit(0)
    if code:
        return {
            "candidate": False,
            "reason": "No NVIDIA CUDA device is available (CUDA 100)."
            if code == 100 else f"NVIDIA driver initialization failed (CUDA {code}).",
        }
    version, count = ctypes.c_int(), ctypes.c_int()
    code = driver.cuDriverGetVersion(ctypes.byref(version))
    if code or version.value < 12080:
        return {
            "candidate": False,
            "reason": f"NVIDIA driver must support CUDA 12.8 (reported {version.value}, error {code}).",
        }
    code = driver.cuDeviceGetCount(ctypes.byref(count))
    if code or count.value < 1:
        return {"candidate": False, "reason": f"No usable NVIDIA CUDA device (CUDA {code})."}
    return {"candidate": True, "reason": "", "driver_version": version.value, "count": count.value}


def recoverable_gpu_error(error: BaseException) -> bool:
    """Do not turn model, input, filesystem, or arbitrary CUDA logic errors into retries."""
    if isinstance(error, OperationCancelled):
        return False
    if isinstance(error, GPUUnavailable):
        return True
    if isinstance(error, SeparationError):
        return False
    if not isinstance(error, RuntimeError):
        return False
    text = str(error).lower()
    return any(marker in text for marker in (
        "cuda out of memory", "cuda error: out of memory",
        "cuda error: no kernel image is available", "cuda error: invalid device function",
        "cuda error: initialization error", "cuda error: insufficient driver",
        "cuda driver version is insufficient", "found no nvidia driver",
        "cuda error: system driver mismatch", "cuda error: unsupported ptx version",
        "cuda-capable device(s) is/are busy or unavailable",
        "cublas_status_alloc_failed", "cudnn_status_alloc_failed",
        "cublas_status_arch_mismatch", "cudnn_status_arch_mismatch",
        "cudnn_status_not_supported",
    ))


def architecture_supported(capability: tuple[int, int], architectures: list[str]) -> bool:
    target = capability[0] * 10 + capability[1]
    for architecture in architectures:
        kind, _, number = architecture.partition("_")
        if not number.isdigit():
            continue
        compiled = int(number)
        if kind == "sm" and compiled // 10 == target // 10 and compiled <= target:
            return True
        if kind == "compute" and compiled <= target:
            return True
    return False


def load_gpu_model(
    path: Path, threads: int, progress: Callable[[str, float | None], None],
    cancelled: Callable[[], bool],
) -> tuple[Any, str, dict[str, Any]]:
    from choicer_voicer_pack_creator._bandit import CHUNK_FRAMES, load_model
    from choicer_voicer_pack_creator.bandit_runtime import configure_threads, make_predictor

    check_cancel(cancelled)
    configure_threads(threads)
    try:
        import torch
        import torchaudio
    except (ImportError, OSError) as error:
        raise GPUUnavailable(f"CUDA runtime could not initialize: {error}") from error
    if (
        torch.__version__ != "2.8.0+cu128" or torchaudio.__version__ != "2.8.0+cu128"
        or torch.version.cuda != "12.8"
    ):
        raise GPUUnavailable("The optional runtime is not pinned torch/torchaudio 2.8.0+cu128.")
    torch.set_num_threads(threads)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    if not torch.cuda.is_available():
        raise GPUUnavailable("The CUDA runtime cannot initialize an NVIDIA device.")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    candidates = []
    reasons = []
    for index in range(torch.cuda.device_count()):
        check_cancel(cancelled)
        capability = torch.cuda.get_device_capability(index)
        if not architecture_supported(capability, torch.cuda.get_arch_list()):
            reasons.append(f"CUDA:{index} architecture sm_{capability[0]}{capability[1]} unsupported")
            continue
        try:
            free, total = torch.cuda.mem_get_info(index)
        except RuntimeError as error:
            if not recoverable_gpu_error(error):
                raise
            reasons.append(f"CUDA:{index}: {error}")
            continue
        if free < MINIMUM_FREE_BYTES + HEADROOM_BYTES:
            reasons.append(f"CUDA:{index} needs 3.5 GiB free VRAM; {free / 1024**3:.2f} GiB available")
            continue
        candidates.append((free, index, total, capability))
    if not candidates:
        raise GPUUnavailable("; ".join(reasons) or "No compatible NVIDIA device is available.")
    free, index, total, capability = max(candidates)
    device = f"cuda:{index}"
    name = torch.cuda.get_device_name(index)
    check_cancel(cancelled)
    # Strict checkpoint validation and CPU construction must not be hidden as GPU fallback.
    model = load_model(path, "cpu", check_cancelled=lambda: check_cancel(cancelled))
    progress(f"Qualifying NVIDIA {name}: full eight-second float32 CUDA inference...", None)
    check_cancel(cancelled)
    model = model.to(device)
    import numpy as np

    make_predictor(model, device=device, cancelled=cancelled)(
        np.zeros((CHUNK_FRAMES, 2), dtype=np.float32),
    )
    torch.cuda.synchronize(index)
    check_cancel(cancelled)
    available_after, _ = torch.cuda.mem_get_info(index)
    if available_after < HEADROOM_BYTES:
        raise GPUUnavailable("CUDA qualification left less than 512 MiB of free VRAM headroom.")
    return model, device, {
        "device": device, "device_name": name, "runtime": torch.__version__,
        "capability": list(capability), "free_vram_before": free, "total_vram": total,
        "peak_vram_allocated": torch.cuda.max_memory_allocated(index),
    }
