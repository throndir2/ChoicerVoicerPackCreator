from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    SourceSnapshot,
    check_cancelled,
    operation_scope,
    path_leases,
)


def test_source_snapshot_detects_replacement_and_directory_inventory(tmp_path):
    path = tmp_path / "media"
    path.write_bytes(b"one")
    file_snapshot = SourceSnapshot.capture([path])
    folder_snapshot = SourceSnapshot.capture([tmp_path])
    file_snapshot.verify()
    path.write_bytes(b"changed")
    with pytest.raises(SourceChangedError):
        file_snapshot.verify()
    with pytest.raises(SourceChangedError):
        folder_snapshot.verify()


def test_cancelled_waiter_does_not_release_owner_lease(tmp_path):
    started = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    waited = threading.Event()

    def owner():
        with path_leases(write_paths=[tmp_path]):
            started.set()
            assert release.wait(5)

    def waiter():
        with (
            operation_scope(cancel.is_set, lambda message, fraction: waited.set()),
            path_leases(write_paths=[tmp_path / "model"]),
        ):
            pytest.fail("Waiter acquired active lease")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner)
        assert started.wait(3)
        second = pool.submit(waiter)
        assert waited.wait(3)
        cancel.set()
        try:
            with pytest.raises(OperationCancelled):
                second.result(timeout=3)
        finally:
            release.set()
        first.result(timeout=3)


def test_nested_scope_combines_cancellation():
    with (
        operation_scope(lambda: False),
        pytest.raises(OperationCancelled),
        operation_scope(lambda: True),
    ):
        check_cancelled()
