from __future__ import annotations

import ctypes
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from dataclasses import FrozenInstanceError, replace

import pytest

from choicer_voicer_pack_creator import export_resources as resources
from choicer_voicer_pack_creator.export_resources import (
    ExportResourceBudget,
    ResourceError,
    ResourceSnapshot,
    WorkEstimate,
    current_ffmpeg_threads,
    estimate_media_work,
)
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope

MIB = 1024**2
GIB = 1024**3
AMPLE = ResourceSnapshot(12 * GIB, 16 * GIB, 8, 8.0, None)
WORK = WorkEstimate(256 * MIB, 2)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def budget_for(snapshot=AMPLE):
    return ExportResourceBudget(lambda: snapshot)


def test_contract_is_frozen_and_unscoped_media_has_no_override():
    with pytest.raises(FrozenInstanceError):
        AMPLE.cpu_capacity = 4
    with pytest.raises(FrozenInstanceError):
        WORK.memory_bytes = 0
    assert current_ffmpeg_threads() is None


@pytest.mark.parametrize("memory,threads", [(-1, 1), (True, 1), (1.5, 1), (1, 0), (1, True), (1, 1.5)])
def test_invalid_estimates_are_rejected(memory, threads):
    with pytest.raises(ValueError):
        WorkEstimate(memory, threads)


def test_media_estimate_defaults_to_fixed_decoder_and_streaming_pcm_overhead():
    assert estimate_media_work() == WorkEstimate(64 * MIB + 192 * 1024)


@pytest.mark.parametrize("width,height,buffers,padded_width,padded_height", [
    (1, 1, 1, 64, 64),
    (63, 64, 2, 64, 64),
    (64, 64, 1, 64, 64),
    (65, 129, 3, 128, 192),
    (1920, 1080, 8, 1920, 1088),
])
def test_media_estimate_pads_each_dimension_and_counts_explicit_buffers(
    width, height, buffers, padded_width, padded_height,
):
    estimate = estimate_media_work(
        width, height, frame_buffers=buffers, extra_bytes=1234, cpu_threads=2,
    )
    assert estimate == WorkEstimate(
        64 * MIB + 192 * 1024 + padded_width * padded_height * 8 * buffers + 1234, 2,
    )


def test_media_estimate_without_frames_adds_only_explicit_extra_bytes():
    duration = 20
    extra_bytes = 3 * duration * 16000
    assert estimate_media_work(1920, extra_bytes=extra_bytes) == WorkEstimate(
        64 * MIB + 192 * 1024 + extra_bytes,
    )


@pytest.mark.parametrize("kwargs", [
    {"width": -1}, {"height": -1}, {"width": True}, {"height": 1.5},
    {"frame_buffers": -1}, {"frame_buffers": True}, {"frame_buffers": 1.5},
    {"extra_bytes": -1}, {"extra_bytes": True}, {"extra_bytes": 1.5},
    {"frame_buffers": 1}, {"width": 1, "frame_buffers": 1},
    {"height": 1, "frame_buffers": 1},
    {"cpu_threads": 0}, {"cpu_threads": -1}, {"cpu_threads": True},
])
def test_media_estimate_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        estimate_media_work(**kwargs)


@pytest.mark.parametrize("changes", [
    {"available_memory_bytes": -1}, {"available_memory_bytes": True},
    {"available_memory_bytes": 17 * GIB}, {"physical_memory_bytes": 0},
    {"physical_memory_bytes": 1.5}, {"cpu_capacity": 0}, {"cpu_capacity": True},
    {"idle_cpu_capacity": -0.1}, {"idle_cpu_capacity": 8.1},
    {"idle_cpu_capacity": float("nan")}, {"idle_cpu_capacity": float("inf")},
    {"idle_cpu_capacity": True}, {"cpu_capacity": None},
    {"unavailable_reason": ""}, {"unavailable_reason": False},
])
def test_invalid_snapshot_values_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(AMPLE, **changes)


