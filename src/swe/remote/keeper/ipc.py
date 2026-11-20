"""Keeper IPC: the Unix socket a hub talks to one keeper over.

One socket per keeper, under a private per-user folder (``/tmp/swe-<uid>/``,
mode 0700, owner checked on both ends; a path under HOME can pass the
104-byte limit macOS puts on a socket address). Frames are one JSON
object per line, like ``swe.remote.acp``.

The conversation, at version 1::

    client  {"t": "hello", "v": 1, "min_v": 1, "last_seq": 41, "client": "hub"}
    keeper  {"t": "welcome", "v": 1, "last_seq": 57, "pending": [...],
             "live": [...], ...}            the state the client joins at
    keeper  {"t": "gap", ...}               only when history was dropped
    keeper  {"t": "event", "e": {...}}      replay from disk: 42 .. 57
    keeper  {"t": "live", "last_seq": 57}   replay is over
    keeper  {"t": "event", "e": {...}}      58 .. as they happen, and deltas

    client  {"t": "cmd", "id": 7, "op": "send", "command_id": "c-1", ...}
    keeper  {"t": "reply", "id": 7, "status": "accepted", ...}

``last_seq`` in the hello is the last seq the client holds: 0 replays
everything, null skips the replay. A keeper that cannot speak any
version the client offers answers ``{"t": "refused", "reason":
"version", ...}`` and closes. Both sides ignore frame types and fields
they do not know, so a newer peer can add them without a version bump;
a bump is for a change an older peer would misread.

``docs/specs/G5-keeper-process.md`` has every frame and op.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import stat
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Callable

# The newest IPC version this code speaks, and the oldest it still
# accepts from a peer.
VERSION = 1
MIN_VERSION = 1

# One frame's size limit. Events carry tool output, so this is generous;
# a longer line is a broken or hostile peer.
MAX_LINE = 16 << 20

# Overrides the socket folder (tests, and a machine with no usable /tmp).
RUNTIME_ENV = "SWE_RUNTIME_DIR"

# sun_path is 104 bytes on macOS and 108 on Linux, NUL included.
SOCKET_PATH_MAX = 103

HELLO_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 90.0
# How long one write may block before the peer counts as stuck.
SEND_TIMEOUT_S = 10.0


class IpcError(Exception):
    """The socket could not be used: gone, unsafe, or the peer broke
    the framing."""


class Unsafe(IpcError):
    """A socket or its folder is not private to this user."""


class Refused(IpcError):
    """The keeper turned the handshake down. ``reason`` is its code
    (``version``, ``bad_hello``...); ``frame`` is what it sent."""

    def __init__(self, reason: str, frame: dict) -> None:
        super().__init__(f"the keeper refused the connection: {reason}")
        self.reason = reason
        self.frame = frame


# ---------------------------------------------------------------------
# Where sockets live


def runtime_dir(base: Path | str | None = None) -> Path:
    """The folder keeper sockets live in: ``base``, else
    ``$SWE_RUNTIME_DIR``, else ``/tmp/swe-<uid>``."""
    if base is not None:
        return Path(base)
    env = os.environ.get(RUNTIME_ENV)
    if env:
        return Path(env)
    return Path(f"/tmp/swe-{os.getuid()}")


def check_private_dir(path: Path) -> None:
    """Raise :class:`Unsafe` unless ``path`` is a real folder (not a
    link) owned by this user that nobody else can enter."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise Unsafe(f"{path}: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise Unsafe(f"{path} is not a folder")
    if st.st_uid != os.getuid():
        raise Unsafe(f"{path} belongs to another user")
    if st.st_mode & 0o077:
        raise Unsafe(f"{path} is open to other users "
                     f"(mode {stat.S_IMODE(st.st_mode):o})")


def ensure_runtime_dir(base: Path | str | None = None) -> Path:
    """Create the socket folder 0700 if it is missing, and check it."""
    path = runtime_dir(base)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise Unsafe(f"{path}: {exc.strerror or exc}") from exc
    check_private_dir(path)
    return path


def socket_path(keeper_dir: Path | str,
                base: Path | str | None = None) -> Path:
    """The socket of the keeper whose folder is ``keeper_dir``. The
    name is a hash of that folder, so two homes on one machine (a test
    home beside the real one) never share a socket."""
    digest = hashlib.sha1(
        os.path.abspath(keeper_dir).encode("utf-8", "surrogateescape")
    ).hexdigest()[:24]
    path = runtime_dir(base) / f"k-{digest}.sock"
    if len(os.fsencode(path)) > SOCKET_PATH_MAX:
        raise Unsafe(f"socket path is too long for a Unix socket: {path}")
    return path


