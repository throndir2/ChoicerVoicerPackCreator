"""Process-local admission for explicit export units, independent of Qt.

These are conservative scheduling estimates, not OS-enforced resource limits.
Available RAM is refreshed for every admission; held estimates are deducted once,
even though some live allocations may already be reflected in that RAM reading.
"""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from choicer_voicer_pack_creator import operations
from choicer_voicer_pack_creator.diagnostics import diagnostic_event

_MIB = 1024**2
_GIB = 1024**3
_CPU_SAMPLE_INTERVAL = 0.25
_CPU_MAX_AGE = 2.0


class ResourceError(RuntimeError):
    """Export work cannot safely enter its resource budget."""


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    available_memory_bytes: int | None
    physical_memory_bytes: int | None
    cpu_capacity: int | None
    idle_cpu_capacity: float | None
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        if self.available_memory_bytes is not None:
            _integer(self.available_memory_bytes, "available_memory_bytes")
        if self.physical_memory_bytes is not None:
            _integer(self.physical_memory_bytes, "physical_memory_bytes", 1)
            if (
                self.available_memory_bytes is not None
                and self.available_memory_bytes > self.physical_memory_bytes
            ):
                raise ValueError("Available RAM cannot exceed physical RAM")
        if self.cpu_capacity is not None:
            _integer(self.cpu_capacity, "cpu_capacity", 1)
        if self.idle_cpu_capacity is not None and (
            isinstance(self.idle_cpu_capacity, bool)
            or not isinstance(self.idle_cpu_capacity, (int, float))
            or not math.isfinite(self.idle_cpu_capacity)
            or self.cpu_capacity is None
            or not 0 <= self.idle_cpu_capacity <= self.cpu_capacity
        ):
            raise ValueError("idle_cpu_capacity must be finite and within known CPU capacity")
        if self.unavailable_reason is not None and (
            not isinstance(self.unavailable_reason, str) or not self.unavailable_reason.strip()
        ):
            raise ValueError("unavailable_reason must be nonempty text or None")


@dataclass(frozen=True, slots=True)
class WorkEstimate:
    memory_bytes: int
    cpu_threads: int = 1

    def __post_init__(self) -> None:
        _integer(self.memory_bytes, "memory_bytes")
        _integer(self.cpu_threads, "cpu_threads", 1)