def test_snapshot_provider_contract_is_checked():
    with pytest.raises(TypeError, match="ResourceSnapshot"):
        ExportResourceBudget(lambda: None).snapshot()
    with pytest.raises(TypeError, match="WorkEstimate"):
        budget_for().try_acquire(1)


def test_snapshot_io_failure_uses_a_diagnosed_baseline(monkeypatch):
    events = []

    def unavailable():
        raise OSError("native query unavailable")

    monkeypatch.setattr(resources, "diagnostic_event", lambda event, **details: events.append(details))
    budget = ExportResourceBudget(unavailable)
    assert budget.try_acquire(WORK, optional=True).admission is None
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 1
    assert all("native query unavailable" in event["reason"] for event in events)


def test_two_ample_units_reserve_before_activation_and_release_idempotently():
    budget = budget_for()
    first = budget.try_acquire(WORK, optional=True)
    second = budget.try_acquire(WORK)
    assert first.admission.ffmpeg_threads == second.admission.ffmpeg_threads == 2
    assert not first.retryable and "headroom" in first.reason
    assert current_ffmpeg_threads() is None
    denied = budget.try_acquire(WORK)
    assert denied.admission is None and denied.retryable
    first.admission.release()
    first.admission.release()
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 2
    second.admission.release()
    assert not budget._held


@pytest.mark.parametrize("snapshot,reason", [
    (replace(AMPLE, available_memory_bytes=800 * MIB), "RAM"),
    (replace(AMPLE, idle_cpu_capacity=1.0), "CPU"),
    (ResourceSnapshot(None, None, None, None, "Telemetry unsupported"), "unsupported"),
    (replace(AMPLE, idle_cpu_capacity=None, unavailable_reason="CPU warming up"), "warming"),
    (replace(AMPLE, physical_memory_bytes=None), "incomplete"),
])
def test_constrained_or_unknown_metrics_force_global_serial_baseline(snapshot, reason):
    budget = budget_for(snapshot)
    assert budget.try_acquire(WORK, optional=True).admission is None
    decision = budget.try_acquire(WORK)
    assert reason in decision.reason
    assert decision.admission.ffmpeg_threads == 1
    assert budget.try_acquire(WorkEstimate(0)).admission is None
    decision.admission.release()
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 1


def test_known_ram_is_enforced_when_cpu_is_unknown():
    snapshot = ResourceSnapshot(600 * MIB, 32 * GIB, 8, None, "CPU warming up")
    decision = budget_for(snapshot).try_acquire(WORK)
    assert decision.admission is None and decision.retryable
    assert "available RAM" in decision.reason


def test_installed_ram_is_never_spendable():
    decision = budget_for(replace(AMPLE, available_memory_bytes=200 * MIB)).try_acquire(WORK)
    assert decision.admission is None and decision.retryable
    assert "available RAM" in decision.reason


def test_unknown_physical_ram_still_reserves_512_mib():
    snapshot = ResourceSnapshot(600 * MIB, None, None, None, "Telemetry incomplete")
    assert budget_for(snapshot).try_acquire(WORK).admission is None
    snapshot = replace(snapshot, available_memory_bytes=768 * MIB)
    admission = budget_for(snapshot).acquire(WORK)
    assert admission.ffmpeg_threads == 1
    admission.release()


def test_small_host_uses_quarter_physical_baseline_reserve():
    snapshot = ResourceSnapshot(512 * MIB, GIB, 1, 0.25, None)
    budget = budget_for(snapshot)
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 1
    assert budget.try_acquire(WorkEstimate(256 * MIB + 1)).admission is None