def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process on the other end, or None when this OS
    will not say."""
    try:
        if sys.platform.startswith("linux"):
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
            return struct.unpack("3i", creds)[1]
        if sys.platform == "darwin":
            # LOCAL_PEERCRED at SOL_LOCAL: struct xucred, whose second
            # field is the uid.
            creds = sock.getsockopt(0, 0x001, 76)
            return struct.unpack_from("II", creds)[1]
    except (OSError, struct.error):
        return None
    return None


# ---------------------------------------------------------------------
# Framing


def encode(frame: dict) -> bytes:
    """One frame as a line of JSON. A text with a lone surrogate (which
    UTF-8 cannot carry) goes out as escaped ASCII."""
    try:
        return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
                + "\n").encode("utf-8")
    except UnicodeEncodeError:
        return (json.dumps(frame, separators=(",", ":")) + "\n").encode(
            "ascii")


def negotiate(client_v: Any, client_min: Any, keeper_v: int = VERSION,
              keeper_min: int = MIN_VERSION) -> int | None:
    """The version both ends speak: the highest one inside both
    ``[min, v]`` ranges, or None when the ranges do not meet."""
    if not _is_version(client_v):
        return None
    if client_min is None:
        client_min = client_v
    if not _is_version(client_min) or client_min > client_v:
        return None
    version = min(client_v, keeper_v)
    return version if version >= max(client_min, keeper_min) else None


def _is_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 1


class LineReader:
    """Reads newline-terminated JSON frames off a socket.

    :meth:`read` returns the next frame, None at end of stream, and
    raises :class:`IpcError` for a line that is too long or is not a
    JSON object. ``timeout`` (seconds, or None to wait) bounds one
    read; it raises ``TimeoutError``.
    """

    def __init__(self, sock: socket.socket, max_line: int = MAX_LINE) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._max = max_line
        self._eof = False

    def read(self, timeout: float | None = None) -> dict | None:
        while True:
            nl = self._buf.find(b"\n")
            if nl > self._max:
                # However the bytes arrived: one read can carry a line
                # past the limit together with its newline.
                raise IpcError("a frame passed the size limit")
            if nl >= 0:
                raw = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                if not raw.strip():
                    continue
                return _decode(raw)
            if self._eof:
                return None
            if len(self._buf) > self._max:
                raise IpcError("a frame passed the size limit")
            try:
                self._sock.settimeout(timeout)
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError("no frame arrived in time") from exc
            except OSError:
                chunk = b""  # closed under us: the end of the stream
            finally:
                try:
                    self._sock.settimeout(None)
                except OSError:
                    pass
            if not chunk:
                self._eof = True  # a partial last line is dropped
                continue
            self._buf += chunk


def _decode(raw: bytes) -> dict:
    try:
        frame = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise IpcError("a frame was not JSON") from exc
    if not isinstance(frame, dict):
        raise IpcError("a frame was not a JSON object")
    return frame


def send_frame(sock: socket.socket, frame: dict) -> None:
    """Write one frame; raises OSError when the peer is gone or stuck
    past the socket's send timeout."""
    sock.sendall(encode(frame))


def set_send_timeout(sock: socket.socket, seconds: float) -> None:
    """Bound every send on ``sock`` without touching its receives."""
    whole = int(seconds)
    micro = int((seconds - whole) * 1_000_000)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                    struct.pack("ll", whole, micro))


# ---------------------------------------------------------------------
# The client (the hub's end)


