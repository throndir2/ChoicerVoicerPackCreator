from __future__ import annotations

import json
import os
import struct
import subprocess
import sys

import pytest
from PySide6.QtCore import QThread
from PySide6.QtNetwork import QLocalSocket

from choicer_voicer_pack_creator import single_instance
from choicer_voicer_pack_creator.single_instance import (
    MAX_PATHS,
    MAX_PAYLOAD_BYTES,
    SingleInstance,
    SingleInstanceError,
    decode_request,
    encode_request,
    server_name,
)


@pytest.fixture
def primary(qapp, tmp_path):
    instance = SingleInstance(tmp_path)
    assert instance.try_acquire()
    instance.listen()
    yield instance
    instance.close()


def test_namespace_is_canonical_and_data_root_specific(tmp_path):
    assert server_name(tmp_path) == server_name(tmp_path / "unused" / "..")
    assert server_name(tmp_path) != server_name(tmp_path / "isolated")
    assert len(server_name(tmp_path)) < 80


def test_request_round_trip_keeps_all_paths_as_data(tmp_path):
    paths = [
        tmp_path / "one.cvpack.json",
        tmp_path / "日本語 project.cvpack.json",
        tmp_path / "not-a-command; echo untrusted",
    ]
    frame = encode_request(paths, os.getpid())
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    assert decode_request(frame[4:], os.getpid()) == paths
    assert decode_request(encode_request([], os.getpid())[4:], os.getpid()) == []


@pytest.mark.parametrize("payload", [
    b"", b"\xff", b"not JSON", b"[]", b"null",
    b'{"version":1,"owner_pid":42,"paths":[],"command":"run"}',
    b'{"version":true,"owner_pid":42,"paths":[]}',
    b'{"version":2,"owner_pid":42,"paths":[]}',
    b'{"version":1,"owner_pid":43,"paths":[]}',
    b'{"version":1,"owner_pid":true,"paths":[]}',
    b'{"version":1,"owner_pid":42,"paths":"path"}',
    b'{"version":1,"owner_pid":42,"paths":[null]}',
    b'{"version":1,"owner_pid":42,"paths":["relative.cvpack.json"]}',
    b'{"version":1,"owner_pid":42,"paths":["https://example.com/project"]}',
    b"[" * 2000,
])
def test_malformed_or_wrong_owner_requests_are_rejected(payload):
    with pytest.raises(SingleInstanceError):
        decode_request(payload, 42)


def test_payload_and_path_limits_are_enforced_before_opening(tmp_path):
    with pytest.raises(SingleInstanceError, match="at most"):
        encode_request([tmp_path / "file"] * (MAX_PATHS + 1), 42)
    with pytest.raises(SingleInstanceError, match="absolute"):
        encode_request([tmp_path / ("x" * 32769)], 42)
    with pytest.raises(SingleInstanceError, match="absolute"):
        encode_request([tmp_path / "nul\0path"], 42)
    with pytest.raises(SingleInstanceError, match="too large"):
        encode_request([tmp_path / ("x" * 4096)] * MAX_PATHS, 42)
    with pytest.raises(SingleInstanceError, match="too large"):
        decode_request(b" " * (MAX_PAYLOAD_BYTES + 1), 42)


def test_only_lock_owner_can_listen_and_secondary_close_preserves_owner(primary, tmp_path):
    secondary = SingleInstance(tmp_path)
    try:
        assert primary.lock.staleLockTime() == 0
        assert not secondary.try_acquire()
        with pytest.raises(SingleInstanceError, match="lock owner"):
            secondary.listen()
        secondary.close()
        assert primary.server.isListening()
        assert not secondary.try_acquire()
        primary.close()
        assert secondary.try_acquire()
        secondary.listen()
    finally:
        secondary.close()


def test_different_roots_can_run_independent_editors(primary, tmp_path):
    root = tmp_path / "visible-validation"
    root.mkdir()
    isolated = SingleInstance(root)
    try:
        assert isolated.try_acquire()
        isolated.listen()
        assert isolated.name != primary.name
        assert isolated.server.isListening() and primary.server.isListening()
    finally:
        isolated.close()


@pytest.mark.parametrize("empty", [False, True], ids=["all-paths", "activate-workspace"])
def test_another_process_forwards_to_gui_thread(qtbot, primary, tmp_path, empty):
    paths = [] if empty else [
        tmp_path / "first.cvpack.json", tmp_path / "second project.cvpack.json",
        tmp_path / "日本語.cvpack.json",
    ]
    received = []

    def deliver(values):
        assert QThread.currentThread() == primary.thread()
        received.append(values)

    primary.set_open_handler(deliver)
    code = """
import json, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from choicer_voicer_pack_creator.single_instance import SingleInstance
application = QCoreApplication([])
secondary = SingleInstance(Path(sys.argv[1]))
assert not secondary.try_acquire()
secondary.forward_paths([Path(value) for value in json.loads(sys.argv[2])])
secondary.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), json.dumps([str(path) for path in paths])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        qtbot.waitUntil(lambda: process.poll() is not None, timeout=10000)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr
        qtbot.waitUntil(lambda: bool(received))
        assert received == [paths]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_partial_frame_waits_for_completion_and_handler_delivery_is_queued(qtbot, primary, tmp_path):
    socket = QLocalSocket()
    path = tmp_path / "first.cvpack.json"
    frame = encode_request([path], os.getpid())
    received = []
    try:
        socket.connectToServer(primary.name)
        qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        socket.write(frame[:6])
        socket.flush()
        qtbot.waitUntil(lambda: bool(primary._connections))
        assert not primary._pending
        socket.write(frame[6:])
        socket.flush()
        qtbot.waitUntil(lambda: bool(primary._pending))
        primary.set_open_handler(received.append)
        assert received == []
        qtbot.waitUntil(lambda: bool(received))
        assert received == [[path]]
    finally:
        socket.abort()


@pytest.mark.parametrize("frame", [
    struct.pack("!I", MAX_PAYLOAD_BYTES + 1),
    struct.pack("!I", 0),
    struct.pack("!I", 1) + b"{}",
    struct.pack("!I", 2) + b"{}",
])
def test_invalid_frames_are_disconnected_without_opening(qtbot, primary, frame):
    received = []
    primary.set_open_handler(received.append)
    socket = QLocalSocket()
    try:
        socket.connectToServer(primary.name)
        qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        socket.write(frame)
        socket.flush()
        qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        assert not received
        assert not primary._pending
        assert primary.server.isListening()
    finally:
        socket.abort()


def test_incomplete_connection_times_out_without_blocking_gui(qtbot, primary, monkeypatch):
    monkeypatch.setattr(single_instance, "FORWARD_TIMEOUT_MS", 30)
    socket = QLocalSocket()
    try:
        socket.connectToServer(primary.name)
        qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        socket.write(b"\0")
        socket.flush()
        qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        assert primary.server.isListening()
    finally:
        socket.abort()


def test_failed_connection_is_bounded_and_never_steals_lock(qapp, tmp_path):
    primary = SingleInstance(tmp_path)
    secondary = SingleInstance(tmp_path)
    try:
        assert primary.try_acquire()
        assert not secondary.try_acquire()
        with pytest.raises(SingleInstanceError, match="did not respond"):
            secondary.forward_paths([], timeout_ms=30)
        assert primary.lock.isLocked()
    finally:
        secondary.close()
        primary.close()