@pytest.mark.parametrize("idle,admitted", [(0.0, False), (0.249, False), (0.25, True), (1.0, True)])
def test_single_core_has_reachable_baseline_but_no_optional_work(idle, admitted):
    budget = budget_for(replace(AMPLE, cpu_capacity=1, idle_cpu_capacity=idle))
    assert budget.try_acquire(WorkEstimate(1), optional=True).admission is None
    decision = budget.try_acquire(WORK)
    assert (decision.admission is not None) == admitted
    if decision.admission:
        assert decision.admission.ffmpeg_threads == 1
        decision.admission.release()


@pytest.mark.parametrize("estimate", [WorkEstimate(1, 3), WorkEstimate(16 * GIB - 512 * MIB + 1)])
def test_impossible_work_fails_without_waiting(estimate):
    clock = Clock()
    budget = ExportResourceBudget(lambda: AMPLE, clock=clock, wait=clock.wait)
    assert not budget.try_acquire(estimate).retryable
    with pytest.raises(ResourceError, match="Cannot admit.*Reduce"):
        budget.acquire(estimate)
    assert not clock.waits and not budget._held


def test_reservations_are_counted_once_against_each_fresh_memory_sample():
    snapshot = replace(AMPLE, available_memory_bytes=4 * GIB)
    budget = budget_for(snapshot)
    first = budget.acquire(WorkEstimate(GIB, 1))
    # Available RAM minus the first claim leaves exactly the optional headroom.
    second = budget.try_acquire(WorkEstimate(2 * GIB, 1), optional=True).admission
    assert second is not None
    second.release()
    for _ in range(5):
        repeated = budget.try_acquire(WorkEstimate(2 * GIB, 1), optional=True).admission
        assert repeated is not None
        repeated.release()
    assert budget.try_acquire(WorkEstimate(2 * GIB + 1), optional=True).admission is None
    first.release()
    assert not budget._held


def test_cpu_claims_are_counted_once_and_released():
    budget = budget_for(replace(AMPLE, idle_cpu_capacity=5.0))
    first = budget.acquire(WORK)
    assert budget.try_acquire(WORK, optional=True).admission is None
    second = budget.try_acquire(WorkEstimate(1, 1), optional=True).admission
    assert second is not None
    second.release()
    first.release()
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 2


@pytest.mark.parametrize("available,working_set", [(2 * GIB, GIB), (12 * GIB, 9 * GIB)])
def test_optional_memory_headroom_has_both_a_floor_and_current_ram_percentage(available, working_set):
    budget = budget_for(replace(AMPLE, available_memory_bytes=available))
    admission = budget.try_acquire(WorkEstimate(working_set), optional=True).admission
    assert admission is not None
    admission.release()
    assert budget.try_acquire(WorkEstimate(working_set + 1), optional=True).admission is None
    baseline = budget.acquire(WorkEstimate(working_set + 1))
    assert baseline.ffmpeg_threads == 1
    baseline.release()


@pytest.mark.parametrize("capacity,idle", [(4, 3.0), (8, 4.0)])
def test_optional_cpu_headroom_has_both_a_floor_and_capacity_percentage(capacity, idle):
    snapshot = replace(AMPLE, cpu_capacity=capacity, idle_cpu_capacity=idle)
    budget = budget_for(snapshot)
    admission = budget.try_acquire(WORK, optional=True).admission
    assert admission is not None
    admission.release()
    budget = budget_for(replace(snapshot, idle_cpu_capacity=idle - 0.01))
    assert budget.try_acquire(WORK, optional=True).admission is None
    baseline = budget.acquire(WORK)
    assert baseline.ffmpeg_threads == 1
    baseline.release()


def test_resource_pressure_changes_only_new_admissions():
    snapshot = AMPLE
    budget = ExportResourceBudget(lambda: snapshot)
    first = budget.acquire(WORK)
    snapshot = replace(AMPLE, idle_cpu_capacity=1.0)
    decision = budget.try_acquire(WORK)
    assert decision.admission is None and decision.retryable
    assert "baseline" in decision.reason
    assert first.ffmpeg_threads == 2 and not first._released
    first.release()
    baseline = budget.acquire(WORK)
    snapshot = AMPLE
    assert budget.try_acquire(WORK).admission is None
    baseline.release()
    with budget.acquire(WORK):
        assert current_ffmpeg_threads() == 2


