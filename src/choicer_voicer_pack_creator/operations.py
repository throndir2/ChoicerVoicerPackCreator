"""Cooperative operation scopes and in-process asset leases, independent of Qt."""

from __future__ import annotations

import math
import os
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


class OperationCancelled(RuntimeError):
    """Cancellation observed; the operation must unwind its owned resources."""


class SourceChangedError(RuntimeError):
    """An input changed after the operation captured its source identity."""


_cancel: ContextVar[Callable[[], bool] | None] = ContextVar("operation_cancel", default=None)
_progress: ContextVar[Callable[[str, float | None], None] | None] = ContextVar(
    "operation_progress", default=None,
)
_critical: ContextVar[int] = ContextVar("operation_critical", default=0)
_owner: ContextVar[str | None] = ContextVar("operation_owner", default=None)
_committed: ContextVar[Callable[[], None] | None] = ContextVar("operation_committed", default=None)


def cancelled() -> bool:
    callback = _cancel.get()
    return not _critical.get() and callback is not None and callback()


def cancellation_deferred() -> bool:
    return bool(_critical.get())


def check_cancelled() -> None:
    if cancelled():
        raise OperationCancelled("Operation cancelled")


def report(message: str, fraction: float | None = None) -> None:
    if fraction is not None and (not math.isfinite(fraction) or not 0 <= fraction <= 1):
        raise ValueError("Progress fraction must be finite and between 0 and 1")
    callback = _progress.get()
    if callback is not None:
        callback(message, fraction)


@contextmanager
def operation_scope(
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str, float | None], None] | None = None,
    *,
    owner: str | None = None,
    committed: Callable[[], None] | None = None,
) -> Iterator[None]:
    previous_cancel = _cancel.get()
    callback = (
        (lambda: previous_cancel() or cancelled())
        if previous_cancel is not None and cancelled is not None else cancelled or previous_cancel
    )
    tokens = [
        (_cancel, _cancel.set(callback)),
        (_progress, _progress.set(progress if progress is not None else _progress.get())),
        (_owner, _owner.set(owner or _owner.get() or uuid.uuid4().hex)),
        (_committed, _committed.set(committed or _committed.get())),
    ]
    try:
        check_cancelled()
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@contextmanager
def critical_stage(message: str) -> Iterator[None]:
    """Finish publication/verification/rollback without cancellation midway.

    Entry is cancellable. Successful exit marks the operation committed: a late
    cancel request must not label a successfully published output as cancelled.
    Exceptions remain failures, and no cancellation check is made on exit.
    """
    check_cancelled()
    token = _critical.set(_critical.get() + 1)
    try:
        report(message, None)
        yield
        callback = _committed.get()
        if callback is not None:
            callback()
    finally:
        _critical.reset(token)


def canonical_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    key: str
    write: bool = True
    path: bool = False


def lease_requests(
    read_paths: Iterable[str | Path] = (),
    write_paths: Iterable[str | Path] = (),
    resource_keys: Iterable[str] = (),
) -> tuple[LeaseRequest, ...]:
    return tuple(
        [LeaseRequest(canonical_path(path), False, True) for path in read_paths]
        + [LeaseRequest(canonical_path(path), True, True) for path in write_paths]
        + [LeaseRequest(key) for key in resource_keys]
    )


def _conflicts(left: LeaseRequest, right: LeaseRequest) -> bool:
    if not (left.write or right.write) or left.path != right.path:
        return False
    if not left.path:
        return left.key == right.key
    return (
        left.key == right.key
        or left.key.startswith(right.key.rstrip(os.sep) + os.sep)
        or right.key.startswith(left.key.rstrip(os.sep) + os.sep)
    )


class LeasePool:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self._held: dict[str, tuple[str, tuple[LeaseRequest, ...]]] = {}

    def acquire(self, owner: str, requests: tuple[LeaseRequest, ...]) -> str | None:
        with self.condition:
            if any(
                other_owner != owner and _conflicts(request, held)
                for other_owner, existing in self._held.values()
                for request in requests for held in existing
            ):
                return None
            token = uuid.uuid4().hex
            self._held[token] = (owner, requests)
            return token

    def release(self, token: str) -> None:
        with self.condition:
            del self._held[token]
            self.condition.notify_all()


leases = LeasePool()


@contextmanager
def path_leases(
    read_paths: Iterable[str | Path] = (),
    write_paths: Iterable[str | Path] = (),
    *,
    resource_keys: Iterable[str] = (),
) -> Iterator[None]:
    requests = lease_requests(read_paths, write_paths, resource_keys)
    with operation_scope():
        owner = _owner.get()
        assert owner is not None
        waiting = False
        while True:
            check_cancelled()
            with leases.condition:
                token = leases.acquire(owner, requests)
                if token is not None:
                    break
                if not waiting:
                    report("Waiting for another task to release shared files or components...", None)
                    waiting = True
                leases.condition.wait(0.1)
        try:
            yield
        finally:
            leases.release(token)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Identity policy, not a media copy; coordinated writes require asset leases.

    Stat identities detect replacement/ordinary external edits, not hostile edits
    which deliberately restore timestamps. Directory inventories include contents.
    """

    roots: tuple[str, ...]
    entries: tuple[tuple[str, int, int, int, int, int], ...]

    @classmethod
    def capture(cls, paths: Iterable[str | Path]) -> SourceSnapshot:
        roots = tuple(sorted({canonical_path(path) for path in paths}))
        entries = []
        seen: set[Path] = set()
        for root in roots:
            path = Path(root)
            candidates = [path]
            if path.is_dir():
                candidates.extend(path.rglob("*"))
            for candidate in sorted(candidates):
                check_cancelled()
                if candidate in seen:
                    continue
                seen.add(candidate)
                value = candidate.stat()
                entries.append((
                    canonical_path(candidate), value.st_dev, value.st_ino,
                    value.st_size, value.st_mtime_ns, value.st_mode,
                ))
        return cls(roots, tuple(entries))

    def verify(self) -> None:
        try:
            current = self.capture(self.roots)
        except OSError as error:
            raise SourceChangedError("Source assets disappeared or became unreadable") from error
        if self != current:
            raise SourceChangedError("Source assets changed during processing; retry with fresh inputs")


def freeze_metadata(value: Any) -> Any:
    """Detach and freeze JSON-like provenance; reject mutable/custom objects."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Snapshot metadata keys must be strings")
        return MappingProxyType({key: freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_metadata(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported snapshot metadata value: {type(value).__name__}")