class KeeperClient:
    """One connection to one keeper.

    ``connect`` does the handshake and leaves the keeper's answer in
    ``welcome``. After it, replies are matched to :meth:`request`
    calls, and every other frame (``event``, ``gap``, ``live``,
    ``bye``) goes to ``on_frame`` when one was given, else onto a queue
    read with :meth:`next`. A closed connection ends with a
    ``{"t": "closed"}`` frame the client makes itself.

    ``last_seq`` is the last seq this client already holds (0: replay
    everything; None: no replay).
    """

    def __init__(self, path: Path | str, *, last_seq: int | None = None,
                 version: int = VERSION, min_version: int = MIN_VERSION,
                 name: str = "hub",
                 on_frame: Callable[[dict], None] | None = None,
                 timeout: float = HELLO_TIMEOUT_S,
                 send_timeout: float = SEND_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.send_timeout = send_timeout
        self.last_seq = last_seq
        self.offer = (version, min_version)
        self.name = name
        self.timeout = timeout
        self.version = 0
        self.welcome: dict = {}
        self._on_frame = on_frame
        self._frames: queue.Queue = queue.Queue()
        self._sock: socket.socket | None = None
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._waiting: dict[int, queue.Queue] = {}
        self._closed = False
        self._thread: threading.Thread | None = None

    def connect(self) -> dict:
        """Open the socket, say hello and return the keeper's
        ``welcome``. Raises :class:`Unsafe` for a socket that is not
        this user's, :class:`Refused` when the keeper says no, and
        :class:`IpcError` when nothing usable answers."""
        check_private_dir(self.path.parent)
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise IpcError(f"no keeper socket at {self.path}") from exc
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise Unsafe(f"{self.path} is not this user's socket")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(str(self.path))
            uid = peer_uid(sock)
            if uid is not None and uid != os.getuid():
                raise Unsafe(f"{self.path} is served by another user")
            sock.settimeout(None)
            # A keeper that stops reading must not hang a request in
            # its write, before the wait for a reply even starts.
            set_send_timeout(sock, self.send_timeout)
            send_frame(sock, {"t": "hello", "v": self.offer[0],
                              "min_v": self.offer[1],
                              "last_seq": self.last_seq,
                              "client": self.name, "pid": os.getpid()})
            reader = LineReader(sock)
            frame = reader.read(self.timeout)
        except (OSError, TimeoutError) as exc:
            sock.close()
            raise IpcError(f"the keeper at {self.path} did not answer: "
                           f"{exc}") from exc
        except IpcError:
            sock.close()
            raise
        if frame is None:
            sock.close()
            raise IpcError(f"the keeper at {self.path} hung up")
        if frame.get("t") != "welcome":
            sock.close()
            raise Refused(str(frame.get("reason") or frame.get("t")), frame)
        self.version = frame.get("v") if _is_version(frame.get("v")) else 0
        self.welcome = frame
        self._sock = sock
        self._thread = threading.Thread(
            target=self._read, args=(reader,), daemon=True,
            name="swe-keeper-client")
        self._thread.start()
        return frame

    def _read(self, reader: LineReader) -> None:
        try:
            while True:
                try:
                    frame = reader.read()
                except IpcError:
                    break
                if frame is None:
                    break
                if frame.get("t") == "reply":
                    with self._lock:
                        box = self._waiting.pop(frame.get("id"), None)
                    if box is not None:
                        box.put(frame)
                    continue
                self._hand(frame)
        finally:
            with self._lock:
                self._closed = True
                waiting, self._waiting = self._waiting, {}
            for box in waiting.values():
                box.put(None)
            self._hand({"t": "closed"})

    def _hand(self, frame: dict) -> None:
        if self._on_frame is not None:
            try:
                self._on_frame(frame)
            except Exception:  # a listener's bug must not end the reader
                pass
        else:
            self._frames.put(frame)

    def next(self, timeout: float | None = None) -> dict | None:
        """The next frame that is not a reply, or None when ``timeout``
        seconds pass without one."""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def request(self, op: str, timeout: float = REQUEST_TIMEOUT_S,
                **fields: Any) -> dict:
        """Send one command and wait for its reply frame. Raises
        :class:`IpcError` when the connection is gone or no reply comes
        in ``timeout`` seconds."""
        box: queue.Queue = queue.Queue(1)
        with self._lock:
            if self._closed or self._sock is None:
                raise IpcError("the keeper connection is closed")
            rpc = self._next_id
            self._next_id += 1
            self._waiting[rpc] = box
        frame = {"t": "cmd", "id": rpc, "op": op}
        frame.update({k: v for k, v in fields.items() if v is not None})
        try:
            with self._wlock:
                send_frame(self._sock, frame)
        except OSError as exc:
            with self._lock:
                self._waiting.pop(rpc, None)
            # Part of a frame may be on the wire: the stream cannot be
            # used again.
            self._hang_up()
            raise IpcError(f"could not reach the keeper: {exc}") from exc
        try:
            reply = box.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._waiting.pop(rpc, None)
            raise IpcError(f"the keeper did not answer {op} in "
                           f"{timeout:g} s") from None
        if reply is None:
            raise IpcError("the keeper connection closed before it answered")
        return reply

    @property
    def closed(self) -> bool:
        return self._closed

    def _hang_up(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self) -> None:
        """Hang up. Idempotent; the keeper keeps running."""
        self._hang_up()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)