def test_wait_recovery_deduplicates_progress_and_diagnostics(monkeypatch):
    clock = Clock()
    events, progress = [], []
    monkeypatch.setattr(resources, "diagnostic_event", lambda event, **details: events.append(details))

    def snapshot():
        if clock.now < 0.3:
            return replace(AMPLE, available_memory_bytes=1)
        if clock.now < 0.6:
            return replace(AMPLE, idle_cpu_capacity=0.0)
        return AMPLE

    budget = ExportResourceBudget(snapshot, clock=clock, wait=clock.wait)
    with (
        operation_scope(progress=lambda message, fraction: progress.append((message, fraction))),
        budget.acquire(WORK),
    ):
        assert current_ffmpeg_threads() == 2
    assert len(progress) == 2
    assert len(events) == 3
    assert "RAM" in progress[0][0] and "CPU" in progress[1][0]
    assert all(fraction is None for _, fraction in progress)
    assert not budget._held


def test_default_timeout_is_finite_and_actionable(monkeypatch):
    clock = Clock()
    events, progress = [], []
    monkeypatch.setattr(resources, "diagnostic_event", lambda event, **details: events.append(details))
    budget = ExportResourceBudget(
        lambda: replace(AMPLE, idle_cpu_capacity=0.0), clock=clock, wait=clock.wait,
    )
    with (
        operation_scope(progress=lambda message, fraction: progress.append(message)),
        pytest.raises(ResourceError, match="Timed out after 30s.*Close resource-heavy"),
    ):
        budget.acquire(WORK)
    assert clock.now == 30
    assert len(events) == len(progress) == 1
    assert not budget._held


@pytest.mark.parametrize("deadline", [-1, float("inf"), float("nan"), True, "30"])
def test_invalid_deadline_is_rejected(deadline):
    with pytest.raises(ValueError, match="max_wait_seconds"):
        budget_for().acquire(WORK, max_wait_seconds=deadline)


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_poll_interval_is_rejected(interval):
    with pytest.raises(ValueError, match="poll_interval"):
        ExportResourceBudget(lambda: AMPLE, poll_interval=interval)


def test_zero_timeout_tries_once_without_waiting():
    clock = Clock()
    budget = ExportResourceBudget(
        lambda: replace(AMPLE, idle_cpu_capacity=0.0), clock=clock, wait=clock.wait,
    )
    with pytest.raises(ResourceError, match="Timed out"):
        budget.acquire(WORK, max_wait_seconds=0)
    assert not clock.waits


def test_cancelled_wait_does_not_release_another_owners_claim():
    clock = Clock()
    budget = ExportResourceBudget(lambda: AMPLE, clock=clock, wait=clock.wait)
    owners = [budget.acquire(WORK), budget.acquire(WORK)]
    with (
        operation_scope(cancelled=lambda: clock.now >= 0.2),
        pytest.raises(OperationCancelled),
    ):
        budget.acquire(WORK)
    assert len(budget._held) == 2
    for owner in owners:
        owner.release()


@pytest.mark.parametrize("method", ["acquire", "try_acquire"])
def test_cancellation_during_snapshot_releases_new_claim(method):
    cancelled = False

    def snapshot():
        nonlocal cancelled
        cancelled = True
        return AMPLE

    budget = ExportResourceBudget(snapshot)
    with operation_scope(cancelled=lambda: cancelled), pytest.raises(OperationCancelled):
        getattr(budget, method)(WORK)
    assert not budget._held


