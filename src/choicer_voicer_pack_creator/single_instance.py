from __future__ import annotations

import hashlib
import json
import os
import struct
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PATHS = 128
MAX_PATH_LENGTH = 32768
MAX_CONNECTIONS = 16
MAX_PENDING_REQUESTS = 32
FORWARD_TIMEOUT_MS = 2000
_HEADER = struct.Struct("!I")


class SingleInstanceError(RuntimeError):
    pass


def server_name(data_root: Path) -> str:
    root = os.path.normcase(str(data_root.resolve()))
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:32]
    return f"choicer-voicer-{digest}"


def _validated_paths(values: object) -> list[Path]:
    if not isinstance(values, list) or len(values) > MAX_PATHS:
        raise SingleInstanceError(f"An open request must contain at most {MAX_PATHS} paths.")
    result = []
    for value in values:
        if (
            not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH
            or "\0" in value or not Path(value).is_absolute()
        ):
            raise SingleInstanceError("Open requests require absolute local file paths.")
        result.append(Path(value))
    return result


def encode_request(paths: Sequence[Path], owner_pid: int) -> bytes:
    values = [str(path) for path in paths]
    _validated_paths(values)
    if type(owner_pid) is not int or owner_pid <= 0:
        raise SingleInstanceError("The existing editor's lock owner is unavailable.")
    payload = json.dumps(
        {"version": 1, "owner_pid": owner_pid, "paths": values}, ensure_ascii=True,
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise SingleInstanceError("The open request is too large.")
    return _HEADER.pack(len(payload)) + payload


def decode_request(payload: bytes, owner_pid: int) -> list[Path]:
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise SingleInstanceError("The open request is empty or too large.")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SingleInstanceError("The open request is not valid JSON.") from error
    if (
        not isinstance(value, dict) or set(value) != {"version", "owner_pid", "paths"}
        or type(value["version"]) is not int or value["version"] != 1
        or type(value["owner_pid"]) is not int or value["owner_pid"] != owner_pid
    ):
        raise SingleInstanceError("The open request does not match this editor's lock owner.")
    return _validated_paths(value["paths"])


class SingleInstance(QObject):
    """One lock owner per data root, with bounded, same-user local open requests."""

    _delivery_requested = Signal()

    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.name = server_name(data_root)
        self.lock = QLockFile(str(data_root / "application-instance.lock"))
        # A healthy editor may keep the lock for days; elapsed time is not staleness.
        self.lock.setStaleLockTime(0)
        self.server: QLocalServer | None = None
        self._connections: dict[QLocalSocket, tuple[bytearray, QTimer]] = {}
        self._pending: deque[list[Path]] = deque()
        self._open_paths: Callable[[list[Path]], None] | None = None
        self._delivery_requested.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def try_acquire(self) -> bool:
        if self.lock.tryLock(0):
            return True
        if self.lock.error() != QLockFile.LockError.LockFailedError:
            raise SingleInstanceError("Could not create the editor's application-data lock.")
        return False

    def listen(self) -> None:
        if not self.lock.isLocked():
            raise SingleInstanceError("Only the existing lock owner can listen for open requests.")
        if self.server is not None:
            return
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.setMaxPendingConnections(MAX_CONNECTIONS)
        # Only a process holding the lock may remove an endpoint left by a crashed editor.
        QLocalServer.removeServer(self.name)
        if not server.listen(self.name):
            error = server.errorString()
            server.deleteLater()
            raise SingleInstanceError(f"Could not listen for local open requests: {error}")
        self.server = server
        server.newConnection.connect(self._accept_connections)

    def set_open_handler(self, callback: Callable[[list[Path]], None]) -> None:
        self._open_paths = callback
        self._delivery_requested.emit()

    @Slot()
    def _deliver(self) -> None:
        if self._open_paths is None or not self._pending:
            return
        self._open_paths(self._pending.popleft())
        if self._pending:
            self._delivery_requested.emit()

    @Slot()
    def _accept_connections(self) -> None:
        if self.server is None:
            return
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self._connections) >= MAX_CONNECTIONS:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(MAX_PAYLOAD_BYTES + _HEADER.size + 1)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda socket=socket: self._reject(socket))
            self._connections[socket] = (bytearray(), timer)
            socket.readyRead.connect(lambda socket=socket: self._read_request(socket))
            socket.disconnected.connect(lambda socket=socket: self._forget(socket))
            timer.start(FORWARD_TIMEOUT_MS)
            self._read_request(socket)

    def _forget(self, socket: QLocalSocket) -> None:
        connection = self._connections.pop(socket, None)
        if connection is not None:
            connection[1].stop()
            socket.deleteLater()

    def _reject(self, socket: QLocalSocket) -> None:
        socket.abort()
        self._forget(socket)

    def _read_request(self, socket: QLocalSocket) -> None:
        connection = self._connections.get(socket)
        if connection is None:
            return
        buffer, timer = connection
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) < _HEADER.size:
            return
        size = _HEADER.unpack_from(buffer)[0]
        if not 0 < size <= MAX_PAYLOAD_BYTES or len(buffer) > size + _HEADER.size:
            self._reject(socket)
            return
        if len(buffer) < size + _HEADER.size:
            return
        try:
            paths = decode_request(bytes(buffer[_HEADER.size:]), os.getpid())
        except SingleInstanceError:
            self._reject(socket)
            return
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            self._reject(socket)
            return
        timer.stop()
        self._pending.append(paths)
        self._delivery_requested.emit()
        socket.readyRead.disconnect()
        socket.write(f"OK {os.getpid()}\n".encode("ascii"))
        socket.disconnectFromServer()
        if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self._forget(socket)

    def forward_paths(self, paths: Sequence[Path], *, timeout_ms: int = FORWARD_TIMEOUT_MS) -> None:
        """Used only by the secondary process, before it creates an editor window."""
        pid, _host, _application = self.lock.getLockInfo()
        if pid <= 0 or self.lock.isLocked():
            raise SingleInstanceError("The existing editor's lock owner is unavailable.")
        request = encode_request(paths, pid)
        deadline = time.monotonic() + max(0, timeout_ms) / 1000

        def remaining() -> int:
            return max(0, int((deadline - time.monotonic()) * 1000))

        socket = QLocalSocket()
        socket.setReadBufferSize(64)
        try:
            # The first process can still be between acquiring its lock and starting Qt.
            while remaining():
                current_pid, *_ = self.lock.getLockInfo()
                if current_pid != pid:
                    raise SingleInstanceError("The existing editor closed before accepting files.")
                socket.connectToServer(self.name)
                if socket.waitForConnected(min(100, remaining())):
                    break
                socket.abort()
                time.sleep(min(0.025, remaining() / 1000))
            else:
                raise SingleInstanceError(
                    "The existing editor did not respond. No additional editor was started."
                )
            if socket.write(request) != len(request):
                raise SingleInstanceError("Could not send the complete open request.")
            while socket.bytesToWrite():
                if not remaining() or not socket.waitForBytesWritten(remaining()):
                    raise SingleInstanceError("The existing editor did not receive the open request.")
            response = bytearray()
            expected = f"OK {pid}\n".encode("ascii")
            while len(response) < len(expected):
                response.extend(bytes(socket.readAll()))
                if len(response) >= len(expected):
                    break
                if not remaining() or not socket.waitForReadyRead(remaining()):
                    raise SingleInstanceError(
                        "The existing editor did not confirm the open request. Check its window."
                    )
            if response != expected:
                raise SingleInstanceError("The existing editor rejected the open request.")
        finally:
            socket.abort()

    def close(self) -> None:
        for socket in list(self._connections):
            self._reject(socket)
        self._pending.clear()
        self._open_paths = None
        if self.server is not None:
            self.server.close()
            self.server.deleteLater()
            self.server = None
        if self.lock.isLocked():
            self.lock.unlock()
