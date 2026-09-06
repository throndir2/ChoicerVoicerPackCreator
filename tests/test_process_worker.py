from __future__ import annotations

import multiprocessing
import os
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from choicer_voicer_pack_creator import process_worker
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.operations import OperationCancelled, operation_scope
from choicer_voicer_pack_creator.process_worker import (
    ProcessWorkerCancelled,
    ProcessWorkerError,
    ProcessWorkerTimeout,
    run_process_worker,
)


def _return(emit, value):
    return {
        "value": value,
        "pid": os.getpid(),
        "start_method": multiprocessing.get_start_method(),
        "qt_imported": any(name.startswith("PySide6") for name in sys.modules),
    }


def _progress(emit, count, delay):
    for index in range(count):
        emit("progress", {"index": index})
        time.sleep(delay)
    return count


def _merge(emit):
    emit("merge", {})
    time.sleep(1.3)
    return "merged"


def _hang(emit):
    emit("started", {})
    threading.Event().wait()


def _noise(emit):
    while True:
        emit("diagnostic", {"message": "still waiting"})
        time.sleep(0.005)


def _fail(emit, error_type):
    raise error_type("actual remote failure")


def _bad_result(emit):
    return lambda: None


def _exit(emit):
    os._exit(37)


def _threaded_events(emit):
    def send(index):
        for item in range(30):
            emit("thread", {"thread": index, "item": item})

    threads = [threading.Thread(target=send, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return "complete"


def _descendants(emit, exit_mode, locked_file=""):
    script = """
import os, subprocess, sys, time
lock = open(sys.argv[1], "rb") if sys.argv[1] else None
child_script = (
    'import sys, time; '
    'lock = open(sys.argv[1], "rb") if sys.argv[1] else None; '
    'print("locked", flush=True); time.sleep(60)'
)
child = subprocess.Popen(
    [sys.executable, "-c", child_script, sys.argv[1]], stdout=subprocess.PIPE, text=True
)
assert child.stdout.readline().strip() == "locked"
print(str(os.getpid()) + ' ' + str(child.pid), flush=True)
time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, locked_file], stdout=subprocess.PIPE, text=True
    )
    try:
        pids = [int(pid) for pid in child.stdout.readline().split()]
    finally:
        child.stdout.close()
    emit("descendants", {"pids": pids})
    if exit_mode == "return":
        return pids
    if exit_mode == "exit":
        os._exit(41)
    threading.Event().wait()


def _partial_response(connection):
    connection.sendall(process_worker._encode_message(("ready",)))
    process_worker._read_task(connection)
    # A receiver using poll()+recv() or Queue.get(timeout) would now hang in its read.
    connection.sendall(process_worker._HEADER.pack(100_000_000) + b"x" * 128_000)
    threading.Event().wait()


def _partial_header(connection):
    connection.sendall(process_worker._encode_message(("ready",)))
    process_worker._read_task(connection)
    connection.sendall(b"\x00\x00")
    threading.Event().wait()


def _connect(emit, address):
    with socket.create_connection(address, timeout=2):
        pass


def _never_cancelled():
    return False


def _ignore_event(event, details):
    return False


@pytest.fixture(autouse=True)
def _no_leaked_workers():
    before = {child.pid for child in multiprocessing.active_children()}
    before_threads = set(threading.enumerate())
    yield
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert set(threading.enumerate()) == before_threads


def _is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID has already exited.
                return False
            raise ctypes.WinError(error)
        try:
            result = api.WaitForSingleObject(handle, 0)
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return result != 0
        finally:
            if not api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        status = Path(f"/proc/{pid}/stat")
        try:
            return status.read_text().split(") ", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    return True


def _assert_stopped(pids):
    if os.name == "nt":
        assert not any(_is_running(pid) for pid in pids)
        return
    until = time.monotonic() + 5
    while any(_is_running(pid) for pid in pids) and time.monotonic() < until:
        time.sleep(0.05)
    assert not any(_is_running(pid) for pid in pids)


@pytest.mark.parametrize("mode", ["capture", "video-progress"])
def test_media_cancellation_reaps_descendants_even_without_pipe_output(tmp_path, mode):
    pids_file = tmp_path / "pids"
    locked_file = tmp_path / "owned-stage"
    locked_file.write_bytes(b"synthetic")
    script = """
import os, pathlib, subprocess, sys, time
child = subprocess.Popen([
        sys.executable, "-c",
        "import sys,time; locked=open(sys.argv[1], 'rb'); print('ready',flush=True); time.sleep(60)",
        sys.argv[2]
], stdout=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == "ready"
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()) + " " + str(child.pid))
time.sleep(60)
"""
    command = [sys.executable, "-c", script, str(pids_file), str(locked_file)]
    media = MediaTools.__new__(MediaTools)
    started = time.monotonic()
    with pytest.raises(OperationCancelled), operation_scope(pids_file.exists):
        if mode == "capture":
            media._capture(command)
        else:
            media._run_video_conversion(command, None)
    assert time.monotonic() - started < 5
    pids = [int(pid) for pid in pids_file.read_text().split()]
    _assert_stopped(pids)
    locked_file.unlink()


@pytest.fixture
def locked_file():
    path = Path(f".process-worker-{uuid4().hex}.locked")
    path.write_bytes(b"descendant-held staging file")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _assert_delete_access(path):
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0x00010000, 7, None, 3, 0, None)  # DELETE access
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def test_spawn_returns_trusted_picklable_values_without_qt():
    value = {"path": Path("relative"), "set": {1, 2}, "bytes": b"media"}
    result = run_process_worker(
        _return, (value,), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
    )
    assert result["value"] == value
    assert result["pid"] != os.getpid()
    assert result["start_method"] == "spawn"
    assert result["qt_imported"] is False


def test_large_result_is_transferred():
    value = b"x" * 5_000_000
    result = run_process_worker(
        _return, (value,), on_event=_ignore_event, cancelled=_never_cancelled, timeout=10
    )
    assert result["value"] == value


def test_useful_progress_renews_idle_deadline():
    events = []

    def on_event(event, details):
        events.append((event, details))
        return True

    result = run_process_worker(
        _progress, (10, 0.15), on_event=on_event, cancelled=_never_cancelled,
        idle_timeout=0.9, timeout=6,
    )
    assert result == 10
    assert events == [("progress", {"index": index}) for index in range(10)]


def test_diagnostics_do_not_renew_idle_deadline():
    events = []

    def on_event(event, details):
        events.append(event)
        return False

    started = time.monotonic()
    with pytest.raises(ProcessWorkerTimeout, match="no useful progress"):
        run_process_worker(
            _noise, (), on_event=on_event, cancelled=_never_cancelled,
            idle_timeout=1.2, timeout=8,
        )
    assert events
    assert time.monotonic() - started < 4


def test_idle_deadline_can_expand_for_merge():
    idle_limit = 0.9

    def on_event(event, details):
        nonlocal idle_limit
        idle_limit = 2.5
        return True

    assert run_process_worker(
        _merge, (), on_event=on_event, cancelled=_never_cancelled,
        idle_timeout=lambda: idle_limit, timeout=6,
    ) == "merged"


def test_dynamic_idle_deadline_is_rechecked_without_events():
    idle_limit = 5

    def waiting(elapsed):
        nonlocal idle_limit
        idle_limit = 0.5

    with pytest.raises(ProcessWorkerTimeout, match="no useful progress"):
        run_process_worker(
            _hang, (), on_event=_ignore_event, cancelled=_never_cancelled,
            idle_timeout=lambda: idle_limit, waiting=waiting, timeout=8,
        )


@pytest.mark.parametrize("target", [_hang, _noise])
def test_hard_timeout_ignores_progress_and_waiting_keeps_ticking(target):
    elapsed = []
    started = time.monotonic()
    with pytest.raises(ProcessWorkerTimeout, match="time limit"):
        run_process_worker(
            target, (), on_event=lambda *_: True, cancelled=_never_cancelled,
            timeout=2.2, waiting=elapsed.append,
        )
    assert time.monotonic() - started < 5
    assert len(elapsed) == 2
    assert 1 <= elapsed[0] < 1.4
    assert 2 <= elapsed[1] < 2.2


@pytest.mark.parametrize("error_type", [ValueError, OSError, SystemExit, KeyboardInterrupt])
def test_child_failures_preserve_type_message_and_traceback(error_type):
    with pytest.raises(ProcessWorkerError, match="actual remote failure") as caught:
        run_process_worker(
            _fail, (error_type,), on_event=_ignore_event, cancelled=_never_cancelled,
            timeout=5,
        )
    assert caught.value.error_type == error_type.__name__
    assert "raise error_type" in caught.value.remote_traceback
    assert f"{error_type.__name__}: actual remote failure" in caught.value.remote_traceback


def test_unpickleable_return_is_reported_as_failure():
    with pytest.raises(ProcessWorkerError, match="local object") as caught:
        run_process_worker(
            _bad_result, (), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
        )
    assert caught.value.error_type == "AttributeError"
    assert "_encode_message" in caught.value.remote_traceback


def test_abrupt_exit_without_result_is_failure():
    with pytest.raises(ProcessWorkerError, match="without returning a result") as caught:
        run_process_worker(
            _exit, (), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
        )
    assert caught.value.error_type == "WorkerExited"


def test_events_are_thread_safe():
    events = []
    result = run_process_worker(
        _threaded_events, (),
        on_event=lambda event, details: events.append((details["thread"], details["item"])),
        cancelled=_never_cancelled, timeout=5,
    )
    assert result == "complete"
    assert sorted(events) == [(thread, item) for thread in range(4) for item in range(30)]


def test_cancellation_interrupts_a_blocked_worker():
    cancel = False

    def on_event(event, details):
        nonlocal cancel
        cancel = True
        return True

    started = time.monotonic()
    with pytest.raises(ProcessWorkerCancelled):
        run_process_worker(
            _hang, (), on_event=on_event, cancelled=lambda: cancel, timeout=8
        )
    assert time.monotonic() - started < 4


def test_cancellation_before_launch_does_not_start_a_process(monkeypatch):
    def forbidden(*args):
        pytest.fail("Started a process despite cancellation")

    monkeypatch.setattr(multiprocessing, "get_context", forbidden)
    with pytest.raises(ProcessWorkerCancelled):
        run_process_worker(_hang, (), on_event=_ignore_event, cancelled=lambda: True)


def test_cancellation_wins_a_race_with_the_result(monkeypatch):
    pop = process_worker._MessageReader.pop
    cancel = False

    def cancel_at_result(reader):
        nonlocal cancel
        message = pop(reader)
        if message is not None and message[0] == "result":
            cancel = True
        return message

    monkeypatch.setattr(process_worker._MessageReader, "pop", cancel_at_result)
    with pytest.raises(ProcessWorkerCancelled):
        run_process_worker(
            _return, (None,), on_event=_ignore_event, cancelled=lambda: cancel, timeout=5
        )


def test_cancellation_during_cleanup_also_wins_the_result_race(monkeypatch):
    reap = process_worker._reap_process
    cancel = False

    def cancel_after_reaping(process):
        nonlocal cancel
        reap(process)
        cancel = True

    monkeypatch.setattr(process_worker, "_reap_process", cancel_after_reaping)
    with pytest.raises(ProcessWorkerCancelled):
        run_process_worker(
            _return, (None,), on_event=_ignore_event, cancelled=lambda: cancel, timeout=5
        )


@pytest.mark.parametrize("replacement", [_partial_response, _partial_header])
@pytest.mark.parametrize("cancel", [False, True])
def test_incomplete_ipc_cannot_block_timeout_or_cancellation(monkeypatch, replacement, cancel):
    monkeypatch.setattr(process_worker, "_worker_main", replacement)
    started = time.monotonic()
    with pytest.raises(ProcessWorkerCancelled if cancel else ProcessWorkerTimeout):
        run_process_worker(
            _return, (None,), on_event=_ignore_event,
            cancelled=lambda: cancel and time.monotonic() - started > 1.2,
            timeout=8 if cancel else 1.2,
        )
    assert time.monotonic() - started < 4


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("return", None),
        ("exit", ProcessWorkerError),
        ("timeout", ProcessWorkerTimeout),
        ("cancel", ProcessWorkerCancelled),
        ("callback", ValueError),
        ("waiting", ValueError),
    ],
)
def test_entire_process_tree_is_cleaned_on_every_exit(mode, expected, locked_file):
    pids = []
    cancel = False

    def on_event(event, details):
        nonlocal cancel
        pids.extend(details["pids"])
        if os.name == "nt":
            with pytest.raises(OSError) as caught:
                _assert_delete_access(locked_file)
            assert caught.value.winerror == 32
        if mode == "callback":
            raise ValueError("callback failed")
        if mode == "cancel":
            cancel = True
        return True

    def waiting(elapsed):
        if mode == "waiting":
            raise ValueError("waiting callback failed")

    def run():
        return run_process_worker(
            _descendants, (mode, str(locked_file)), on_event=on_event, cancelled=lambda: cancel,
            waiting=waiting, timeout=1.6 if mode == "timeout" else 6,
        )

    if expected is None:
        assert run() == pids
    else:
        with pytest.raises(expected):
            run()
    if os.name == "nt":
        _assert_delete_access(locked_file)
    locked_file.unlink()
    assert len(pids) == 2
    _assert_stopped(pids)


@pytest.mark.skipif(os.name != "nt", reason="Windows descendant file-handle rundown")
@pytest.mark.parametrize("attempt", range(10))
@pytest.mark.parametrize("mode", ["return", "exit"])
def test_descendant_file_locks_are_released_immediately(mode, attempt, locked_file):
    pids = []

    def on_event(event, details):
        pids.extend(details["pids"])
        return True

    if mode == "return":
        run_process_worker(
            _descendants, (mode, str(locked_file)), on_event=on_event,
            cancelled=_never_cancelled, timeout=5,
        )
    else:
        with pytest.raises(ProcessWorkerError, match="without returning a result"):
            run_process_worker(
                _descendants, (mode, str(locked_file)), on_event=on_event,
                cancelled=_never_cancelled, timeout=5,
            )
    # No retries or grace interval: staging cleanup/retry happens immediately in callers.
    _assert_delete_access(locked_file)
    locked_file.unlink()
    assert not any(_is_running(pid) for pid in pids)


def test_launch_failure_propagates_and_closes_resources(monkeypatch):
    context = multiprocessing.get_context("spawn")
    process = context.Process()
    closed = []
    original_close = process.close

    def close():
        closed.append(True)
        original_close()

    def fail():
        raise OSError("process launch failed")

    monkeypatch.setattr(process, "start", fail)
    monkeypatch.setattr(process, "close", close)
    monkeypatch.setattr(context, "Process", lambda **kwargs: process)
    with pytest.raises(OSError, match="process launch failed"):
        run_process_worker(
            _hang, (), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
        )
    assert closed == [True]


def test_bad_target_fails_before_process_launch(monkeypatch):
    def forbidden(*args):
        pytest.fail("Launched a process with an unpickleable target")

    monkeypatch.setattr(multiprocessing, "get_context", forbidden)
    with pytest.raises(AttributeError, match="local object"):
        run_process_worker(
            lambda emit: None, (), on_event=_ignore_event, cancelled=_never_cancelled
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment and startup gate")
def test_failed_job_assignment_never_allows_target_to_start(monkeypatch):
    def fail(self, pid):
        time.sleep(0.8)
        raise OSError("cannot assign worker job")

    monkeypatch.setattr(process_worker._WindowsJob, "assign", fail)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError, match="cannot assign worker job"):
            run_process_worker(
                _connect, (listener.getsockname(),), on_event=_ignore_event,
                cancelled=_never_cancelled, timeout=5,
            )
        assert select.select([listener], [], [], 0)[0] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows job cleanup errors")
def test_cleanup_errors_are_not_suppressed(monkeypatch):
    close = process_worker._WindowsJob.close

    def fail_after_closing(job):
        close(job)
        raise OSError("job cleanup failed")

    monkeypatch.setattr(process_worker._WindowsJob, "close", fail_after_closing)
    with pytest.raises(OSError, match="job cleanup failed"):
        run_process_worker(
            _return, (None,), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows bounded process-tree cleanup")
def test_job_drain_timeout_is_bounded_and_not_a_retryable_worker_timeout(monkeypatch):
    monkeypatch.setattr(process_worker, "_CLEANUP_TIMEOUT", 0.15)
    monkeypatch.setattr(process_worker._WindowsJob, "_active_process_count", lambda self: 1)
    started = time.monotonic()
    with pytest.raises(ProcessWorkerError, match="process tree cleanup") as caught:
        run_process_worker(
            _return, (None,), on_event=_ignore_event, cancelled=_never_cancelled, timeout=5
        )
    assert caught.value.error_type == "WorkerCleanupTimeout"
    assert not isinstance(caught.value, ProcessWorkerTimeout)
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name != "nt", reason="Windows job termination failures")
def test_job_termination_failure_is_propagated_and_job_handle_closed(monkeypatch):
    import ctypes

    job = process_worker._WindowsJob()

    def fail(handle, exit_code):
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(job.api, "TerminateJobObject", fail)
    with pytest.raises(OSError) as caught:
        job.close()
    assert caught.value.winerror == 5
    assert job.handle is None


@pytest.mark.parametrize(
    ("arguments", "returncode", "output"),
    [
        (["--multiprocessing-fork"], 0, ["freeze-support"]),
        (["--apply-update", "manifest.json"], 7, ["freeze-support", "update:manifest.json"]),
    ],
)
def test_entrypoint_dispatches_before_importing_qt(arguments, returncode, output):
    script = """
import importlib.abc
import multiprocessing
import runpy
import sys
import types

class RejectGui(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "choicer_voicer_pack_creator.app" or fullname.startswith("PySide6"):
            raise AssertionError("GUI import reached before worker/update dispatch")

sys.meta_path.insert(0, RejectGui())

def freeze_support():
    print("freeze-support")
    if sys.argv[1] == "--multiprocessing-fork":
        raise SystemExit(0)

multiprocessing.freeze_support = freeze_support
updates = types.ModuleType("choicer_voicer_pack_creator.updates")
updates.helper_main = lambda path: print("update:" + str(path)) or 7
sys.modules[updates.__name__] = updates
sys.argv = ["entry", *sys.argv[1:]]
runpy.run_module("choicer_voicer_pack_creator.__main__", run_name="__main__")
assert not any(name.startswith("PySide6") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == returncode, result.stderr
    assert result.stdout.splitlines() == output


@pytest.mark.skipif(os.name != "nt", reason="Standard-library Windows frozen dispatch")
def test_entrypoint_uses_the_real_windows_freeze_support_dispatch():
    script = """
import importlib.abc
import multiprocessing.spawn
import runpy
import sys

class RejectGui(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "choicer_voicer_pack_creator.app" or fullname.startswith("PySide6"):
            raise AssertionError("Frozen worker tried importing the GUI")

sys.meta_path.insert(0, RejectGui())
sys.frozen = True
sys.argv = ["app.exe", "--multiprocessing-fork", "pipe_handle=123", "parent_pid=456"]
multiprocessing.spawn.spawn_main = lambda **kwargs: print(sorted(kwargs.items()))
runpy.run_module("choicer_voicer_pack_creator.__main__", run_name="__main__")
raise AssertionError("freeze_support did not terminate the worker dispatch")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[('parent_pid', 456), ('pipe_handle', 123)]"