def test_cancellation_before_context_entry_releases_claim():
    cancelled = False
    budget = budget_for()
    with operation_scope(cancelled=lambda: cancelled):
        admission = budget.acquire(WORK)
        cancelled = True
        with pytest.raises(OperationCancelled), admission:
            pytest.fail("Cancelled admission activated")
    assert not budget._held and current_ffmpeg_threads() is None


@pytest.mark.parametrize("phase", ["diagnostics", "progress", "body", "activation"])
def test_callback_body_and_context_activation_errors_do_not_leak(monkeypatch, phase):
    budget = budget_for(replace(AMPLE, idle_cpu_capacity=0.0) if phase == "progress" else AMPLE)

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    if phase == "diagnostics":
        monkeypatch.setattr(resources, "diagnostic_event", fail)
    elif phase == "activation":
        class FailedContext:
            get = staticmethod(lambda: None)
            set = staticmethod(fail)
        monkeypatch.setattr(resources, "_active", FailedContext())
    with (
        operation_scope(progress=fail if phase == "progress" else None),
        pytest.raises(RuntimeError, match="injected failure"),
        budget.acquire(WORK),
    ):
        fail()
    assert not budget._held and current_ffmpeg_threads() is None


def test_try_acquire_diagnostic_failure_releases_only_its_new_claim(monkeypatch):
    budget = budget_for()
    owner = budget.acquire(WORK)

    def fail(*args, **kwargs):
        raise OSError("diagnostic write failed")

    monkeypatch.setattr(resources, "diagnostic_event", fail)
    with pytest.raises(OSError, match="diagnostic write"):
        budget.try_acquire(WORK)
    assert list(budget._held) == [owner]
    owner.release()


def test_progress_triggered_cancellation_does_not_sleep():
    clock = Clock()
    cancelled = False

    def progress(message, fraction):
        nonlocal cancelled
        cancelled = True

    budget = ExportResourceBudget(
        lambda: replace(AMPLE, idle_cpu_capacity=0.0), clock=clock, wait=clock.wait,
    )
    with (
        operation_scope(cancelled=lambda: cancelled, progress=progress),
        pytest.raises(OperationCancelled),
    ):
        budget.acquire(WORK)
    assert not clock.waits and not budget._held


def test_nested_acquisition_fails_immediately_across_budget_instances():
    budget, other = budget_for(), budget_for()
    with budget.acquire(WORK):
        for target in (budget, other):
            decision = target.try_acquire(WORK)
            assert decision.admission is None and not decision.retryable
            with pytest.raises(ResourceError, match="Nested"):
                target.acquire(WORK)
        assert current_ffmpeg_threads() == 2
    assert not budget._held and not other._held


def test_nested_activation_releases_only_the_unactivated_claim():
    budget = budget_for()
    first, second = budget.acquire(WORK), budget.acquire(WORK)
    with first:
        with pytest.raises(ResourceError, match="Nested"), second:
            pytest.fail("Nested activation succeeded")
        with pytest.raises(ResourceError, match="twice"), first:
            pytest.fail("Repeated activation succeeded")
        with pytest.raises(ResourceError, match="Exit the active"):
            first.release()
        assert len(budget._held) == 1 and current_ffmpeg_threads() == 2
    with pytest.raises(ResourceError, match="reused"), second:
        pytest.fail("Released reservation reentered")
    assert not budget._held


def test_context_propagation_and_shared_singleton_across_worker_callers(monkeypatch):
    singleton = resources.export_resources
    assert not singleton._held
    monkeypatch.setattr(singleton, "_snapshot_provider", lambda: AMPLE)
    with singleton.acquire(WORK):
        context = copy_context()
        assert Context().run(current_ffmpeg_threads) is None
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(context.run, current_ffmpeg_threads).result(timeout=3) == 2
            assert not pool.submit(
                context.run, lambda: singleton.try_acquire(WORK).retryable,
            ).result(timeout=3)
        assert len(singleton._held) == 1
    assert context.run(current_ffmpeg_threads) is None
    fresh = context.run(singleton.acquire, WORK)
    context.run(fresh.__enter__)
    assert context.run(current_ffmpeg_threads) == 2
    context.run(fresh.__exit__)
    assert not singleton._held


