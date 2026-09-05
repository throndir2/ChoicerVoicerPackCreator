"""Supervise blocking, trusted Python work without blocking the calling thread's polling.

Targets must be importable, pickleable callables: workers always use ``spawn``, including
in source installations. The child calls ``target(emit, *args)``. Only events which the
parent's ``on_event`` accepts as useful progress renew the idle deadline.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import select
import signal
import socket
import struct
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import ExitStack, suppress
from typing import Any

_POLL_INTERVAL = 0.1
_HEADER = struct.Struct("!Q")
_READ_SIZE = 64 * 1024
_CLEANUP_TIMEOUT = 5.0


class ProcessWorkerError(RuntimeError):
    """A child failure, with its original exception type and traceback."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        remote_traceback: str = "",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type or type(self).__name__
        self.remote_traceback = remote_traceback


class ProcessWorkerCancelled(ProcessWorkerError):
    """The caller cancelled the operation."""


class ProcessWorkerTimeout(ProcessWorkerError):
    """The absolute or useful-progress deadline expired."""


def _encode_message(message: Any) -> bytes:
    payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    return _HEADER.pack(len(payload)) + payload


def _read_task(connection: socket.socket) -> Any:
    def read_exactly(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = connection.recv(min(_READ_SIZE, size - len(result)))
            if not chunk:
                raise EOFError("Parent closed the worker startup gate")
            result.extend(chunk)
        return bytes(result)

    (size,) = _HEADER.unpack(read_exactly(_HEADER.size))
    return pickle.loads(read_exactly(size))


def _worker_main(connection: socket.socket) -> None:
    lock = threading.Lock()

    def send(message: Any) -> None:
        with lock:
            connection.sendall(_encode_message(message))

    def emit(event: str, details: dict) -> None:
        send(("event", event, details))

    try:
        connection.set_inheritable(False)
        if os.name != "nt":
            os.setsid()
        send(("ready",))
        # The parent sends the task only after installing process-tree containment.
        # Even importing/unpickling the target therefore happens inside that boundary.
        target, args = _read_task(connection)
        result = target(emit, *args)
        send(("result", result))
    except BaseException as error:
        send(("error", str(error), type(error).__name__, traceback.format_exc()))
    finally:
        connection.close()


def _windows_worker_entry(startup, worker_main: Callable[[socket.socket], None]) -> None:
    # Passing a socket directly to spawn on Windows starts a persistent multiprocessing
    # resource-sharer thread in the parent. A spawn-duplicated pipe and explicit socket
    # sharing avoid that thread; only this small, fixed-size descriptor uses the pipe.
    try:
        descriptor = startup.recv_bytes()
    finally:
        startup.close()
    with socket.fromshare(descriptor) as connection:
        worker_main(connection)


class _MessageReader:
    """Incremental framing; readiness never implies a whole pickle is available.

    Queue.get(timeout) and Connection.poll followed by recv can still block forever on
    an incomplete large message. This nonblocking socket reader never waits for the
    rest of a frame, and has no receiver/feeder thread to strand during cancellation.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.size: int | None = None

    def pop(self) -> Any:
        if self.size is None and len(self.buffer) >= _HEADER.size:
            (self.size,) = _HEADER.unpack_from(self.buffer)
            del self.buffer[: _HEADER.size]
        if self.size is None or len(self.buffer) < self.size:
            return None
        payload = self.buffer[: self.size]
        del self.buffer[: self.size]
        self.size = None
        return pickle.loads(payload)


class _WindowsJob:
    """A private, non-inheritable Windows job which also owns worker descendants."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self.accounting_type = BasicAccounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, restype, argtypes in (
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            (
                "SetInformationJobObject",
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            ),
            ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            (
                "QueryInformationJobObject",
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            ),
            ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            ("WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
            (
                "IsProcessInJob",
                wintypes.BOOL,
                [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
            ),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
        ):
            function = getattr(self.api, name)
            function.restype = restype
            function.argtypes = argtypes
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        self.limits = limits
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            try:
                raise error
            finally:
                self.close()

    def assign(self, pid: int) -> None:
        import ctypes

        handle = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if not self.api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def _close_handle(self, handle: int) -> None:
        import ctypes

        if not self.api.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessWorkerError(
                "Timed out waiting for worker process tree cleanup",
                error_type="WorkerCleanupTimeout",
            )
        return remaining

    def _process_ids(self, deadline: float) -> list[int]:
        import ctypes
        from ctypes import wintypes

        capacity = 16
        while True:
            self._remaining(deadline)

            class ProcessIds(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            info = ProcessIds()
            success = self.api.QueryInformationJobObject(
                self.handle, 3, ctypes.byref(info), ctypes.sizeof(info), None
            )
            if success and info.NumberOfProcessIdsInList >= info.NumberOfAssignedProcesses:
                return list(info.ProcessIdList[: info.NumberOfProcessIdsInList])
            error = ctypes.get_last_error()
            if not success and error != 234:  # ERROR_MORE_DATA: resize the snapshot.
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, info.NumberOfAssignedProcesses)

    def _active_process_count(self) -> int:
        import ctypes

        info = self.accounting_type()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses

    def close(self) -> None:
        import ctypes
        from ctypes import wintypes

        if not self.handle:
            return
        deadline = time.monotonic() + _CLEANUP_TIMEOUT
        try:
            # Disallow further child creation before taking ownership of process handles.
            # Lowering the limit doesn't terminate existing members; any live member
            # already occupies the one allowed slot, closing the snapshot/spawn race.
            self.limits.BasicLimitInformation.LimitFlags |= 0x0008
            self.limits.BasicLimitInformation.ActiveProcessLimit = 1
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(self.limits), ctypes.sizeof(self.limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            with ExitStack() as resources:
                processes = {}
                while True:
                    # Retain handles BEFORE termination: job accounting can reach zero
                    # before process handle/I/O rundown has released descendant file locks.
                    for pid in self._process_ids(deadline):
                        if pid in processes:
                            continue
                        handle = self.api.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
                        if not handle:
                            error = ctypes.get_last_error()
                            if error == 87:  # Already exited between snapshot and OpenProcess.
                                continue
                            raise ctypes.WinError(error)
                        resources.callback(self._close_handle, handle)
                        belongs = wintypes.BOOL()
                        if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(belongs)):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if belongs.value:
                            processes[pid] = handle
                    if not self.api.TerminateJobObject(self.handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                    for handle in processes.values():
                        milliseconds = max(1, int(self._remaining(deadline) * 1000))
                        status = self.api.WaitForSingleObject(handle, milliseconds)
                        if status == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        if status != 0:
                            raise ProcessWorkerError(
                                "Timed out waiting for worker process tree cleanup",
                                error_type="WorkerCleanupTimeout",
                            )
                    if self._active_process_count() == 0:
                        break
                    # A descendant may have spawned another process during the snapshot.
                    time.sleep(min(0.01, self._remaining(deadline)))
        finally:
            # Kill-on-close remains a fallback even when querying/waiting fails.
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def _reap_process(process: multiprocessing.Process) -> None:
    if process.pid is not None:
        if process.is_alive():
            process.kill()
        process.join()


def _kill_group(pid: int) -> None:
    # A worker without surviving descendants may already have left the group empty.
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def run_process_worker(
    target: Callable[..., Any],
    args: tuple,
    *,
    on_event: Callable[[str, dict], bool],
    cancelled: Callable[[], bool],
    timeout: float | None = None,
    idle_timeout: float | Callable[[], float] | None = None,
    waiting: Callable[[float], None] | None = None,
) -> Any:
    """Run a trusted target in a spawned, forcibly cancellable process tree.

    ``timeout`` bounds the entire lifetime, including startup. ``idle_timeout`` can be
    a callable, evaluated each poll, so accepted progress can select a longer (but
    still bounded) deadline for a merge. ``waiting`` runs about once per second with
    elapsed seconds. Parent callbacks run synchronously and must themselves be quick.
    On every exit the worker is reaped and its owned descendants are terminated.
    """
    started = last_progress = time.monotonic()
    next_wait = started + 1.0

    def check_limits() -> float:
        if cancelled():
            raise ProcessWorkerCancelled("Process worker cancelled")
        now = time.monotonic()
        if timeout is not None and now - started >= timeout:
            raise ProcessWorkerTimeout(f"Process worker exceeded its {timeout:g}s time limit")
        idle_limit = idle_timeout() if callable(idle_timeout) else idle_timeout
        if idle_limit is not None and now - last_progress >= idle_limit:
            raise ProcessWorkerTimeout(
                f"Process worker made no useful progress for {idle_limit:g}s"
            )
        return now

    check_limits()
    # Serialize before launch: a bad target/argument must not leave a half-started child.
    task = memoryview(_encode_message((target, args)))
    with ExitStack() as resources:
        parent, child = socket.socketpair()
        resources.enter_context(parent)
        resources.enter_context(child)
        parent.setblocking(False)
        context = multiprocessing.get_context("spawn")
        startup_reader = startup_writer = None
        if os.name == "nt":
            startup_reader, startup_writer = context.Pipe(duplex=False)
            resources.enter_context(startup_reader)
            resources.enter_context(startup_writer)
        process = context.Process(
            target=_windows_worker_entry if startup_reader is not None else _worker_main,
            args=(startup_reader, _worker_main) if startup_reader is not None else (child,),
            name="isolated-process-worker",
        )
        resources.callback(process.close)
        resources.callback(_reap_process, process)
        job = _WindowsJob() if os.name == "nt" else None
        if job is not None:
            resources.callback(job.close)
        process.start()
        if job is not None:
            job.assign(process.pid)
        if startup_writer is not None:
            startup_reader.close()
            startup_writer.send_bytes(child.share(process.pid))
            startup_writer.close()
        child.close()
        reader = _MessageReader()
        ready = eof = False

        while True:
            now = check_limits()
            if waiting is not None and now >= next_wait:
                waiting(now - started)
                next_wait = now + 1.0
                continue

            message = reader.pop()
            if message is not None:
                kind = message[0]
                if kind == "ready":
                    if ready:
                        raise ProcessWorkerError("Worker sent a duplicate startup message")
                    ready = True
                    if job is None:
                        resources.callback(_kill_group, process.pid)
                elif kind == "event":
                    if on_event(message[1], message[2]):
                        last_progress = time.monotonic()
                elif kind == "result":
                    check_limits()  # Cancellation wins a race with a completed result.
                    result = message[1]
                    break
                elif kind == "error":
                    raise ProcessWorkerError(
                        message[1], error_type=message[2], remote_traceback=message[3]
                    )
                else:
                    raise ProcessWorkerError(f"Unknown worker message: {kind!r}")
                continue

            if eof:
                raise ProcessWorkerError(
                    "Process worker exited without returning a result",
                    error_type="WorkerExited",
                )
            readable, writable, _ = select.select(
                [parent], [parent] if ready and task else [], [], _POLL_INTERVAL
            )
            if writable:
                try:
                    task = task[parent.send(task[:_READ_SIZE]) :]
                except BlockingIOError:
                    pass
                except ConnectionError as error:
                    raise ProcessWorkerError(
                        "Process worker exited without returning a result",
                        error_type="WorkerExited",
                    ) from error
            if readable:
                try:
                    chunk = parent.recv(_READ_SIZE)
                except BlockingIOError:
                    continue
                except ConnectionError as error:
                    raise ProcessWorkerError(
                        "Process worker exited without returning a result",
                        error_type="WorkerExited",
                    ) from error
                if chunk:
                    reader.buffer.extend(chunk)
                else:
                    eof = True
            elif process.exitcode is not None:
                raise ProcessWorkerError(
                    f"Process worker exited with code {process.exitcode} without returning a result",
                    error_type="WorkerExited",
                )
    check_limits()  # Include cancellation requested while reaping the process tree.
    return result