def estimate_media_work(
    width: int = 0,
    height: int = 0,
    *,
    frame_buffers: int = 0,
    extra_bytes: int = 0,
    cpu_threads: int = 1,
) -> WorkEstimate:
    """Estimate decoder overhead, streaming PCM, and padded eight-byte frame buffers."""
    for name, value in (
        ("width", width), ("height", height),
        ("frame_buffers", frame_buffers), ("extra_bytes", extra_bytes),
    ):
        _integer(value, name)
    if frame_buffers and (not width or not height):
        raise ValueError("Frame buffers require positive width and height")
    padded_width = ((width + 63) // 64) * 64
    padded_height = ((height + 63) // 64) * 64
    return WorkEstimate(
        64 * _MIB + 192 * 1024
        + padded_width * padded_height * 8 * frame_buffers + extra_bytes,
        cpu_threads,
    )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    admission: Admission | None
    reason: str
    retryable: bool


_active: ContextVar[Admission | None] = ContextVar("export_resource_admission", default=None)


def current_ffmpeg_threads() -> int | None:
    admission = _active.get()
    return admission.ffmpeg_threads if admission is not None and not admission._released else None


class Admission:
    """A reservation starts at acquisition; only its context activates FFmpeg caps."""

    def __init__(
        self, budget: ExportResourceBudget, ffmpeg_threads: int, conservative: bool,
    ) -> None:
        self._budget = budget
        self._ffmpeg_threads = ffmpeg_threads
        self._conservative = conservative
        self._released = False
        self._token: Token[Admission | None] | None = None

    @property
    def ffmpeg_threads(self) -> int:
        return self._ffmpeg_threads

    def __enter__(self) -> Admission:
        with self._budget._lock:
            if self._released or self._token is not None:
                raise ResourceError("An export admission cannot be reused or entered twice")
            try:
                if current_ffmpeg_threads() is not None:
                    raise ResourceError("Nested export admissions are not supported")
                operations.check_cancelled()
                self._token = _active.set(self)
            except BaseException:
                self.release()
                raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._token is None:
            raise ResourceError("The export admission is not active")
        try:
            _active.reset(self._token)
        finally:
            with self._budget._lock:
                self._token = None
                self.release()

    def release(self) -> None:
        """Release a declined, not-yet-activated reservation; repeated release is safe."""
        with self._budget._lock:
            if self._token is not None:
                raise ResourceError("Exit the active export admission context before releasing it")
            self._budget._held.pop(self, None)
            self._released = True


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("memory_load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64), ("available_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
        ("available_extended_virtual", ctypes.c_uint64),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def value(self) -> int:
        return (int(self.high) << 32) | int(self.low)


def _windows_api() -> Any:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = ctypes.c_void_p
    api.GetProcessAffinityMask.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ]
    api.GetProcessAffinityMask.restype = ctypes.c_int
    api.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
    api.GetSystemTimes.restype = ctypes.c_int
    return api


def _native_error(function: str) -> OSError:
    return OSError(ctypes.get_last_error(), f"{function} failed")


class _WindowsTelemetry:
    def __init__(self, clock: Callable[[], float], *, kernel32: Any = None) -> None:
        self._clock = clock
        self._api = kernel32
        self._previous: tuple[float, int, tuple[int, int, int]] | None = None
        self._idle: float | None = None
        self._cpu_reason = "CPU load is warming up"

    def __call__(self) -> ResourceSnapshot:
        if self._api is None:
            if sys.platform != "win32":
                return ResourceSnapshot(None, None, None, None, "Windows telemetry is unsupported")
            try:
                self._api = _windows_api()
            except OSError as error:
                return ResourceSnapshot(None, None, None, None, f"Windows telemetry: {error}")
        reasons = []
        available = physical = capacity = idle = None
        try:
            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not self._api.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise _native_error("GlobalMemoryStatusEx")
            if not status.total_physical or status.available_physical > status.total_physical:
                raise OSError("GlobalMemoryStatusEx returned invalid physical RAM values")
            available, physical = int(status.available_physical), int(status.total_physical)
        except OSError as error:
            reasons.append(f"RAM telemetry unavailable: {error}")
        try:
            process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
            if not self._api.GetProcessAffinityMask(
                self._api.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask),
            ):
                raise _native_error("GetProcessAffinityMask")
            capacity = (process_mask.value & system_mask.value).bit_count()
            if not capacity:
                raise OSError("GetProcessAffinityMask returned no usable CPUs")
        except OSError as error:
            capacity = None
            self._previous = None
            self._idle = None
            reasons.append(f"CPU capacity unavailable: {error}")
        if capacity is not None:
            idle, reason = self._sample_cpu(capacity)
            if reason:
                reasons.append(reason)
        return ResourceSnapshot(available, physical, capacity, idle, "; ".join(reasons) or None)

    def _sample_cpu(self, capacity: int) -> tuple[float | None, str | None]:
        now = self._clock()
        previous = self._previous
        if previous is not None and capacity == previous[1]:
            age = now - previous[0]
            if 0 <= age < _CPU_SAMPLE_INTERVAL:
                return self._idle, self._cpu_reason
        try:
            times = (_FileTime(), _FileTime(), _FileTime())
            if not self._api.GetSystemTimes(*(ctypes.byref(value) for value in times)):
                raise _native_error("GetSystemTimes")
            values = tuple(value.value() for value in times)
        except OSError as error:
            self._previous = None
            self._idle = None
            self._cpu_reason = f"CPU load unavailable: {error}"
            return None, self._cpu_reason
        self._previous = (now, capacity, values)
        self._idle = None
        if previous is None or capacity != previous[1]:
            self._cpu_reason = "CPU load is warming up"
        elif not 0 <= now - previous[0] <= _CPU_MAX_AGE:
            self._cpu_reason = "CPU load sample is stale"
        else:
            idle, kernel, user = (value - old for value, old in zip(values, previous[2], strict=True))
            total = kernel + user  # Windows kernel time includes idle time.
            if min(idle, kernel, user) < 0 or total <= 0 or idle > kernel:
                self._cpu_reason = "CPU load sample has invalid counters"
            else:
                # GetSystemTimes is system-wide, not a per-affinity-core load measurement.
                self._idle = capacity * idle / total
                self._cpu_reason = None
        return self._idle, self._cpu_reason


class ExportResourceBudget:
    def __init__(
        self,
        snapshot_provider: Callable[[], ResourceSnapshot] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.1,
    ) -> None:
        if (
            isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
            or not math.isfinite(poll_interval) or poll_interval <= 0
        ):
            raise ValueError("poll_interval must be positive and finite")
        self._clock, self._wait, self._poll_interval = clock, wait, poll_interval
        self._snapshot_provider = (
            _WindowsTelemetry(clock) if snapshot_provider is None else snapshot_provider
        )
        self._lock = threading.RLock()
        self._held: dict[Admission, WorkEstimate] = {}

    def snapshot(self) -> ResourceSnapshot:
        with self._lock:
            try:
                snapshot = self._snapshot_provider()
            except OSError as error:
                return ResourceSnapshot(
                    None, None, None, None, f"Resource telemetry unavailable: {error}",
                )
            if not isinstance(snapshot, ResourceSnapshot):
                raise TypeError("snapshot_provider must return ResourceSnapshot")
            return snapshot

    def _decide(self, estimate: WorkEstimate, optional: bool) -> AdmissionDecision:
        if not isinstance(estimate, WorkEstimate):
            raise TypeError("estimate must be WorkEstimate")
        if current_ffmpeg_threads() is not None:
            return AdmissionDecision(None, "Nested export admissions are not supported", False)
        if estimate.cpu_threads > 2:
            return AdmissionDecision(None, "An export unit can request at most two CPU threads", False)
        with self._lock:
            snapshot = self.snapshot()
            physical = snapshot.physical_memory_bytes
            reserve = min(512 * _MIB, physical // 4) if physical is not None else 512 * _MIB
            if physical is not None and estimate.memory_bytes > physical - reserve:
                return AdmissionDecision(
                    None, "The export working set cannot fit in physical RAM with editor headroom", False,
                )
            if len(self._held) >= 2:
                return AdmissionDecision(None, "Both export resource slots are in use", True)
            if any(admission._conservative for admission in self._held):
                return AdmissionDecision(None, "A serial baseline export unit is still active", True)
            available = snapshot.available_memory_bytes
            idle, capacity = snapshot.idle_cpu_capacity, snapshot.cpu_capacity
            memory_held = sum(work.memory_bytes for work in self._held.values())
            cpu_held = sum(admission.ffmpeg_threads for admission in self._held)
            if available is not None and available - memory_held < estimate.memory_bytes + reserve:
                return AdmissionDecision(
                    None, "Insufficient available RAM for the export working set and editor headroom", True,
                )
            unknown = snapshot.unavailable_reason or (
                "Resource telemetry is incomplete"
                if None in (available, physical, idle, capacity) else None
            )
            ample_memory = (
                available is not None
                and available - memory_held >= estimate.memory_bytes + max(_GIB, available // 4)
            )
            ample_cpu = (
                capacity is not None and capacity > 1 and idle is not None
                and idle - cpu_held >= estimate.cpu_threads + max(1, capacity / 4)
            )
            if not unknown and ample_memory and ample_cpu:
                threads, conservative = estimate.cpu_threads, False
                reason = "Admitted with available RAM and idle CPU headroom"
            else:
                constraint = unknown or (
                    "Available RAM needs the smaller baseline reserve" if not ample_memory
                    else "Idle CPU capacity needs the one-thread baseline"
                )
                if optional:
                    return AdmissionDecision(None, f"Optional export work declined: {constraint}", True)
                if self._held:
                    return AdmissionDecision(None, "The serial baseline must wait for active export units", True)
                if (
                    capacity is not None and idle is not None
                    and idle - cpu_held < min(1, capacity / 4)
                ):
                    return AdmissionDecision(
                        None, "Insufficient idle CPU capacity even for a one-thread export baseline", True,
                    )
                threads, conservative = 1, True
                reason = f"Admitted serial one-thread baseline: {constraint}"
            admission = Admission(self, threads, conservative)
            self._held[admission] = estimate
            return AdmissionDecision(admission, reason, False)

    @staticmethod
    def _diagnose(decision: AdmissionDecision) -> None:
        diagnostic_event(
            "export_resource_admission", reason=decision.reason, retryable=decision.retryable,
            admitted=decision.admission is not None,
            ffmpeg_threads=decision.admission.ffmpeg_threads if decision.admission else None,
        )

    def try_acquire(self, estimate: WorkEstimate, *, optional: bool = False) -> AdmissionDecision:
        operations.check_cancelled()
        decision = self._decide(estimate, optional)
        try:
            operations.check_cancelled()
            self._diagnose(decision)
        except BaseException:
            if decision.admission is not None:
                decision.admission.release()
            raise
        return decision

    def acquire(self, estimate: WorkEstimate, *, max_wait_seconds: float = 30) -> Admission:
        if (
            isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, (int, float))
            or not math.isfinite(max_wait_seconds) or max_wait_seconds < 0
        ):
            raise ValueError("max_wait_seconds must be nonnegative and finite")
        deadline = self._clock() + max_wait_seconds
        previous_reason = None
        while True:
            operations.check_cancelled()
            decision = self._decide(estimate, False)
            try:
                operations.check_cancelled()
                if decision.reason != previous_reason:
                    self._diagnose(decision)
                if decision.admission is not None:
                    return decision.admission
                if not decision.retryable:
                    raise ResourceError(f"Cannot admit export work: {decision.reason}. Reduce the requested working set or thread count.")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ResourceError(
                        f"Timed out after {max_wait_seconds:g}s waiting for export resources: "
                        f"{decision.reason}. Close resource-heavy applications or reduce the "
                        "export working set, then retry."
                    )
                if decision.reason != previous_reason:
                    operations.report(f"Waiting for export resources: {decision.reason}...", None)
                    previous_reason = decision.reason
                operations.check_cancelled()
                remaining = deadline - self._clock()
                if remaining > 0:
                    self._wait(min(self._poll_interval, remaining))
            except BaseException:
                if decision.admission is not None:
                    decision.admission.release()
                raise


export_resources = ExportResourceBudget()