def test_overlapping_callers_atomically_share_the_two_slots(monkeypatch):
    singleton = resources.export_resources
    monkeypatch.setattr(singleton, "_snapshot_provider", lambda: AMPLE)
    start = threading.Barrier(3)
    all_attempted = threading.Barrier(3)
    results = []
    results_lock = threading.Lock()

    def caller():
        start.wait(timeout=3)
        decision = singleton.try_acquire(WORK)
        with results_lock:
            results.append(decision)
        try:
            all_attempted.wait(timeout=3)
            if decision.admission is not None:
                with decision.admission:
                    assert current_ffmpeg_threads() == 2
        finally:
            if decision.admission is not None:
                decision.admission.release()

    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(caller) for _ in range(3)]
        for future in futures:
            future.result(timeout=5)
    assert sum(decision.admission is not None for decision in results) == 2
    assert not singleton._held


class NativeAPI:
    def __init__(self):
        self.available, self.physical = 12 * GIB, 16 * GIB
        self.affinity, self.system_affinity = 0b01010101, 0b11111111
        self.times = (100, 200, 100)
        self.memory_calls = self.cpu_calls = 0
        self.fail_memory = self.fail_affinity = self.fail_cpu = False

    def GlobalMemoryStatusEx(self, pointer):  # noqa: N802
        self.memory_calls += 1
        if self.fail_memory:
            return 0
        status = ctypes.cast(pointer, ctypes.POINTER(resources._MemoryStatus)).contents
        assert status.length == ctypes.sizeof(resources._MemoryStatus)
        status.total_physical, status.available_physical = self.physical, self.available
        return 1

    def GetCurrentProcess(self):  # noqa: N802
        return 123

    def GetProcessAffinityMask(self, process, process_pointer, system_pointer):  # noqa: N802
        assert process == 123
        if self.fail_affinity:
            return 0
        ctypes.cast(process_pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = self.affinity
        ctypes.cast(system_pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = self.system_affinity
        return 1

    def GetSystemTimes(self, *pointers):  # noqa: N802
        self.cpu_calls += 1
        if self.fail_cpu:
            return 0
        for pointer, value in zip(pointers, self.times, strict=True):
            stamp = ctypes.cast(pointer, ctypes.POINTER(resources._FileTime)).contents
            stamp.low, stamp.high = value & 0xFFFFFFFF, value >> 32
        return 1


def test_native_cpu_delta_is_cached_but_available_ram_is_always_fresh():
    clock, api = Clock(), NativeAPI()
    provider = resources._WindowsTelemetry(clock, kernel32=api)
    first = provider()
    assert first.cpu_capacity == 4 and first.idle_cpu_capacity is None
    assert "warming" in first.unavailable_reason
    clock.now = 0.25
    api.times = (180, 280, 120)
    second = provider()
    assert second.idle_cpu_capacity == 3.2
    assert second.unavailable_reason is None
    api.available = GIB
    clock.now = 0.3
    cached = provider()
    assert cached.available_memory_bytes == GIB
    assert cached.idle_cpu_capacity == 3.2
    assert api.cpu_calls == 2 and api.memory_calls == 3


def test_stale_cpu_sample_and_changed_affinity_require_a_new_delta():
    clock, api = Clock(), NativeAPI()
    provider = resources._WindowsTelemetry(clock, kernel32=api)
    provider()
    clock.now = 0.25
    api.times = (180, 280, 120)
    assert provider().idle_cpu_capacity == 3.2
    clock.now = 3.0
    api.times = (280, 380, 140)
    stale = provider()
    assert stale.idle_cpu_capacity is None and "stale" in stale.unavailable_reason
    clock.now = 3.25
    api.times = (360, 460, 160)
    assert provider().idle_cpu_capacity == 3.2
    api.affinity = 1
    clock.now = 3.3
    changed = provider()
    assert changed.cpu_capacity == 1 and changed.idle_cpu_capacity is None
    assert "warming" in changed.unavailable_reason


@pytest.mark.parametrize("times", [(90, 300, 120), (200, 210, 110), (100, 200, 100), (100, 199, 150)])
def test_invalid_native_cpu_counters_are_explicitly_unknown(times):
    clock, api = Clock(), NativeAPI()
    provider = resources._WindowsTelemetry(clock, kernel32=api)
    provider()
    clock.now = 0.25
    api.times = times
    snapshot = provider()
    assert snapshot.idle_cpu_capacity is None
    assert "invalid counters" in snapshot.unavailable_reason


@pytest.mark.parametrize("failure,function", [
    ("fail_memory", "GlobalMemoryStatusEx"),
    ("fail_affinity", "GetProcessAffinityMask"),
    ("fail_cpu", "GetSystemTimes"),
])
def test_native_failures_preserve_independent_metrics_and_recover(monkeypatch, failure, function):
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    clock, api = Clock(), NativeAPI()
    setattr(api, failure, True)
    provider = resources._WindowsTelemetry(clock, kernel32=api)
    snapshot = provider()
    assert function in snapshot.unavailable_reason
    if failure != "fail_memory":
        assert snapshot.available_memory_bytes == 12 * GIB
    else:
        assert snapshot.available_memory_bytes is None and snapshot.cpu_capacity == 4
    setattr(api, failure, False)
    clock.now = 0.25
    api.times = (180, 280, 120)
    provider()
    clock.now = 0.5
    api.times = (260, 360, 140)
    recovered = provider()
    assert recovered.unavailable_reason is None and recovered.idle_cpu_capacity == 3.2


@pytest.mark.parametrize("physical,available", [(0, 0), (GIB, 2 * GIB)])
def test_invalid_native_ram_is_explicitly_unavailable(physical, available):
    api = NativeAPI()
    api.physical, api.available = physical, available
    snapshot = resources._WindowsTelemetry(Clock(), kernel32=api)()
    assert snapshot.available_memory_bytes is None
    assert "invalid physical RAM" in snapshot.unavailable_reason


def test_native_filetime_uses_high_bits():
    clock, api = Clock(), NativeAPI()
    api.times = (2**32 - 20, 2**32 + 80, 2**32 - 1)
    provider = resources._WindowsTelemetry(clock, kernel32=api)
    provider()
    clock.now = 0.25
    api.times = tuple(value + delta for value, delta in zip(api.times, (80, 80, 20), strict=True))
    assert provider().idle_cpu_capacity == 3.2


def test_native_api_load_failure_is_explicitly_unknown(monkeypatch):
    def fail():
        raise OSError("kernel32 load failed")

    monkeypatch.setattr(resources.sys, "platform", "win32")
    monkeypatch.setattr(resources, "_windows_api", fail)
    snapshot = resources._WindowsTelemetry(Clock())()
    assert snapshot.available_memory_bytes is None and snapshot.cpu_capacity is None
    assert "kernel32 load failed" in snapshot.unavailable_reason


def test_memory_programming_errors_are_not_swallowed():
    api = NativeAPI()

    def broken(pointer):
        raise TypeError("bad native declaration")

    api.GlobalMemoryStatusEx = broken
    with pytest.raises(TypeError, match="bad native"):
        resources._WindowsTelemetry(Clock(), kernel32=api)()


def test_unsupported_platform_has_an_explicit_unknown_snapshot(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "unsupported")
    snapshot = resources._WindowsTelemetry(Clock())()
    assert snapshot.available_memory_bytes is None
    assert snapshot.idle_cpu_capacity is None
    assert "unsupported" in snapshot.unavailable_reason
